"""Card refs: phases, transactional transitions and convergence (spec §10)."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional, Union

from kankan import clock, faults, labels
from kankan.config import project_lock
from kankan.errors import KankanError, Status
from kankan.refstore import (CARD_PREFIX, Create, Delete, RefValue, Update, Verify, card_ref, seat_ref)
from kankan.seats import Lease
from kankan.seatstate import update_state
from kankan.trello import Card

CONVERGENCE_FAILURE_LIMIT = 3


class Phase(str, enum.Enum):
    IMPL = "impl"
    IMPLEMENTED = "implemented"
    REVIEW = "review"
    INTEGRATED = "integrated"
    PARKED = "parked"


OWNED_PHASES = (Phase.IMPL.value, Phase.REVIEW.value)


@dataclass
class CardMeta:
    phase: str
    card: str
    short: str
    owner: Optional[str] = None
    session: Optional[str] = None
    implementer: Optional[str] = None
    implementer_type: Optional[str] = None
    branch: Optional[str] = None
    target_list: Optional[str] = None
    from_list: Optional[str] = None
    created: str = field(default_factory=lambda: clock.iso(clock.now()))
    schema: int = 1
    merge: Optional[str] = None
    tested: Optional[str] = None
    cause: Optional[str] = None
    nonce: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "CardMeta":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def evolve(self, **changes) -> "CardMeta":
        data = self.to_dict()
        data.pop("nonce", None)
        data["created"] = clock.iso(clock.now())
        data.update(changes)
        return CardMeta.from_dict(data)


@dataclass
class AdminActor:
    name: str


Actor = Union[Lease, AdminActor]


@dataclass
class TransitionResult:
    ref: Optional[RefValue]
    moved: bool


def target_list_for(cfg, phase: str, seat: Optional[str], implementer: Optional[str], cause: Optional[str] = None
                    ) -> Optional[str]:
    """Destination list of each phase (§10.3)."""
    if phase == Phase.IMPL.value:
        return cfg.seats[seat].in_progress
    if phase == Phase.IMPLEMENTED.value:
        return cfg.seats[implementer].implemented
    if phase == Phase.REVIEW.value:
        return cfg.seats[seat].review
    if phase == Phase.INTEGRATED.value:
        return cfg.done
    if phase == Phase.PARKED.value:
        if cause == "yield":
            return cfg.backlog
        if cause == "block":
            return cfg.blocked
        return None
    raise KankanError(Status.INTERNAL_ERROR, f"unknown phase {phase}")


def transition(project, actor: Actor, card: Card, expect: Optional[RefValue], new: Optional[CardMeta],
               comment: Optional[str]) -> TransitionResult:
    """Git first, then Trello: one ref transaction, then a verified move and a de-duplicated comment."""
    with project_lock(project.paths):
        fresh = project.trello.card(card.id)
        ops = []
        if isinstance(actor, Lease):
            ops.append(Verify(seat_ref(actor.seat), actor.seat_oid))
        ref = card_ref(card.id)
        new_value = None
        if new is not None:
            new.from_list = fresh.list_id if fresh is not None else None
            oid = project.refs.write_blob(new.to_dict())
            new_value = project.refs.read_blob(oid)
            ops.append(Update(ref, oid, expect.oid) if expect else Create(ref, oid))
            op_id = oid
        elif expect is not None:
            ops.append(Delete(ref, expect.oid))
            op_id = "del-" + expect.oid
        else:
            raise KankanError(Status.INTERNAL_ERROR, "transition without old or new value")
        if not project.refs.transaction(ops):
            if isinstance(actor, Lease):
                current = project.refs.read(seat_ref(actor.seat))
                if current is None or current.oid != actor.seat_oid:
                    raise KankanError(Status.NOT_OWNER, f"seat {actor.seat} is no longer held by this session")
            if expect is None:
                raise KankanError(Status.ALREADY_CLAIMED, f"card {card.short or card.id} was claimed by someone else")
            raise KankanError(Status.INELIGIBLE, f"card {card.short or card.id} changed in the meantime")
        faults.point("after_ref_tx")
        actor_name = actor.seat if isinstance(actor, Lease) else actor.name
        moved = True
        if new is not None and new.target_list and fresh is not None and not fresh.closed \
                and fresh.list_id != new.target_list:
            try:
                project.trello.move_card(card.id, new.target_list)
            except KankanError as err:
                moved = False
                project.log("move_failed", card=card.id, target=new.target_list, error=err.message)
        faults.point("after_move")
        if comment and fresh is not None:
            try:
                project.trello.add_comment(card.id, comment, op_id)
            except KankanError as err:
                project.log("comment_failed", card=card.id, error=err.message)
        project.log("transition", card=card.id, actor=actor_name, old=expect.oid if expect else None,
                    new=new_value.oid if new_value else None, phase=new.phase if new else "deleted", moved=moved)
        return TransitionResult(new_value, moved)


def _parse_refs(project) -> Dict[str, Union[RefValue, KankanError]]:
    result = {}
    for ref, (oid, kind, body) in project.refs.list_raw(CARD_PREFIX).items():
        try:
            result[ref] = project.refs._parse(ref, oid, kind, body)
        except KankanError as err:
            result[ref] = err
    return result


def converge(project, lease: Lease, snap=None) -> List[str]:
    """Complete Trello moves lagging behind the refs (§10.4); never revert a human move.

    Returns events ``"<kind>:<card id>"`` with kinds ``moved``, ``deleted``, ``human_moved``,
    ``move_failed``, ``malformed``.
    """
    events: List[str] = []
    with project_lock(project.paths):
        cfg = project.cfg
        if snap is None:
            snap = project.trello.snapshot(cfg.board_id)
        for ref, value in _parse_refs(project).items():
            card_id = ref[len(CARD_PREFIX):]
            if isinstance(value, KankanError):
                events.append(f"malformed:{card_id}")
                continue
            meta = value.meta
            phase = meta.get("phase")
            if phase in OWNED_PHASES and meta.get("owner") != lease.seat:
                continue
            card = snap.cards.get(card_id)
            if card is None:
                card = project.trello.card(card_id)
            if card is None or card.closed:
                if phase in OWNED_PHASES:
                    events.append(f"human_moved:{card_id}")
                continue
            target = meta.get("target_list")
            if phase == Phase.INTEGRATED.value and card.list_id == cfg.done:
                _hold_after_integration(project, card, meta)
                if project.refs.delete(ref, value.oid):
                    labels.clear_kankan_labels(project, card, snap.labels, lease.seat)
                    project.log("ref_closed", card=card_id)
                    events.append(f"deleted:{card_id}")
                continue
            if target is None or card.list_id == target:
                _reset_failures(project, lease, card_id)
                continue
            if card.list_id != meta.get("from_list"):
                events.append(f"human_moved:{card_id}")
                continue
            try:
                project.trello.move_card(card_id, target)
                events.append(f"moved:{card_id}")
                _reset_failures(project, lease, card_id)
                if phase == Phase.INTEGRATED.value:
                    _hold_after_integration(project, card, meta)
                if phase == Phase.INTEGRATED.value and project.refs.delete(ref, value.oid):
                    labels.clear_kankan_labels(project, card, snap.labels, lease.seat)
                    events.append(f"deleted:{card_id}")
            except KankanError as err:
                events.append(f"move_failed:{card_id}")
                project.log("converge_move_failed", card=card_id, error=err.message)
                if _count_failure(project, lease, card_id) >= CONVERGENCE_FAILURE_LIMIT:
                    try:
                        labels.set_label(project, card_id, cfg.label_error, "convergence", lease.seat,
                                         comment=f"kankan: cannot move this card to its expected list ({err.message}).",
                                         op_id=f"convfail-{value.oid}")
                    except KankanError as label_err:
                        project.log("label_failed", card=card_id, error=label_err.message)
    return events


def _count_failure(project, lease: Lease, card_id: str) -> int:
    def bump(state):
        state.convergence_failures[card_id] = state.convergence_failures.get(card_id, 0) + 1
        return state.convergence_failures[card_id]
    return update_state(project.paths, lease.seat, bump)


def _reset_failures(project, lease: Lease, card_id: str) -> None:
    from kankan.seatstate import load_state
    if card_id in load_state(project.paths, lease.seat).convergence_failures:
        update_state(project.paths, lease.seat, lambda s: s.convergence_failures.pop(card_id, None))


def _hold_after_integration(project, card, meta: dict) -> None:
    """§9.4: HOLD on a card whose merge is already on main is irreversible; say how to undo it."""
    if project.cfg.label_hold in card.label_ids and meta.get("merge"):
        from kankan import messages
        try:
            project.trello.comment_once(card.id, messages.HOLD_AFTER_INTEGRATION.format(merge=meta["merge"]),
                                        f"holdlate-{meta['merge']}")
        except KankanError as err:
            project.log("comment_failed", card=card.id, error=err.message)
