"""Task choice, eligibility and preparation before involving the agent (spec §9, §11)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from kankan import boardconfig, boardsettings, gitutil, labels, messages, modelcheck, worktrees
from kankan.cards import OWNED_PHASES, CardMeta, Phase, _parse_refs, converge, target_list_for, transition
from kankan.config import check_lists, check_registry, project_lock
from kankan.errors import KankanError, Status
from kankan.refstore import CARD_PREFIX, RefValue
from kankan.seats import Lease, seat_type
from kankan.seatstate import load_state, update_state
from kankan.trello import Card, Snapshot


@dataclass
class Claimed:
    meta: CardMeta
    moved: bool


@dataclass
class Choice:
    kind: str  # impl, review, resume_impl, resume_review, hold_yield, hold_wip, moved_yield, moved_wip
    card: Card
    ref: Optional[RefValue]


def card_refs(project) -> Dict[str, RefValue]:
    """Valid card refs by card id (malformed ones are left to reconcile)."""
    return {ref[len(CARD_PREFIX):]: value for ref, value in _parse_refs(project).items()
            if isinstance(value, RefValue)}


def has_hold(project, card: Card) -> bool:
    return project.cfg.label_hold in card.label_ids


def deps_satisfied(project, snap: Snapshot, card: Card) -> bool:
    from kankan import deps
    try:
        return deps.dep_status(project, snap, card).ok
    except KankanError as err:
        project.log("deps_unreadable", card=card.id, error=err.message)
        return False


def _own_choices(project, lease: Lease, snap: Snapshot, refs: Dict[str, RefValue]) -> List[Choice]:
    result = []
    for phase in (Phase.REVIEW.value, Phase.IMPL.value):
        for card_id, value in sorted(refs.items()):
            meta = value.meta
            if meta.get("owner") != lease.seat or meta.get("phase") != phase:
                continue
            card = snap.cards.get(card_id)
            if card is None or card.list_id not in (meta.get("target_list"), meta.get("from_list")):
                # §14.6: moved out of the seat's lists, archived or deleted by the user
                fetched = card or project.trello.card(card_id) or _gone_card(card_id, meta)
                dirty = worktrees.dirty_files(project.paths.seat_worktree(lease.seat))
                result.append(Choice("moved_wip" if dirty else "moved_yield", fetched, value))
                continue
            if card.list_id != meta.get("target_list"):
                continue  # lagging move: convergence completes it
            if has_hold(project, card):
                dirty = worktrees.dirty_files(project.paths.seat_worktree(lease.seat))
                result.append(Choice("hold_wip" if dirty else "hold_yield", card, value))
            else:
                result.append(Choice(f"resume_{phase}", card, value))
    return result


def _gone_card(card_id: str, meta: dict) -> Card:
    """Placeholder for a card deleted on Trello (``closed`` and no list)."""
    return Card(id=card_id, short=meta.get("short", ""), board_id="", name="", desc="", url="", list_id="",
                pos=0, label_ids=[], attachments=[], closed=True, last_activity="", )


def review_candidates(project, lease: Lease, snap: Snapshot, refs: Dict[str, RefValue]) -> List[Choice]:
    implemented_lists = {sc.implemented for sc in project.cfg.seats.values()}
    result = []
    for card in sorted(snap.cards.values(), key=lambda c: c.pos):
        if card.list_id not in implemented_lists or has_hold(project, card):
            continue
        value = refs.get(card.id)
        if value is None:
            continue
        meta = value.meta
        if meta.get("phase") not in (Phase.IMPLEMENTED.value, Phase.PARKED.value) or not meta.get("implementer"):
            continue
        if meta.get("implementer_type") == lease.type:
            continue
        result.append(Choice("review", card, value))
    return result


def backlog_candidates(project, snap: Snapshot, refs: Dict[str, RefValue]) -> List[Choice]:
    result = []
    for card in sorted(snap.cards.values(), key=lambda c: c.pos):
        if card.list_id != project.cfg.backlog or has_hold(project, card):
            continue
        value = refs.get(card.id)
        if value is not None and value.meta.get("phase") != Phase.PARKED.value:
            continue
        if not deps_satisfied(project, snap, card):
            continue
        result.append(Choice("impl", card, value))
    return result


def choose(project, lease: Lease, snap: Snapshot, refs: Dict[str, RefValue], skip: Optional[Set[str]] = None
           ) -> Optional[Choice]:
    """Priority (§9.1): own work, then review, then Backlog from the top."""
    skip = skip or set()
    for group in (_own_choices(project, lease, snap, refs), review_candidates(project, lease, snap, refs),
                  backlog_candidates(project, snap, refs)):
        for choice in group:
            if choice.card.id not in skip:
                return choice
    return None


def claim_impl(project, lease: Lease, choice: Choice, pair=None) -> Optional[Claimed]:
    """Claim a Backlog card: branch check, ref transition, HOLD recheck, branch checkout."""
    card = choice.card
    repo = project.paths.repo
    worktree = project.paths.seat_worktree(lease.seat)
    try:
        existing = worktrees.find_branches(repo, card.short)
        if len(existing) > 1:
            raise KankanError(Status.NEEDS_RECONCILIATION, "branches", {"branches": existing})
    except KankanError as err:
        if err.status == Status.NEEDS_RECONCILIATION:
            labels.set_label(project, card.id, project.cfg.label_error, "branches", lease.seat,
                             comment=messages.BRANCH_CONFLICT.format(branches=", ".join(err.data["branches"])),
                             op_id=f"branches-{card.id}-{'-'.join(err.data['branches'])}")
            raise KankanError(Status.INELIGIBLE, f"card {card.short} has more than one branch")
        raise
    if worktrees.dirty_files(worktree):
        raise KankanError(Status.NEEDS_RECONCILIATION, f"worktree {worktree} is not clean; cannot start a new card")
    branch = existing[0] if existing else worktrees.branch_name(card.short)
    meta = CardMeta(phase=Phase.IMPL.value, card=card.id, short=card.short, owner=lease.seat, session=lease.session,
                    implementer=lease.seat, implementer_type=lease.type, branch=branch,
                    target_list=target_list_for(project.cfg, Phase.IMPL.value, lease.seat, None))
    result = transition(project, lease, card, choice.ref, meta,
                        messages.CLAIMED.format(seat=lease.seat, branch=branch) + _claim_selection(lease, pair, "implementation"))
    fresh = project.trello.card(card.id)
    if fresh is not None and has_hold(project, fresh):
        park = CardMeta.from_dict(result.ref.meta).evolve(phase=Phase.PARKED.value, owner=None, cause="yield",
                                                           target_list=project.cfg.backlog)
        transition(project, lease, card, result.ref, park, messages.HOLD_AFTER_CLAIM)
        return None
    branch = worktrees.prepare_branch(repo, card.short, project.cfg.integration_branch)
    worktrees.checkout(worktree, branch)
    return Claimed(CardMeta.from_dict(result.ref.meta), result.moved)


def _free_branch_for_review(project, lease: Lease, card: Card, value: RefValue) -> None:
    """§11 start of review: a clean blocked-* worktree is removed; an unfinished merge waits; elsewhere is an error."""
    repo = project.paths.repo
    branch = value.meta.get("branch")
    where = worktrees.where_checked_out(repo, branch)
    if where is None or where.resolve() == project.paths.seat_worktree(lease.seat).resolve():
        return
    if where.name == f"blocked-{card.short}":
        if worktrees.merge_in_progress(where) or worktrees.dirty_files(where):
            project.trello.comment_once(card.id, messages.MERGE_NOT_COMPLETED.format(path=where),
                                       f"mergeopen-{value.oid}")
            raise KankanError(Status.INELIGIBLE, f"merge not completed in {where}")
        gitutil.run(repo, ["worktree", "remove", str(where)])
        project.log("blocked_worktree_removed", card=card.id, path=str(where))
        return
    labels.set_label(project, card.id, project.cfg.label_error, "branch_elsewhere", lease.seat,
                     comment=messages.BRANCH_ELSEWHERE.format(branch=branch, path=where),
                     op_id=f"elsewhere-{value.oid}")
    raise KankanError(Status.INELIGIBLE, f"branch {branch} is checked out in {where}")


def claim_review(project, lease: Lease, choice: Choice, pair=None) -> Optional[Claimed]:
    """Claim a card in an Implemented list for review (§9.2, §11)."""
    card, value = choice.card, choice.ref
    worktree = project.paths.seat_worktree(lease.seat)
    if worktrees.dirty_files(worktree):
        raise KankanError(Status.NEEDS_RECONCILIATION, f"worktree {worktree} is not clean; cannot start a review")
    _free_branch_for_review(project, lease, card, value)
    meta = CardMeta.from_dict(value.meta).evolve(
        phase=Phase.REVIEW.value, owner=lease.seat, session=lease.session, cause=None,
        target_list=target_list_for(project.cfg, Phase.REVIEW.value, lease.seat, None))
    result = transition(project, lease, card, value, meta, messages.REVIEW_CLAIMED.format(
        seat=lease.seat, branch=meta.branch, implementer=meta.implementer) + _claim_selection(lease, pair, "review"))
    fresh = project.trello.card(card.id)
    if fresh is not None and has_hold(project, fresh):
        back = CardMeta.from_dict(result.ref.meta).evolve(
            phase=Phase.IMPLEMENTED.value, owner=None, session=None,
            target_list=target_list_for(project.cfg, Phase.IMPLEMENTED.value, None, meta.implementer))
        transition(project, lease, card, result.ref, back, messages.HOLD_AFTER_CLAIM_REVIEW)
        return None
    worktrees.checkout(worktree, meta.branch)
    return Claimed(CardMeta.from_dict(result.ref.meta), result.moved)


def _prepare_resume(project, lease: Lease, choice: Choice) -> None:
    worktree = project.paths.seat_worktree(lease.seat)
    branch = choice.ref.meta.get("branch")
    if worktrees.current_branch(worktree) != branch:
        if worktrees.dirty_files(worktree):
            raise KankanError(Status.NEEDS_RECONCILIATION, f"worktree {worktree} is dirty and not on {branch}")
        worktrees.checkout(worktree, branch)


def task_role(choice: Choice) -> str:
    phase = choice.ref.meta.get("phase") if choice.ref else None
    return 'review' if choice.kind in ('review', 'resume_review') or phase == 'review' else 'implementation'


def _claim_selection(lease, pair, role):
    return messages.CLAIM_SELECTION.format(role='reviewing' if role == 'review' else 'implementing',
                                         type=lease.type, model=pair.model, effort=pair.effort) if pair else ''


def tick(project, lease: Lease, *, defer_assignment: bool = False) -> Optional[dict]:
    """One launcher iteration without the agent: checks, convergence, choice and preparation.

    Returns the task dict stored in seat state, or None when nothing was assigned.
    """
    cfg = project.reload_config()
    snap = project.trello.snapshot(cfg.board_id)
    check_registry(cfg, snap.desc)
    check_lists(cfg, snap.lists)
    if converge(project, lease, snap):
        snap = project.trello.snapshot(cfg.board_id)
    adapters = boardsettings.adapters_for(project.paths)
    boardsettings.apply_read(project, boardsettings.from_snapshot(project, snap), adapters)
    boardsettings.drain_actions(project)
    refs = card_refs(project)
    skip: Set[str] = set()
    while True:
        choice = choose(project, lease, snap, refs, skip)
        if choice is None:
            return None
        skip.add(choice.card.id)
        try:
            role = task_role(choice)
            pair = boardconfig.effective(project.paths, role).get(lease.type)
            cleanup = choice.kind in ('hold_yield', 'moved_yield')
            if not cleanup:
                if pair is None:
                    continue
                if not defer_assignment:
                    modelcheck.check(adapters[lease.type], pair)
                if boardsettings.role_block(project, lease.type, role, pair):
                    continue
            with project_lock(project.paths):
                from kankan import seats
                seats.verify_owner(project, lease.seat, lease.session)
                if load_state(project.paths, lease.seat).stop_requested:
                    return None
                fresh = project.trello.snapshot(cfg.board_id)
                check_registry(cfg, fresh.desc)
                current_refs = card_refs(project)
                # Recheck eligibility after any outside-lock model check.
                candidates = _own_choices(project, lease, fresh, current_refs) + review_candidates(project, lease, fresh, current_refs) + backlog_candidates(project, fresh, current_refs)
                updated = next((candidate for candidate in candidates if candidate.card.id == choice.card.id and candidate.kind == choice.kind), None)
                if updated is None:
                    continue
                if not cleanup:
                    current_pair = boardconfig.effective(project.paths, role).get(lease.type)
                    if current_pair != pair or boardsettings.role_block(project, lease.type, role, pair):
                        continue
                task = _prepare(project, lease, updated, pair=pair)
        except KankanError as err:
            if err.status in (Status.ALREADY_CLAIMED, Status.INELIGIBLE):
                project.log("claim_skipped", card=choice.card.id, reason=err.message)
                refs = card_refs(project)
                continue
            raise
        if task is not None:
            return task if defer_assignment else activate_task(project, lease, task)
        if choice.kind in ("impl", "review"):
            return None
        refs = card_refs(project)


def _prepare(project, lease: Lease, choice: Choice, pair=None) -> Optional[dict]:
    def store(project, lease, kind, card, branch, resumed):
        from kankan.refstore import card_ref
        value = project.refs.read(card_ref(card.id))
        return {"kind": kind, "card": card.id, "short": card.short, "branch": branch, "resumed": resumed,
                "role": task_role(choice), "model": pair.model if pair else None, "effort": pair.effort if pair else None,
                "ref_oid": value.oid if value else None, "session": lease.session}
    kind = choice.kind
    if kind == "hold_yield":
        from kankan import agentcmds
        agentcmds.kankan_yield(project, lease, choice.card, choice.ref)
        project.log("hold_yield", card=choice.card.id)
        return None
    if kind == "impl":
        claimed = claim_impl(project, lease, choice, pair)
        if claimed is None or not claimed.moved:
            return None
        return store(project, lease, "impl", choice.card, claimed.meta.branch, resumed=False)
    if kind == "review":
        claimed = claim_review(project, lease, choice, pair)
        if claimed is None or not claimed.moved:
            return None
        return store(project, lease, "review", choice.card, claimed.meta.branch, resumed=False)
    if kind == "hold_wip":
        return store(project, lease, "hold_wip", choice.card, choice.ref.meta.get("branch"), resumed=True)
    if kind in ("moved_yield", "moved_wip"):
        cause = _user_cause(project, choice.card)
        if kind == "moved_yield":
            from kankan import agentcmds
            agentcmds.kankan_yield(project, lease, None if cause == "user_deleted" else choice.card, choice.ref, cause)
            project.log("user_move_yield", card=choice.card.id, cause=cause)
            return None
        task = store(project, lease, "hold_wip", choice.card, choice.ref.meta.get("branch"), resumed=True)
        task["suspend_cause"] = "user_moved"
        return task
    _prepare_resume(project, lease, choice)
    return store(project, lease, kind.replace("resume_", ""), choice.card, choice.ref.meta.get("branch"),
                       resumed=True)


def _validate_prepared(project, lease: Lease, prepared: dict):
    """Called under the project lock, both at activation and immediately before pane writes."""
    from kankan import agentcmds, seats
    from kankan.refstore import card_ref
    cfg = project.reload_config()
    seats.verify_owner(project, lease.seat, lease.session)
    if prepared.get('session') != lease.session or load_state(project.paths, lease.seat).stop_requested:
        return None
    snap = project.trello.snapshot(cfg.board_id)
    check_registry(cfg, snap.desc)
    check_lists(cfg, snap.lists)
    value = project.refs.read(card_ref(prepared['card']))
    if value is None or ('ref_oid' in prepared and value.oid != prepared['ref_oid']) or value.meta.get('owner') != lease.seat:
        return None
    role = 'review' if value.meta.get('phase') == 'review' else 'implementation'
    if value.meta.get('phase') not in ('impl', 'review') or prepared.get('role', role) != role:
        return None
    card = snap.cards.get(prepared['card']) or project.trello.card(prepared['card'])
    gone = card is None or card.closed or card.list_id != value.meta.get('target_list')
    held = card is not None and has_hold(project, card)
    task = dict(prepared)
    if gone or held:
        dirty = worktrees.dirty_files(project.paths.seat_worktree(lease.seat))
        if not dirty:
            cause = _user_cause(project, card or _gone_card(prepared['card'], value.meta)) if gone else 'yield'
            agentcmds.kankan_yield(project, lease, card, value, cause)
            return None
        task['kind'] = 'hold_wip'
        task['suspend_cause'] = 'user_moved' if gone else 'hold'
    card = card or _gone_card(prepared['card'], value.meta)
    return task, card


def activate_task(project, lease: Lease, prepared: dict) -> Optional[dict]:
    with project_lock(project.paths):
        validated = _validate_prepared(project, lease, prepared)
        if validated is None:
            return None
        task, card = validated
        return _store_task(project, lease, task['kind'], card, task['branch'], task['resumed'], prepared=task)


def refresh_task(project, lease: Lease, task: dict) -> Optional[dict]:
    """Revalidate an activated but unsent assignment, retaining its task ID and pinned pair."""
    with project_lock(project.paths):
        validated = _validate_prepared(project, lease, dict(task, session=lease.session))
        if validated is None:
            return None
        updated = {key: value for key, value in validated[0].items() if key != 'session'}
        def replace(state):
            if state.task and state.task.get('id') == task['id']:
                state.task = updated
                return updated
            return None
        return update_state(project.paths, lease.seat, replace)


def release_unsent(project, lease: Lease, prepared: dict) -> bool:
    """A stop arrived between a fresh claim and its prompt: give the untouched card back (T§3 Restart step 6).

    Resumed work, a WIP-only task, a dirty worktree, a changed ref or a card no longer where the claim put it
    stay as they are: the seat keeps them for its next start (or the user-move path handles them).
    """
    from kankan import agentcmds, seats
    from kankan.refstore import card_ref
    if prepared.get('resumed') or prepared.get('kind') not in ('impl', 'review') or not prepared.get('ref_oid'):
        return False
    with project_lock(project.paths):
        seats.verify_owner(project, lease.seat, lease.session)
        value = project.refs.read(card_ref(prepared['card']))
        if value is None or value.oid != prepared['ref_oid'] or value.meta.get('owner') != lease.seat:
            return False
        if worktrees.dirty_files(project.paths.seat_worktree(lease.seat)):
            return False
        card = project.trello.card(prepared['card'])
        if card is None or card.closed or card.list_id != value.meta.get('target_list'):
            return False
        agentcmds.kankan_yield(project, lease, card, value)
        def clear(state):
            if state.task and state.task.get('card') == prepared['card']:
                state.task = None
                state.signal = None
        update_state(project.paths, lease.seat, clear)
    project.log("stop_yield", seat=lease.seat, card=prepared['card'])
    return True


def _user_cause(project, card: Card) -> str:
    if card.closed and not card.list_id:
        return "user_deleted"
    return "user_archived" if card.closed else "user_moved"


def _store_task(project, lease: Lease, kind: str, card: Card, branch: Optional[str], resumed: bool, *, prepared=None) -> dict:
    task = {"id": uuid.uuid4().hex[:12], "kind": kind, "card": card.id, "short": card.short, "branch": branch,
            "resumed": resumed}
    if prepared:
        task.update({key: prepared[key] for key in ("role", "model", "effort", "suspend_cause", "ref_oid") if key in prepared})

    def store(state):
        state.task = task
        state.signal = None
    update_state(project.paths, lease.seat, store)
    project.log("task_assigned", seat=lease.seat, **task)
    return task


def active_types(project) -> Set[str]:
    from kankan import seats
    return {sc.type for name, sc in project.cfg.seats.items() if seats.liveness(project, name) == "alive"}


def waiting_reasons(project, snap: Snapshot, refs: Dict[str, RefValue], types: Optional[Set[str]] = None
                    ) -> Dict[str, str]:
    """Why cards in Backlog or Implemented lists are not being picked up."""
    reasons = {}
    implemented_lists = {sc.implemented for sc in project.cfg.seats.values()}
    types = active_types(project) if types is None else types
    for card in snap.cards.values():
        value = refs.get(card.id)
        phase = value.meta.get("phase") if value else None
        if card.list_id == project.cfg.backlog:
            if has_hold(project, card):
                reasons[card.id] = "hold"
            elif phase not in (None, Phase.PARKED.value):
                reasons[card.id] = "ineligible_phase"
            elif not deps_satisfied(project, snap, card):
                reasons[card.id] = "deps"
        elif card.list_id in implemented_lists:
            if has_hold(project, card):
                reasons[card.id] = "hold"
            elif phase not in (Phase.IMPLEMENTED.value, Phase.PARKED.value) or not (value and value.meta.get("implementer")):
                reasons[card.id] = "ineligible_phase"
            elif not (types - {value.meta.get("implementer_type")}):
                reasons[card.id] = "no_reviewer_type"
    for card in snap.cards.values():
        if card.id in reasons:
            continue
        if card.list_id == project.cfg.backlog:
            eligible, role = types, 'implementation'
        elif card.list_id in implemented_lists:
            value = refs.get(card.id)
            eligible, role = types - {value.meta.get('implementer_type')} if value else set(), 'review'
        else:
            continue
        if eligible:
            value = boardconfig.effective(project.paths, role)
            blocks = [boardsettings.role_block(project, name, role, value[name]) if name in value else 'model_check_unverified' for name in eligible]
            if all(blocks):
                reasons[card.id] = 'model_check_failed' if 'model_check_failed' in blocks else 'model_check_unverified'
    return reasons
