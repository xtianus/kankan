"""Diagnostics and administration: status, claims, reconcile, release, seat recover/remove, purge-remote."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from kankan import boardconfig, boardsettings, clock, messages, modelcheck, seats, workflow
from kankan.adapters import ModelEffort
from kankan.config import qa_list_open
from kankan.seatstate import load_state


def _age(created: str) -> int:
    try:
        then = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=clock.now().tzinfo)
    except (TypeError, ValueError):
        return -1
    return int((clock.now() - then).total_seconds())


def _seat_order(name: str) -> tuple:
    """Seats by type, work seats by number, the Q&A seat after them."""
    agent_type, suffix = name.rsplit("-", 1)
    return (agent_type, 1, 0) if suffix == "qa" else (agent_type, 0, int(suffix))


def status(project) -> dict:
    cfg = project.reload_config()
    seat_rows = []
    for name in sorted(cfg.seats, key=_seat_order):
        state = load_state(project.paths, name)
        seat_rows.append({"seat": name, "type": cfg.seats[name].type, "liveness": seats.liveness(project, name),
                          "launcher_state": state.launcher_state, "task": state.task,
                          "stop_requested": state.stop_requested, "running_selection": None})
        # A launcher killed without its shutdown leaves the field behind: only a live seat runs an agent.
        if seat_rows[-1]["liveness"] == "alive":
            seat_rows[-1]["running_selection"] = state.running_selection
    # the Q&A seat implements and reviews nothing: only work seats make a type active
    types = {row["type"] for row in seat_rows if row["liveness"] == "alive" and cfg.seats[row["seat"]].role != "qa"}
    snap = project.trello.snapshot(cfg.board_id)
    refs = workflow.card_refs(project)
    reasons = workflow.waiting_reasons(project, snap, refs, types=types)
    waiting = [{"card": card_id, "short": snap.cards[card_id].short, "title": snap.cards[card_id].name,
                "reason": reason} for card_id, reason in sorted(reasons.items(), key=lambda i: snap.cards[i[0]].pos)]
    adapters = boardsettings.adapters_for(project.paths)
    state = boardconfig.load(project.paths)
    values = boardconfig._values(state, boardconfig.schema(adapters, boardconfig.roles(project.paths)))
    settings = {}
    for role, value in values.items():
        record = state.settings.get(role, {})
        card_record = state.cards.get(record.get('card_id'), {})
        checks = {}
        for name, pair in value.items():
            if name in adapters:
                result = modelcheck.cached(adapters[name], pair)
                checks[name] = {'status': result.status if result else 'unchecked',
                                'message': result.message if result else '', 'retry_at': result.retry_at if result else None}
        settings[role] = {'value': boardconfig.encode(value), 'card_id': record.get('card_id'),
                          'structural_error': card_record.get('structural') or bool(record.get('invalid')),
                          'probe_failures': card_record.get('probes', {}), 'checks': checks}
    for row in waiting:
        if row['reason'].startswith('model_check_'):
            role = 'implementation' if snap.cards[row['card']].list_id == cfg.backlog else 'review'
            row['model_checks'] = settings[role]['checks']
    available = cfg.settings_list in snap.lists and not snap.lists[cfg.settings_list].closed
    return {"seats": seat_rows, "waiting": waiting, "qa": _qa_status(project, seat_rows, snap, refs, reasons),
            "branches": branch_divergence(project), 'settings': settings,
            'settings_warning': None if available else messages.SETTINGS_MISSING_LIST,
            'settings_errors': [{'card_id': ident, 'name': record.get('key'), 'structural': record.get('structural', []),
                                 'probe_failures': record.get('probes', {})}
                                for ident, record in state.cards.items() if record.get('invalid')]}


def _qa_status(project, seat_rows: List[dict], snap, refs, reasons: Dict[str, str]) -> Optional[dict]:
    """The Q&A seat, its active card and turn, the number of other cards in ``Q&A`` and a warning (questions F§2);
    None when no Q&A seat and no ``Q&A`` list are configured."""
    cfg = project.cfg
    seat = cfg.qa_seat()
    if seat is None and cfg.qa_list is None:
        return None
    row = next((r for r in seat_rows if r["seat"] == seat), None)
    active = next((value for value in refs.values() if value.meta.get("phase") == "qa"
                   and value.meta.get("owner") == seat), None) if seat else None
    card = active.meta["card"] if active else None
    task = (row or {}).get("task") or {}
    turn = task.get("turn") if task.get("kind") == "qa" and task.get("card") == card and card else None
    queue = sum(1 for c in snap.cards.values() if c.list_id == cfg.qa_list and not c.closed and c.id != card) \
        if cfg.qa_list else 0
    if not qa_list_open(cfg, snap.lists):
        warning = messages.QA_LIST_MISSING
    elif any(reason == "no_qa_seat" for reason in reasons.values()):
        warning = messages.QA_CARDS_WAIT
    else:
        warning = None
    return {"seat": seat, "liveness": row["liveness"] if row else None,
            "launcher_state": row["launcher_state"] if row else None, "card": card,
            "short": active.meta.get("short") if active else None, "turn": turn, "queue": queue, "warning": warning}


def branch_divergence(project) -> dict:
    """How far the integration branch and the user branch diverge, in commits (spec §12.5)."""
    cfg, repo = project.cfg, project.paths.repo
    integration, user = cfg.integration_branch, cfg.user_branch
    result = {"integration": integration, "user": None, "ahead": None, "behind": None,
              "oldest_unmerged_integration": None}
    if integration == user or not gitutil.rev(repo, f"refs/heads/{integration}") or \
            not gitutil.rev(repo, f"refs/heads/{user}"):
        return result
    counts = gitutil.out(repo, ["rev-list", "--left-right", "--count", f"refs/heads/{integration}...refs/heads/{user}"])
    ahead, behind = (int(n) for n in counts.split())
    dates = gitutil.out(repo, ["log", "--first-parent", "--merges", "--format=%cI",
                               f"refs/heads/{user}..refs/heads/{integration}"]).splitlines()
    result.update(user=user, ahead=ahead, behind=behind, oldest_unmerged_integration=dates[-1] if dates else None)
    return result


def status_text(data: dict) -> str:
    lines = ["Seats:"]
    if not data["seats"]:
        lines.append("  (none)")
    for row in data["seats"]:
        task = row["task"]
        task_text = f"{task['kind']} {task['short']}" if task else "-"
        running = row.get('running_selection')
        if running:
            task_text += messages.STATUS_RUNNING.format(**running)
        stop = " (stop requested)" if row["stop_requested"] else ""
        lines.append(f"  {row['seat']:<10} {row['liveness']:<11} {row['launcher_state']:<13} {task_text}{stop}")
    branches = data.get("branches") or {}
    if branches.get("user"):
        lines.append(f"Branches: {branches['integration']} is {branches['ahead']} commits ahead / "
                     f"{branches['behind']} behind {branches['user']}")
    lines.append("Waiting cards:")
    if not data["waiting"]:
        lines.append("  (none)")
    for row in data["waiting"]:
        lines.append(f"  {row['short']}  {row['reason']:<16} {row['title']}")
    if data.get('settings'):
        lines.append(messages.STATUS_SETTINGS)
        for role, record in data['settings'].items():
            value = {name: ModelEffort(**pair) for name, pair in record['value'].items()}
            lines.append(messages.STATUS_SETTING.format(role=role, values=boardconfig.display(value),
                         error=' (Error)' if record['structural_error'] or record['probe_failures'] else ''))
            for name, check in record['checks'].items():
                if check['status'] != 'passed':
                    lines.append(f"    {name}: {check['status']} {check['message']}" + (f" (retry at {check['retry_at']})" if check['retry_at'] else ''))
        if data.get('settings_warning'):
            lines.append('  ' + data['settings_warning'])
    qa_row = data.get("qa")
    if qa_row:
        if qa_row["seat"]:
            lines.append(messages.STATUS_QA.format(seat=qa_row["seat"], liveness=qa_row["liveness"],
                                                   state=qa_row["launcher_state"] or "-",
                                                   card=qa_row["short"] or "-", queue=qa_row["queue"]))
        else:
            lines.append(messages.STATUS_QA_NONE.format(queue=qa_row["queue"]))
        if qa_row["warning"]:
            lines.append(qa_row["warning"])
    return "\n".join(lines)


def claims(project) -> List[Dict]:
    rows = []
    for card_id, value in sorted(workflow.card_refs(project).items()):
        meta = value.meta
        rows.append({"card": card_id, "short": meta.get("short"), "phase": meta.get("phase"),
                     "owner": meta.get("owner"), "age_s": _age(meta.get("created", "")), "branch": meta.get("branch")})
    return rows


def claims_text(rows: List[Dict]) -> str:
    if not rows:
        return "No card refs."
    return "\n".join(f"{r['short']}  {r['phase']:<12} {r['owner'] or '-':<10} {r['age_s']:>7}s  {r['branch']}"
                     for r in rows)


# ---------------------------------------------------------------------- reconcile
from dataclasses import asdict, dataclass  # noqa: E402

from kankan import attachments, auditlog, cards, deps, gitutil, labels, qa, worktrees  # noqa: E402
from kankan.config import check_registry, project_lock, read_json, atomic_write_json  # noqa: E402
from kankan.errors import KankanError  # noqa: E402
from kankan.refstore import CARD_PREFIX, RefValue, card_ref  # noqa: E402

LABEL_AGE_HOURS = 24


@dataclass
class Issue:
    kind: str
    subject: str
    detail: str
    fixed: bool = False


def _mismatch_memory(project) -> dict:
    return read_json(project.paths.kankan_dir / "reconcile.json", {}) or {}


def reconcile(project, fix: bool = False, *, record: bool = True) -> List[Issue]:
    """Report inconsistencies (§15.3); with ``fix`` repair only the certain cases."""
    cfg = project.reload_config()
    issues: List[Issue] = []
    snap = project.trello.snapshot(cfg.board_id)
    try:
        check_registry(cfg, snap.desc)
    except KankanError as err:
        issues.append(Issue("registry_invalid", cfg.board_short, err.message))
    raw = project.refs.list_raw(CARD_PREFIX)
    refs: Dict[str, RefValue] = {}
    for ref, (oid, kind, body) in raw.items():
        card_id = ref[len(CARD_PREFIX):]
        try:
            refs[card_id] = project.refs._parse(ref, oid, kind, body)
        except KankanError as err:
            issues.append(Issue("malformed_blob", card_id, err.message))
    liveness = {name: seats.liveness(project, name) for name in cfg.seats}
    # card refs against lists
    memory = _mismatch_memory(project)
    seen_now = {}
    fetched = {}
    qa_ended = set()
    for card_id, value in refs.items():
        meta = value.meta
        card = snap.cards.get(card_id)
        if card is None:
            card = fetched.setdefault(card_id, project.trello.card(card_id))
        if meta.get("phase") == cards.Phase.QA.value:
            if meta.get("owner") not in cfg.seats:  # an unfinished replacement whose new seat was removed (T§2)
                issues.append(Issue("qa_owner_unregistered", card_id,
                                    messages.QA_OWNER_UNREGISTERED.format(owner=meta.get("owner"))))
            # questions T§3: a conversation whose card left Q&A ends at its seat; reported only without one
            if (card is None or card.closed or card.list_id != cfg.qa_list) \
                    and liveness.get(meta.get("owner")) != "alive":
                qa_ended.add(card_id)
                issues.append(Issue("qa_conversation_ended", card_id,
                                    f"the card left Q&A and seat {meta.get('owner')} is not alive"))
            continue
        if card is None:
            issues.append(Issue("ref_of_deleted_card", card_id, f"phase {meta.get('phase')}, cause {meta.get('cause')}"))
            continue
        if card.closed:
            issues.append(Issue("ref_of_archived_card", card_id, f"phase {meta.get('phase')}"))
            continue
        target = meta.get("target_list")
        if target and card.list_id != target:
            key = f"{card_id}:{value.oid}"
            seen_now[key] = memory.get(key, clock.iso(clock.now()))
            if key in memory:
                issues.append(Issue("phase_list_mismatch", card_id,
                                    f"phase {meta.get('phase')} expects list {target}, card is in {card.list_id}"))
    if record:
        atomic_write_json(project.paths.kankan_dir / "reconcile.json", seen_now)
    seat_lists = cfg.seat_lists()
    for card in snap.cards.values():
        if card.list_id in seat_lists and card.id not in refs:
            issues.append(Issue("card_without_ref", card.id, f"{card.short} is in a list of {seat_lists[card.list_id]}"))
    # Done cards
    merged = {m["card"] for m in _all_merges(project)}
    qa_cards = qa.recorded(project.paths)
    for card in snap.cards.values():
        if card.list_id == cfg.done:
            if card.id in refs or card.id in qa_cards:
                pass
            elif card.short not in merged:
                issues.append(Issue("done_without_merge", card.id, f"{card.short} has no Kankan-Card merge on the integration branch"))
    # seats
    for name, state in liveness.items():
        if state == "dead":
            issues.append(Issue("dead_seat", name, "registered seat whose holder died"))
        elif state == "unavailable":
            issues.append(Issue("seat_unavailable", name, "processes of the dead session survive"))
        seat_cfg = cfg.seats[name]
        if seat_cfg.role == "qa":
            continue
        for list_id in (seat_cfg.in_progress, seat_cfg.implemented, seat_cfg.review):
            lst = snap.lists.get(list_id)
            if lst is None or lst.closed:
                issues.append(Issue("seat_without_lists", name, f"list {list_id} is archived or missing"))
                break
    # dependencies
    for card in snap.cards.values():
        if card.list_id == cfg.backlog and deps.dependencies(card):
            try:
                result = deps.dep_status(project, snap, card)
            except KankanError as err:
                result = deps.DepStatus(False, [], [("?", err.message)])
            for short, reason in result.problems:
                issues.append(Issue("unsatisfiable_dependency", card.id, f"{card.short} -> {short}: {reason}"))
    # branches and blocked worktrees
    shorts = {meta.meta.get("short") for meta in refs.values()} | {c.short for c in snap.cards.values()}
    for short in sorted(s for s in shorts if s):
        found = worktrees.find_branches(project.paths.repo, short)
        if len(found) > 1:
            issues.append(Issue("multiple_branches", short, ", ".join(found)))
    blocked_shorts = {c.short for c in snap.cards.values() if c.list_id == cfg.blocked}
    for entry in worktrees.worktree_list(project.paths.repo):
        path = entry.get("worktree", "")
        name = path.rsplit("/", 1)[-1]
        if name.startswith("blocked-") and name[len("blocked-"):] not in blocked_shorts:
            issues.append(Issue("stale_blocked_worktree", path, "its card is no longer in Blocked"))
    # attachment directories of cards without a ref
    ref_shorts = {value.meta.get("short") for value in refs.values()}
    for name in attachments.directories(project.paths):
        if name not in ref_shorts:
            issues.append(Issue("attachment_dir_without_ref", name, "no card ref has this shortLink"))
    # Q&A conversation directories no ref names
    ref_conversations = {value.meta.get("conversation") for value in refs.values()}
    for name in qa.conversations(project.paths):
        if name not in ref_conversations:
            issues.append(Issue("qa_conversation_dir_without_ref", name, "no card ref names this conversation"))
    # old Error/Question labels
    limit = LABEL_AGE_HOURS * 3600
    for name in cfg.seats:
        for card_id, causes in load_state(project.paths, name).label_causes.items():
            for label_id, entry in causes.items():
                if label_id in (cfg.label_error, cfg.label_question) and _age(entry.get("at", "")) > limit:
                    issues.append(Issue("old_label", card_id, f"{entry.get('cause')} since {entry.get('at')}"))
    # push failures
    if cfg.push_main:
        remote_main = gitutil.rev(project.paths.repo, f"refs/remotes/origin/{cfg.integration_branch}")
        for event in auditlog.read(project.paths):
            if event.get("event") == "push_failed" and event.get("merge"):
                if not remote_main or not gitutil.is_ancestor(project.paths.repo, event["merge"], remote_main):
                    issues.append(Issue("push_failed", event["merge"], "the merge is not on origin"))
    for path in worktrees.main_checked_out(project.paths.repo, cfg.integration_branch):
        issues.append(Issue("main_checked_out", str(path), f"{cfg.integration_branch} is checked out"))
    if fix:
        issues += _fix(project, snap, refs, liveness, qa_ended)
    return issues


def _all_merges(project):
    from kankan.integrate import merges_on_main
    return merges_on_main(project.paths.repo, project.cfg.integration_branch, 100000)


def _fix(project, snap, refs, liveness, qa_ended=frozenset()) -> List[Issue]:
    fixed: List[Issue] = []
    cfg = project.cfg
    gone = _qa_record_gone(project, snap)
    with project_lock(project.paths):
        for card_id, value in refs.items():
            meta = value.meta
            owner = meta.get("owner")
            if owner and liveness.get(owner) == "alive":
                continue
            if card_id in qa_ended:
                if project.refs.delete(f"{CARD_PREFIX}{card_id}", value.oid):
                    fixed.append(Issue("qa_conversation_ref_deleted", card_id, "the card left Q&A", True))
                continue
            card = snap.cards.get(card_id)
            if meta.get("phase") == "integrated" and card is not None and card.list_id == cfg.done:
                if cards.delete_integrated_ref(project, f"{CARD_PREFIX}{card_id}", value):
                    fixed.append(Issue("integrated_ref_deleted", card_id, "card already in Done", True))
        fixed += _fix_attachment_dirs(project)
        fixed += _fix_qa_record(project, gone)
        fixed += _fix_conversation_dirs(project)
        for label_id in labels.gc_resume_labels(project):
            fixed.append(Issue("resume_label_deleted", label_id, "unused or expired", True))
        snap = project.trello.snapshot(cfg.board_id)
        for card in snap.cards.values():
            if card.list_id != cfg.done:
                continue
            value = refs.get(card.id)
            if value is not None and value.meta.get("owner") and liveness.get(value.meta["owner"]) == "alive":
                continue
            for label_id in labels.kankan_label_ids(project, card, snap.labels):
                project.trello.remove_label(card.id, label_id)
                fixed.append(Issue("done_label_removed", card.id, label_id, True))
    project.log("reconcile_fix", fixed=len(fixed))
    return fixed


def _fix_attachment_dirs(project) -> List[Issue]:
    """Under the project lock: delete the attachment directories of cards without a ref. Deletes nothing when a
    ref is malformed or has no valid shortLink (its directory could be wrongly taken for an orphan)."""
    current = cards.parse_refs(project)
    shorts = set()
    for value in current.values():
        short = None if isinstance(value, KankanError) else value.meta.get("short")
        if not attachments._valid_short(short):
            project.log("attachment_fix_skipped", reason="a card ref is malformed or has no valid shortLink")
            return []
        shorts.add(short)
    return [Issue("attachment_dir_deleted", name, "no card ref has this shortLink", True)
            for name in attachments.directories(project.paths)
            if name not in shorts and attachments._valid_short(name) and attachments.discard(project.paths, name)]


def _qa_record_gone(project, snap) -> Dict[str, tuple]:
    """Without the project lock: the ``qa.json`` entries whose card is archived or deleted, one card read each for
    the cards missing from the board snapshot, as ``{card_id: (detail, ref oid before the read)}``."""
    gone = {}
    for card_id in sorted(qa.recorded(project.paths) - set(snap.cards)):
        try:
            before = project.refs.read(card_ref(card_id))
            card = project.trello.card(card_id)
        except KankanError as err:
            project.log("qa_record_read_failed", card=card_id, error=err.message)
            continue
        if card is None or card.closed:
            gone[card_id] = ("the card was deleted" if card is None else "the card is archived",
                             before.oid if before else None)
    return gone


def _fix_qa_record(project, gone: Dict[str, tuple]) -> List[Issue]:
    """Under the project lock: drop the ``qa.json`` entries of cards claimed for implementation since (an
    ``impl``, ``implemented``, ``review`` or ``integrated`` ref), and of the archived or deleted cards found by
    ``_qa_record_gone`` whose card ref did not change since (questions T§2)."""
    current = workflow.card_refs(project)
    claimed = {"impl", "implemented", "review", "integrated"}
    fixed = []
    for card_id in sorted(qa.recorded(project.paths)):
        value = current.get(card_id)
        if value is not None and value.meta.get("phase") in claimed:
            detail = f"the card holds a {value.meta.get('phase')} ref"
        elif card_id in gone:
            try:
                now = project.refs.read(card_ref(card_id))
            except KankanError:
                continue
            detail, before = gone[card_id]
            if (now.oid if now else None) != before:
                continue  # changed during the read: decided at the next run
        else:
            continue
        qa.record_remove(project.paths, card_id)
        fixed.append(Issue("qa_record_entry_removed", card_id, detail, True))
    return fixed


def _fix_conversation_dirs(project) -> List[Issue]:
    """Under the project lock: delete the conversation directories no card ref names. Deletes nothing when a
    ref is malformed (its conversation could be wrongly taken for an orphan)."""
    names = set()
    for value in cards.parse_refs(project).values():
        if isinstance(value, KankanError):
            project.log("qa_conversation_fix_skipped", reason="a card ref is malformed")
            return []
        names.add(value.meta.get("conversation"))
    return [Issue("qa_conversation_dir_deleted", name, "no card ref names this conversation", True)
            for name in qa.conversations(project.paths)
            if name not in names and qa.valid_conversation(name) and qa.discard_conversation(project.paths, name)]


def issues_text(issues: List[Issue]) -> str:
    if not issues:
        return "No issues found."
    return "\n".join(f"{'fixed ' if i.fixed else ''}{i.kind}: {i.subject} — {i.detail}" for i in issues)


def issues_data(issues: List[Issue]) -> List[dict]:
    return [asdict(i) for i in issues]


# ---------------------------------------------------------------------- administration
from kankan import messages  # noqa: E402
from kankan.cards import AdminActor, CardMeta, Phase, transition  # noqa: E402
from kankan.errors import Status  # noqa: E402
from kankan.refstore import seat_ref, validate_seat  # noqa: E402


def _find_ref(project, card: str):
    for card_id, value in workflow.card_refs(project).items():
        if card in (card_id, value.meta.get("short")):
            return card_id, value
    raise KankanError(Status.INELIGIBLE, f"no card ref for {card}")


def release_force(project, card: str) -> dict:
    """Bring a card's ref to ``parked`` (cause admin_release); the card stays where it is."""
    with project_lock(project.paths):
        card_id, value = _find_ref(project, card)
        trello_card = project.trello.card(card_id) or workflow._gone_card(card_id, value.meta)
        new = CardMeta.from_dict(value.meta).evolve(phase=Phase.PARKED.value, owner=None, session=None,
                                                    cause="admin_release", target_list=None)
        transition(project, AdminActor("release --force"), trello_card, value, new, messages.ADMIN_RELEASED)
        project.log("admin_release", card=card_id, old_owner=value.meta.get("owner"))
    return {"card": card_id, "phase": "parked"}


def seat_recover(project, seat: str, kill: bool) -> dict:
    """Recover a seat that is alive but stuck: its session loses ownership (and is killed with ``kill``)."""
    validate_seat(seat)
    with project_lock(project.paths):
        value = project.refs.read(seat_ref(seat))
        survivors = seats.recover_dead(project, seat, kill_alive=True) if kill else []
        if survivors:
            raise KankanError(Status.NEEDS_ACTION, f"processes of {seat} survive: {survivors}", {"pids": survivors})
        if value is not None and not project.refs.delete(seat_ref(seat), value.oid):
            raise KankanError(Status.INELIGIBLE, f"the ref of {seat} changed meanwhile; retry")
        for card in seats.owned_cards(project, seat, ("impl", "review", "qa")):
            try:
                project.trello.add_comment(card.meta["card"], messages.ADMIN_SEAT_RECOVERED.format(seat=seat),
                                           f"admin-recover-{value.oid if value else 'none'}")
            except KankanError as err:
                project.log("comment_failed", error=err.message)
        project.log("admin_seat_recover", seat=seat, kill=kill)
    return {"seat": seat, "killed": kill}


def _remove_worktree(project, seat: str) -> bool:
    return worktrees.remove_if_clean(project.paths.repo, project.paths.seat_worktree(seat))


def _qa_seat_remove(project, seat: str) -> dict:
    """``seat remove <type>-qa`` (questions F§6 §7.1): refused unless the seat is free; its conversation ends
    (``qa.end_admin``, one project lock hold per conversation); no list is archived."""
    def check(cfg) -> None:
        if seat not in cfg.seats:
            raise KankanError(Status.USAGE_ERROR, messages.UNKNOWN_SEAT.format(seat=seat))
        state = seats.liveness(project, seat)
        if state != "free":
            template = {"dead": messages.QA_SEAT_REMOVE_DEAD,
                        "unavailable": messages.QA_SEAT_REMOVE_UNAVAILABLE}.get(state, messages.QA_SEAT_REMOVE_RUNNING)
            raise KankanError(Status.NEEDS_ACTION, template.format(seat=seat, state=state),
                              {"blockers": [f"the seat is {state}"]})
    with qa.lifecycle_lock(project.paths):  # never between the stages of a replacement
        with project_lock(project.paths):
            check(project.reload_config())
        qa.end_admin(project, seat)
        with project_lock(project.paths):
            cfg = project.reload_config()
            check(cfg)
            held = [v.meta.get("short") for v in workflow.card_refs(project).values() if v.meta.get("owner") == seat]
            if held:  # a conversation changed meanwhile: end_admin ends it at the next run
                blocker = messages.SEAT_HOLDS_CARDS.format(cards=", ".join(held))
                raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_BLOCKED.format(seat=seat, blockers=blocker),
                                  {"blockers": [blocker]})
            del cfg.seats[seat]
            from kankan.config import save_config
            save_config(project.paths, cfg)
            qa.drop_stale_record(project, None)  # a replacement towards this seat can no longer complete
            removed = _remove_worktree(project, seat)
            project.log("seat_removed", seat=seat, worktree_removed=removed)
    return {"seat": seat, "worktree_removed": removed}


def seat_remove(project, seat: str) -> dict:
    validate_seat(seat)
    if seats.is_qa(seat):
        return _qa_seat_remove(project, seat)
    with project_lock(project.paths):
        cfg = project.reload_config()
        if seat not in cfg.seats:
            raise KankanError(Status.USAGE_ERROR, f"unknown seat {seat}")
        blockers = []
        state = seats.liveness(project, seat)
        if state != "free":
            blockers.append(f"the seat is {state}")
        held = [v.meta.get("short") for v in workflow.card_refs(project).values()
                if seat in (v.meta.get("owner"),) or (v.meta.get("implementer") == seat and v.meta.get("phase") == "implemented")]
        if held:
            blockers.append("it holds cards: " + ", ".join(held))
        snap = project.trello.snapshot(cfg.board_id)
        seat_cfg = cfg.seats[seat]
        lists = (seat_cfg.in_progress, seat_cfg.implemented, seat_cfg.review)
        in_lists = [c.short for c in snap.cards.values() if c.list_id in lists]
        if in_lists:
            blockers.append("its lists are not empty: " + ", ".join(in_lists))
        if blockers:
            raise KankanError(Status.NEEDS_ACTION, f"cannot remove {seat}: " + "; ".join(blockers),
                              {"blockers": blockers})
        for list_id in lists:
            project.trello.archive_list(list_id)
        del cfg.seats[seat]
        from kankan.config import save_config
        save_config(project.paths, cfg)
        removed = _remove_worktree(project, seat)
        project.log("seat_removed", seat=seat, worktree_removed=removed)
    return {"seat": seat, "worktree_removed": removed}


def purge_remote(project, remote: str = "origin") -> dict:
    """Delete refs/agent-* published by mistake (bypassing the local pre-push hook)."""
    out = gitutil.out(project.paths.repo, ["ls-remote", remote, "refs/agent-*"], timeout=120)
    deleted = []
    for line in out.splitlines():
        ref = line.split("\t", 1)[1] if "\t" in line else ""
        if ref.startswith("refs/agent-"):
            gitutil.run(project.paths.repo, ["push", "--no-verify", "-q", remote, "--delete", ref], timeout=120)
            deleted.append(ref)
    project.log("purge_remote", remote=remote, deleted=deleted)
    return {"remote": remote, "deleted": deleted}
