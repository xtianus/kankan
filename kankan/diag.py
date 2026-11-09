"""Diagnostics and administration: status, claims, reconcile, release, seat recover/remove, purge-remote."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from kankan import clock, seats, workflow
from kankan.seatstate import load_state


def _age(created: str) -> int:
    try:
        then = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=clock.now().tzinfo)
    except (TypeError, ValueError):
        return -1
    return int((clock.now() - then).total_seconds())


def status(project) -> dict:
    cfg = project.reload_config()
    seat_rows = []
    for name in sorted(cfg.seats, key=lambda n: (n.rsplit("-", 1)[0], int(n.rsplit("-", 1)[1]))):
        state = load_state(project.paths, name)
        seat_rows.append({"seat": name, "type": cfg.seats[name].type, "liveness": seats.liveness(project, name),
                          "launcher_state": state.launcher_state, "task": state.task,
                          "stop_requested": state.stop_requested})
    types = {row["type"] for row in seat_rows if row["liveness"] == "alive"}
    snap = project.trello.snapshot(cfg.board_id)
    refs = workflow.card_refs(project)
    reasons = workflow.waiting_reasons(project, snap, refs, types=types)
    waiting = [{"card": card_id, "short": snap.cards[card_id].short, "title": snap.cards[card_id].name,
                "reason": reason} for card_id, reason in sorted(reasons.items(), key=lambda i: snap.cards[i[0]].pos)]
    return {"seats": seat_rows, "waiting": waiting, "branches": branch_divergence(project)}


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

from kankan import auditlog, deps, gitutil, labels, worktrees  # noqa: E402
from kankan.config import check_registry, project_lock, read_json, atomic_write_json  # noqa: E402
from kankan.errors import KankanError  # noqa: E402
from kankan.refstore import CARD_PREFIX, RefValue  # noqa: E402

LABEL_AGE_HOURS = 24


@dataclass
class Issue:
    kind: str
    subject: str
    detail: str
    fixed: bool = False


def _mismatch_memory(project) -> dict:
    return read_json(project.paths.kankan_dir / "reconcile.json", {}) or {}


def reconcile(project, fix: bool = False) -> List[Issue]:
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
    for card_id, value in refs.items():
        meta = value.meta
        card = snap.cards.get(card_id)
        if card is None:
            card = fetched.setdefault(card_id, project.trello.card(card_id))
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
    atomic_write_json(project.paths.kankan_dir / "reconcile.json", seen_now)
    seat_lists = cfg.seat_lists()
    for card in snap.cards.values():
        if card.list_id in seat_lists and card.id not in refs:
            issues.append(Issue("card_without_ref", card.id, f"{card.short} is in a list of {seat_lists[card.list_id]}"))
    # Done cards
    merged = {m["card"] for m in _all_merges(project)}
    for card in snap.cards.values():
        if card.list_id == cfg.done:
            if card.id in refs:
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
        issues += _fix(project, snap, refs, liveness)
    return issues


def _all_merges(project):
    from kankan.integrate import merges_on_main
    return merges_on_main(project.paths.repo, project.cfg.integration_branch, 100000)


def _fix(project, snap, refs, liveness) -> List[Issue]:
    fixed: List[Issue] = []
    cfg = project.cfg
    with project_lock(project.paths):
        for card_id, value in refs.items():
            meta = value.meta
            owner = meta.get("owner")
            if owner and liveness.get(owner) == "alive":
                continue
            card = snap.cards.get(card_id)
            if meta.get("phase") == "integrated" and card is not None and card.list_id == cfg.done:
                if project.refs.delete(f"{CARD_PREFIX}{card_id}", value.oid):
                    fixed.append(Issue("integrated_ref_deleted", card_id, "card already in Done", True))
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
        for card in seats.owned_cards(project, seat):
            try:
                project.trello.add_comment(card.meta["card"], messages.ADMIN_SEAT_RECOVERED.format(seat=seat),
                                           f"admin-recover-{value.oid if value else 'none'}")
            except KankanError as err:
                project.log("comment_failed", error=err.message)
        project.log("admin_seat_recover", seat=seat, kill=kill)
    return {"seat": seat, "killed": kill}


def seat_remove(project, seat: str) -> dict:
    validate_seat(seat)
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
        worktree = project.paths.seat_worktree(seat)
        removed = False
        if (worktree / ".git").exists() and not worktrees.dirty_files(worktree):
            gitutil.run(project.paths.repo, ["worktree", "remove", str(worktree)])
            removed = True
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
