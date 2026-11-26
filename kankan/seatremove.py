"""``kankan seat remove`` (seat spec): the cards of a removed seat go to the rest of the board.

Each card is classified by the rows of seat T§4.1 from its ref and its list, read again under the project lock of
its own move, then moved with one of four actions: a ref transition, the completion of a pending move, the closing
of an integrated card, or a plain move of a card without ref.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set

from kankan.boardreader import LIVE
from kankan import agents_cmd, auditlog, clock, gitutil, labels, messages, seats, worktrees
from kankan.cards import (AdminActor, CardMeta, Phase, _hold_after_integration, confirm_move, delete_integrated_ref,
                          implemented_destination, parse_refs, transition)
from kankan.config import atomic_write_json, file_lock, project_lock, save_config
from kankan.errors import KankanError, Status
from kankan.refstore import CARD_PREFIX, RefValue, card_ref, seat_ref
from kankan.seatstate import load_state
from kankan.trello import Card

ACTOR = AdminActor("seat remove")


@dataclass
class Move:
    card_id: str
    short: str
    row: int  # seat T§4.1 row, 1–8
    to: Optional[str]  # destination list; None for a closed card (row 8)


def seat_lists(cfg, seat: str) -> Set[str]:
    seat_cfg = cfg.seats.get(seat)
    if seat_cfg is None or seat_cfg.role == "qa":
        return set()
    return {lst for lst in (seat_cfg.in_progress, seat_cfg.implemented, seat_cfg.review) if lst}


def removing_cfg(project, seat: str):
    """The configuration as seen by the destinations: the removed seat marked as being removed."""
    cfg = project.reload_config()
    if seat in cfg.seats and not cfg.seats[seat].removing:
        cfg = copy.deepcopy(cfg)
        cfg.seats[seat].removing = "immediate"
    return cfg


def _row(meta: Optional[dict], card: Optional[Card], lists: Set[str], seat: str, own_implemented: Optional[str]) -> int:
    """The first matching row of seat T§4.1 (row 8, a closed or deleted card, is checked first); 0: nothing to do."""
    closed = card is None or card.closed
    if meta is not None:
        phase, owner, target = meta.get("phase"), meta.get("owner"), meta.get("target_list")
        if closed:  # archived or deleted by the user: its claim must stop naming the seat or its lists
            tied = owner == seat or target in lists or meta.get("from_list") in lists
            return 8 if tied and phase != Phase.INTEGRATED.value else 0
        if phase == Phase.INTEGRATED.value:
            return 1
        if phase == Phase.IMPL.value and owner == seat:
            return 2
        if phase == Phase.REVIEW.value and owner == seat:
            return 3
        if phase in (Phase.QA.value, Phase.IMPL.value, Phase.REVIEW.value) and owner and owner != seat:
            return 4 if target and card.list_id != target and card.list_id in lists else 0
        if phase == Phase.PARKED.value and meta.get("cause") in ("block", "handoff"):
            return 5 if target and card.list_id != target else 0
        if target and target not in lists and phase != Phase.IMPLEMENTED.value:
            return 5 if card.list_id != target and card.list_id in lists else 0
        if phase == Phase.IMPLEMENTED.value:
            return 6 if (card.list_id in lists or target in lists) else 0
        if phase == Phase.PARKED.value and meta.get("implementer") and card.list_id == own_implemented:
            return 6
    if not closed and card.list_id in lists:
        return 7
    return 0


def plan_moves(project, seat: str) -> List[Move]:
    """seat T§4 step 1: the cards in the seat's lists, held by the seat, or on their way to its lists."""
    cfg = removing_cfg(project, seat)
    lists = seat_lists(cfg, seat)
    refs: Dict[str, RefValue] = {}
    for ref, value in parse_refs(project).items():
        if isinstance(value, KankanError):
            raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_MALFORMED.format(
                seat=seat, card=ref[len(CARD_PREFIX):], error=value.message), {"ref": ref})
        refs[ref[len(CARD_PREFIX):]] = value
    snap = project.board.snapshot(LIVE)
    for card_id, value in refs.items():  # before anything is changed: a claim missing a field stops the removal
        meta = value.meta
        if meta.get("owner") == seat or meta.get("target_list") in lists or meta.get("from_list") in lists:
            try:
                CardMeta.from_dict(meta)
            except TypeError as err:
                raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_MALFORMED.format(
                    seat=seat, card=card_id, error=str(err)), {"ref": card_ref(card_id)}) from err
    ids = {card.id for card in snap.cards.values() if card.list_id in lists}
    for card_id, value in refs.items():
        meta = value.meta
        if meta.get("owner") == seat or meta.get("target_list") in lists or meta.get("from_list") in lists:
            ids.add(card_id)
    moves = []
    for card_id in sorted(ids):
        card = snap.cards.get(card_id) or project.board.card(card_id, LIVE)
        value = refs.get(card_id)
        row, to = _classify(cfg, seat, lists, value, card)
        if row:
            moves.append(Move(card_id, (card.short if card else "") or (value.meta.get("short", "") if value else ""),
                              row, to))
    return moves


def _classify(cfg, seat: str, lists: Set[str], value: Optional[RefValue], card: Optional[Card]):
    meta = value.meta if value is not None else None
    own_implemented = cfg.seats[seat].implemented if seat in cfg.seats else None
    row = _row(meta, card, lists, seat, own_implemented)
    if row == 1:
        return row, cfg.done
    if row in (2, 7):
        return row, cfg.backlog
    if row in (3, 6):
        return row, implemented_destination(cfg, meta.get("implementer"))[1]
    if row in (4, 5):
        return row, meta.get("target_list")
    return row, None


def apply_move(project, seat: str, move: Move) -> Optional[Move]:
    """Re-read the card and its ref under the project lock, re-classify, move; then comment and clear the labels
    the seat set. Returns the move done, None when nothing was left to do or the Trello move failed."""
    with project_lock(project.paths):
        cfg = removing_cfg(project, seat)
        lists = seat_lists(cfg, seat)
        value = project.refs.read(card_ref(move.card_id))
        card = project.board.card(move.card_id, LIVE)
        row, to = _classify(cfg, seat, lists, value, card)
        if not row:
            return None
        if row == 1:
            done = _close_integrated(project, seat, value, card)
        elif row in (4, 5):
            done = _complete_pending(project, value, card)
        elif row == 7 and value is None:
            done = _plain_move(project, card, cfg.backlog)
        else:
            done = _transition(project, cfg, seat, row, value, card)
    if not done:
        return None
    result = Move(move.card_id, card.short if card else move.short, row, to)
    if row != 8:
        _comment(project, cfg, seat, result)
    _clear_seat_labels(project, seat, move.card_id)
    return result


def _transition(project, cfg, seat: str, row: int, value: RefValue, card: Optional[Card]) -> bool:
    meta = CardMeta.from_dict(value.meta)
    if row == 8:
        new = meta.evolve(phase=Phase.PARKED.value, owner=None, session=None, cause="seat_removed", target_list=None)
    elif row in (2, 7):
        new = meta.evolve(phase=Phase.PARKED.value, owner=None, session=None, cause="seat_removed",
                          target_list=cfg.backlog)
    else:  # 3, 6: back to review
        phase, target, cause = implemented_destination(cfg, value.meta.get("implementer"))
        new = meta.evolve(phase=phase, owner=None, session=None, cause=cause, target_list=target)
    if card is None:
        card = Card(id=value.meta["card"], short=value.meta.get("short", ""), board_id="", name="", desc="", url="",
                    list_id="", pos=0, label_ids=[], attachments=[], closed=True, last_activity="")
    result = transition(project, ACTOR, card, value, new, None)
    if row == 8 and result.ref is not None:  # no move to confirm: drop the source list transition() recorded
        confirm_move(project, card_ref(card.id), result.ref)
    return result.moved or row == 8


def _complete_pending(project, value: RefValue, card: Card) -> bool:
    meta = value.meta
    try:
        project.trello.move_card(card.id, meta["target_list"], pos="bottom" if meta.get("pos") == "bottom" else "top")
    except KankanError as err:
        project.log("seat_remove_move_failed", card=card.id, error=err.message)
        return False
    if meta.get("from_list"):
        confirm_move(project, card_ref(card.id), value)
    return True


def _close_integrated(project, seat: str, value: RefValue, card: Card) -> bool:
    if card.list_id != project.cfg.done:
        try:
            project.trello.move_card(card.id, project.cfg.done)
        except KankanError as err:
            project.log("seat_remove_move_failed", card=card.id, error=err.message)
            return False
    fresh = project.board.card(card.id, LIVE) or card
    _hold_after_integration(project, fresh, value.meta)
    delete_integrated_ref(project, card_ref(card.id), value)
    return True


def _plain_move(project, card: Card, target: str) -> bool:
    try:
        project.trello.move_card(card.id, target, pos="top")
    except KankanError as err:
        project.log("seat_remove_move_failed", card=card.id, error=err.message)
        return False
    return True


def _comment(project, cfg, seat: str, move: Move) -> None:
    if move.row == 1:
        text = messages.SEAT_REMOVED_DONE.format(seat=seat)
    elif move.to == cfg.backlog and move.row in (2, 3, 6, 7):
        text = messages.SEAT_REMOVED_BACKLOG.format(seat=seat)
    elif move.row in (3, 6):
        text = messages.SEAT_REMOVED_IMPLEMENTED.format(seat=seat, list=cfg.workflow_lists().get(move.to, move.to))
    else:
        text = messages.SEAT_REMOVED_PENDING.format(seat=seat, list=cfg.workflow_lists().get(move.to, move.to))
    try:
        project.trello.comment_once(move.card_id, text, f"seat-remove-{seat}-{move.card_id}")
    except KankanError as err:
        project.log("comment_failed", card=move.card_id, error=err.message)


def _clear_seat_labels(project, seat: str, card_id: str) -> None:
    causes = load_state(project.paths, seat).label_causes.get(card_id) or {}
    labels.clear_causes(project, seat, card_id, list(causes))


# ---------------------------------------------------------------------- completion (seat T§6)

LOCK_WAIT_SECONDS = 600


def _lock_path(paths, seat: str):
    return paths.seats_dir / f"{seat}.remove.lock"


def _set_error(project, seat: str, error: Optional[str]) -> None:
    with project_lock(project.paths):
        cfg = project.reload_config()
        if seat in cfg.seats and cfg.seats[seat].remove_error != error:
            cfg.seats[seat].remove_error = error
            save_config(project.paths, cfg)
            project.reload_config()


def last_report(project, seat: str) -> Optional[dict]:
    """The report of the latest completed removal of ``seat`` (log entry ``seat_removed``)."""
    for entry in reversed(auditlog.read(project.paths)):
        if entry.get("event") == "seat_removed" and entry.get("seat") == seat and entry.get("report"):
            return entry["report"]
    return None


def stop_seat(project, seat: str, *, kill_alive: bool) -> None:
    """Terminate the seat's processes and free it (as ``seat recover --kill``, without its comment), then mark its
    removal ``immediate`` under the same lock, so no launcher occupies it while the completion runs. A running
    seat is refused unless ``kill_alive``; survivors stop the removal before anything moves."""
    with project_lock(project.paths):
        state = seats._liveness(project, seat)
        if state == "alive" and not kill_alive:
            raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_ALIVE.format(seat=seat))
        if state != "free":
            _kill(project, seat, state)
        cfg = project.reload_config()
        if seat in cfg.seats and cfg.seats[seat].removing != "immediate":
            cfg.seats[seat].removing = "immediate"
            save_config(project.paths, cfg)
            project.reload_config()


def _kill(project, seat: str, state: str) -> None:
    """Under the project lock: terminate the seat's recorded processes and delete its seat ref."""
    survivors = seats.recover_dead(project, seat, kill_alive=True)
    if survivors:
        raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_SURVIVORS.format(seat=seat, pids=survivors),
                          {"pids": survivors})
    value = project.refs.read(seat_ref(seat))
    if value is not None:
        project.refs.delete(seat_ref(seat), value.oid)
    project.log("seat_remove_stopped", seat=seat, liveness=state)


def drop_worktree(project, seat: str) -> List[str]:
    """seat T§4 step 6: abort an unfinished merge, then remove the worktree with its uncommitted changes, before any
    card moves (no card branch stays checked out). Returns the discarded files."""
    path = project.paths.seat_worktree(seat)
    if not (path / ".git").exists():
        return []
    if worktrees.merge_in_progress(path):
        try:  # best effort: the forced removal below discards the merge state anyway
            gitutil.run(path, ["merge", "--abort"])
        except KankanError as err:
            project.log("seat_remove_merge_abort_failed", seat=seat, error=err.message)
    discarded = worktrees.dirty_files(path)
    gitutil.run(project.paths.repo, ["worktree", "remove", "--force", str(path)])
    project.log("seat_worktree_removed", seat=seat, discarded=discarded)
    return discarded


def remaining_cards(project, seat: str) -> List[str]:
    """Open cards still in the seat's lists, or refs still targeting them or moving from them (seat T§6 step 3)."""
    cfg = project.reload_config()
    lists = seat_lists(cfg, seat)
    if not lists:
        return []
    snap = project.board.snapshot(LIVE)
    left = {card.short for card in snap.cards.values() if card.list_id in lists}
    for value in parse_refs(project).values():
        if not isinstance(value, RefValue):
            continue
        card = snap.cards.get(value.meta.get("card"))  # open cards only: a closed card's claim names no list
        if card is None:
            continue
        target = value.meta.get("target_list")
        if target in lists or (value.meta.get("from_list") in lists and card.list_id != target):
            left.add(value.meta.get("short") or value.meta.get("card"))
    return sorted(left)


def _sweep_labels(project, seat: str) -> None:
    """Clear every label cause the seat still records, except on cards in ``Blocked`` and on closed cards."""
    causes = load_state(project.paths, seat).label_causes
    for card_id, by_label in list(causes.items()):
        card = project.board.card(card_id, LIVE)
        if card is None or card.closed or card.list_id == project.cfg.blocked:
            continue
        labels.clear_causes(project, seat, card_id, list(by_label))


def _forget_seat(project, seat: str) -> None:
    """The last steps: reset the seat's state and proc files (lock files stay), then delete the entry."""
    for name in (f"{seat}.state.json", f"{seat}.proc.json"):
        try:
            (project.paths.seats_dir / name).unlink()
        except FileNotFoundError:
            pass
    with project_lock(project.paths):
        cfg = project.reload_config()
        cfg.seats.pop(seat, None)
        save_config(project.paths, cfg)
        project.reload_config()


def complete(project, seat: str) -> dict:
    """seat T§6: finish the removal of ``seat``; serialized by the removal lock, the entry deleted last."""
    with file_lock(_lock_path(project.paths, seat), timeout=LOCK_WAIT_SECONDS, what="seat removal lock"):
        cfg = project.reload_config()
        if seat not in cfg.seats:
            return last_report(project, seat) or {"seat": seat, "moved": [], "discarded": [], "lists_archived": 0,
                                                  "worktree_removed": False}
        _set_error(project, seat, None)
        stop_seat(project, seat, kill_alive=False)
        try:
            return _complete(project, seat, cfg.seats[seat].role == "qa")
        except KankanError as err:
            _set_error(project, seat, err.message)
            raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_INCOMPLETE.format(seat=seat, error=err.message),
                              err.data) from err


def _finish_block(project, seat: str) -> None:
    """An interrupted ``block`` of the seat is finished first (its card goes to ``Blocked`` with its blocked
    worktree), so the card is not given to another agent while its branch is checked out there."""
    if load_state(project.paths, seat).block_journal is None:
        return
    from kankan import integrate
    with project_lock(project.paths):
        lease = seats._occupy(project, seat, seats.seat_type(seat), None, comment=False)
    if lease is None:
        raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_ALIVE.format(seat=seat))
    try:
        integrate.resume_block(project, lease)
    finally:
        seats.release(project, lease)


def _complete(project, seat: str, is_qa: bool) -> dict:
    worktree = project.paths.seat_worktree(seat)
    had_worktree = (worktree / ".git").exists()
    if not is_qa:
        plan_moves(project, seat)  # a malformed claim stops the removal before anything is changed
        _finish_block(project, seat)
    discarded = drop_worktree(project, seat)
    moved: List[dict] = []
    if is_qa:
        from kankan import qa
        with qa.lifecycle_lock(project.paths):
            qa.end_admin(project, seat)
            with project_lock(project.paths):
                qa.drop_stale_record(project, None)
                owned = [v.meta.get("short") or v.meta.get("card") for v in qa._owned(project, seat)]
            if owned:
                raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_QA_OWNED.format(
                    seat=seat, cards=", ".join(owned)), {"cards": owned})
    else:
        for move in plan_moves(project, seat):
            done = apply_move(project, seat, move)
            if done is not None:
                moved.append({"card": done.card_id, "short": done.short, "to": done.to})
        left = remaining_cards(project, seat)
        if left:
            raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_CARDS_LEFT.format(seat=seat, cards=", ".join(left)),
                              {"cards": left})
    _sweep_labels(project, seat)
    archived = 0
    if not is_qa:
        cfg = project.reload_config()
        for list_id in sorted(seat_lists(cfg, seat)):
            info = project.board.list_info(list_id, LIVE)
            if info is not None and not info.closed:
                project.trello.archive_list(list_id)
            archived += 1
    report = {"seat": seat, "moved": moved, "discarded": discarded, "lists_archived": archived,
              "worktree_removed": had_worktree and not (worktree / ".git").exists()}
    _forget_seat(project, seat)
    project.log("seat_removed", seat=seat, report=report)
    return report


# ---------------------------------------------------------------------- autostart and stop record (seat T§7)

def _lowered(args: str, cfg, seat: str) -> str:
    wanted, qa_type = agents_cmd.parse_wanted(args.split()) if args.strip() else ({}, None)
    seat_cfg = cfg.seats.get(seat)
    if seat_cfg is not None and seat_cfg.role == "qa":
        qa_type = None
    elif seat_cfg is not None:
        left = sum(1 for name, sc in cfg.work_seats().items()
                   if sc.type == seat_cfg.type and not sc.removing and name != seat)
        if seat_cfg.type in wanted:
            wanted[seat_cfg.type] = min(wanted[seat_cfg.type], left)
    parts = [f"{t}={n}" for t, n in wanted.items() if n > 0] + ([f"qa={qa_type}"] if qa_type else [])
    return " ".join(parts)


def adjust_autostart(project, seat: str, apply: bool = True) -> str:
    """seat T§7: lower the unit's and the stop record's count for the seat's type to the seats left (``qa=`` dropped
    with the Q&A seat). Returns "unchanged", the unit's new arguments, "disabled" or "error: <message>"; with
    ``apply`` false nothing is written (the confirmation's preview)."""
    from kankan import stopall
    cfg = project.reload_config()
    result = "unchanged"
    unit_args = stopall._unit_args(project) if agents_cmd.unit_path(project).exists() else None
    if unit_args is not None:
        new = _lowered(unit_args, cfg, seat)
        if new != unit_args.strip():
            result = new or "disabled"
            if apply:
                try:
                    if new:
                        wanted, qa_type = agents_cmd.parse_wanted(new.split())
                        agents_cmd.autostart_enable(project, wanted, qa=qa_type)
                    else:
                        agents_cmd.autostart_disable(project)
                except KankanError as err:
                    result = f"error: {err.message}"
    record = stopall.load_record(project.paths)
    if apply and record and record.get("up"):
        new = _lowered(record["up"], cfg, seat)
        if new != record["up"]:
            if new:
                atomic_write_json(stopall.record_path(project.paths), dict(record, up=new))
            else:
                stopall.record_path(project.paths).unlink(missing_ok=True)
    if apply:
        project.log("seat_remove_autostart", seat=seat, result=result)
    return result


# ---------------------------------------------------------------------- immediate removal (seat T§4)

def _list_name(cfg, list_id: Optional[str]) -> str:
    if list_id is None:
        return "(stays where it is)"
    return cfg.workflow_lists().get(list_id, list_id)


def _mark(project, seat: str, mode: str) -> bool:
    """Record the removal mode; False when the seat was removed meanwhile (by another command or its launcher)."""
    with project_lock(project.paths):
        cfg = project.reload_config()
        if seat not in cfg.seats:
            return False
        cfg.seats[seat].removing = mode
        save_config(project.paths, cfg)
        project.reload_config()
    project.log("seat_remove_requested", seat=seat, mode=mode)
    return True


def _already_removed(project, seat: str) -> dict:
    return last_report(project, seat) or {"seat": seat}


def describe(project, seat: str, moves: List[Move], discarded: List[str], autostart: str,
             conversation: Optional[str] = None, confirm_text: Optional[str] = None) -> str:
    """The plan shown before an immediate removal (seat F§3.2 steps 1–2)."""
    cfg = project.cfg
    lines = [messages.SEAT_REMOVE_PLAN.format(seat=seat)]
    for move in moves:
        lines.append(f"  {move.short or move.card_id} → {_list_name(cfg, move.to)}")
    if conversation:
        lines.append(messages.SEAT_REMOVE_PLAN_QA.format(card=conversation))
    if discarded:
        lines.append(messages.SEAT_REMOVE_PLAN_DISCARD.format(files=", ".join(discarded)))
    if autostart != "unchanged":
        lines.append(messages.SEAT_REMOVE_PLAN_AUTOSTART.format(autostart=autostart))
    lines.append(confirm_text or messages.SEAT_REMOVE_CONFIRM.format(seat=seat))
    return "\n".join(lines)


def _plan(project, seat: str):
    cfg = project.reload_config()
    is_qa = cfg.seats[seat].role == "qa"
    worktree = project.paths.seat_worktree(seat)
    discarded = worktrees.dirty_files(worktree) if (worktree / ".git").exists() and not is_qa else []
    if is_qa:
        from kankan import qa
        owned = qa._owned(project, seat)
        return [], discarded, (owned[0].meta.get("short") or owned[0].meta.get("card")) if owned else None
    return plan_moves(project, seat), discarded, None


def remove(project, seat: str, *, graceful: bool, confirm: Callable[[str], bool], say: Callable[[str], None],
           tmux=None, poll: float = 1.0) -> dict:
    """``kankan seat remove``: the single entry point (seat F§3, T§4–T§5)."""
    cfg = project.reload_config()
    if seat not in cfg.seats:
        raise KankanError(Status.USAGE_ERROR, messages.UNKNOWN_SEAT.format(seat=seat))
    mode = cfg.seats[seat].removing
    if mode == "immediate":  # confirmed earlier and interrupted: completed without a question; autostart again
        return _finish_immediate(project, seat, adjust_autostart(project, seat))
    if mode == "graceful":  # seat F§3.7: the same seat asked again
        running = messages.SEAT_REMOVE_GRACEFUL_RUNNING.format(seat=seat, state=_state_line(project, seat))
        if graceful:
            raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_GRACEFUL_REPEATED.format(
                seat=seat, state=_state_line(project, seat)))
        moves, discarded, conversation = _plan(project, seat)
        question = describe(project, seat, moves, discarded, "unchanged", conversation,
                            confirm_text=messages.SEAT_REMOVE_ESCALATE.format(seat=seat))
        if not confirm(running + "\n" + question):
            return {"cancelled": True, "graceful_running": True, "seat": seat}
        if not _mark(project, seat, "immediate"):
            return _already_removed(project, seat)
        return _finish_immediate(project, seat, "unchanged")
    if graceful:
        return request_graceful(project, seat, say=say, poll=poll, tmux=tmux)
    moves, discarded, conversation = _plan(project, seat)
    preview = adjust_autostart(project, seat, apply=False)
    if (moves or conversation) and not confirm(describe(project, seat, moves, discarded, preview, conversation)):
        return {"cancelled": True, "seat": seat}
    if seat not in project.reload_config().seats:
        return _already_removed(project, seat)
    autostart = adjust_autostart(project, seat)  # before the mark: a fast completion must not skip it
    if not _mark(project, seat, "immediate"):
        return _already_removed(project, seat)
    return _finish_immediate(project, seat, autostart)


def _finish_immediate(project, seat: str, autostart: str) -> dict:
    stop_seat(project, seat, kill_alive=True)
    report = dict(complete(project, seat), autostart=autostart)
    if autostart.startswith("error: "):
        raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_AUTOSTART_FAILED.format(
            seat=seat, error=autostart[len("error: "):]), report)
    return report


def report_text(report: dict, cfg) -> str:
    if report.get("cancelled"):
        return messages.SEAT_REMOVE_CANCELLED.format(seat=report["seat"])
    lines = [f"{report['seat']} removed"]
    for move in report.get("moved") or []:
        lines.append(f"  {move['short'] or move['card']} → {_list_name(cfg, move['to'])}")
    if report.get("discarded"):
        lines.append(f"  uncommitted changes discarded: {', '.join(report['discarded'])}")
    if report.get("lists_archived"):
        lines.append(f"  lists archived: {report['lists_archived']}")
    if report.get("autostart") and report["autostart"] != "unchanged":
        lines.append(f"  autostart: {report['autostart']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- graceful removal (seat T§5)

def has_work(project, seat: str) -> bool:
    """A work seat holds an ``impl``/``review`` ref; the Q&A seat has a pending answer or a turn running."""
    if seats.is_qa(seat):
        from kankan import qa
        for value in qa._owned(project, seat):
            conversation = value.meta.get("conversation")
            if conversation and (qa.conversation_dir(project.paths, conversation) / "pending.json").exists():
                return True
        return load_state(project.paths, seat).launcher_state == "working"
    return bool(seats.owned_cards(project, seat))


def _state_line(project, seat: str) -> str:
    state = load_state(project.paths, seat)
    if seats.liveness(project, seat) != "alive":
        return messages.SEAT_REMOVE_STATE_STOPPED
    task = state.task
    work = f"{task.get('kind', 'task')} {task.get('short', task.get('card', ''))}" if task else "no task"
    return f"{state.launcher_state}, {work}"


def await_start(project, seat: str, session: str) -> None:
    """Wait until a restarted seat occupies its seat, or its removal completed meanwhile."""
    deadline = clock.monotonic() + agents_cmd.UP_WAIT_SECONDS
    while clock.monotonic() < deadline:
        if seat not in project.reload_config().seats or seats.liveness(project, seat) == "alive":
            return
        clock.sleep(0.3)
    raise KankanError(Status.NEEDS_ACTION, messages.UP_NOT_STARTED.format(what=seat, session=session,
                                                                         seconds=agents_cmd.UP_WAIT_SECONDS))


def request_graceful(project, seat: str, *, say: Callable[[str], None], poll: float = 1.0, tmux=None) -> dict:
    """seat T§5 steps 1–4: refuse another kankan, mark the seat, adjust autostart, restart or complete, wait."""
    from kankan import stopall
    if seats.liveness(project, seat) == "alive":
        stopall.refuse_other_code(project, [seat], messages.SEAT_REMOVE_OTHER_CODE.replace("<seat>", seat))
    autostart = adjust_autostart(project, seat)  # before the mark: a fast completion must not skip it
    if not _mark(project, seat, "graceful"):
        return dict(_already_removed(project, seat), autostart=autostart)
    if autostart != "unchanged":
        say(messages.SEAT_REMOVE_PLAN_AUTOSTART.strip().format(autostart=autostart))
    try:
        agents_cmd.resume_removals(project, tmux=tmux, only=seat, strict=True)
    except KankanError as err:
        raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_START_FAILED.format(seat=seat, error=err.message),
                          err.data) from err
    if seat in project.reload_config().seats:
        say(messages.SEAT_REMOVE_WAITING.format(seat=seat))
        report = _wait(project, seat, say, poll)
    else:
        report = last_report(project, seat) or {"seat": seat}
    report = dict(report, autostart=autostart)
    if autostart.startswith("error: "):
        raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_AUTOSTART_FAILED.format(
            seat=seat, error=autostart[len("error: "):]), report)
    return report


def _wait(project, seat: str, say: Callable[[str], None], poll: float, start_grace: float = None) -> dict:
    """Show the seat's state until its removal completed; ``needs_action`` when it failed, when the agent stopped
    with work left (after a start grace), or on Ctrl-C (the removal stays requested)."""
    grace = agents_cmd.UP_WAIT_SECONDS if start_grace is None else start_grace
    started = clock.monotonic()
    shown = None
    try:
        while True:
            cfg = project.reload_config()
            if seat not in cfg.seats:
                return last_report(project, seat) or {"seat": seat}
            if cfg.seats[seat].remove_error:
                raise KankanError(Status.NEEDS_ACTION, messages.STATUS_REMOVE_ERROR.format(
                    error=cfg.seats[seat].remove_error, seat=seat))
            if seats.liveness(project, seat) != "alive" and clock.monotonic() - started >= grace:
                if has_work(project, seat):
                    raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_STOPPED_EARLY.format(seat=seat))
                return complete(project, seat)  # the launcher left without completing (or was killed)
            line = f"  {seat}: {_state_line(project, seat)}"
            if line != shown:
                shown = line
                say(line)
            clock.sleep(poll)
    except KeyboardInterrupt:
        say("")
        raise KankanError(Status.NEEDS_ACTION, messages.SEAT_REMOVE_WAIT_INTERRUPTED.format(seat=seat)) from None
