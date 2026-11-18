"""Q&A seat policy (questions spec T§2–T§6): the Q&A record, the conversation directories and their lock, the
candidates and what is new, eligibility, claim, take-over, turns and the end of a conversation."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from kankan import attachments, auditlog, clock, deps, faults, gitutil, messages, notify, worktrees
from kankan.cards import CardMeta, Phase, parse_refs
from kankan.config import Paths, atomic_write_json, project_lock, read_json
from kankan.errors import KankanError, Status
from kankan.refstore import CARD_PREFIX, Create, Delete, RefValue, Update, Verify, card_ref, seat_ref
from kankan.seatstate import load_state, update_state
from kankan.trello import MARKER_PREFIX, Card, Comment, Snapshot

# A conversation ID is lowercase hex (e.g. ``uuid.uuid4().hex[:16]``): it names a directory and is part of
# the comment markers ``kankan:qa-<conversation>-<turn>-<i>``, so it never contains a separator.
_CONVERSATION = re.compile(r"^[0-9a-f]{8,64}$")


# ---------------------------------------------------------------------- Q&A record (T§2)

def _record_path(paths: Paths) -> Path:
    return paths.kankan_dir / "qa.json"


def recorded(paths: Paths) -> Set[str]:
    """IDs of the cards that have been Q&A cards and were not claimed for implementation since."""
    data = read_json(_record_path(paths), None) or {}
    return set(data.get("cards", []))


def _write(paths: Paths, cards: Set[str]) -> None:
    atomic_write_json(_record_path(paths), {"cards": sorted(cards)})


def record_add(paths: Paths, card_id: str) -> None:
    """Add ``card_id`` to ``qa.json``. The caller holds the project lock."""
    cards = recorded(paths)
    if card_id not in cards:
        _write(paths, cards | {card_id})


def record_remove(paths: Paths, card_id: str) -> None:
    """Remove ``card_id`` from ``qa.json`` (nothing when absent). The caller holds the project lock."""
    cards = recorded(paths)
    if card_id in cards:
        _write(paths, cards - {card_id})


# ---------------------------------------------------------------------- conversation directories (T§5)

def valid_conversation(conversation) -> bool:
    return isinstance(conversation, str) and _CONVERSATION.fullmatch(conversation) is not None


def _root(paths: Paths) -> Path:
    return paths.kankan_dir / "qa"


def conversation_dir(paths: Paths, conversation: str) -> Path:
    """``<gcd>/kankan/qa/<conversation>``: ``op.lock``, ``turn.json`` and ``pending.json``."""
    if not valid_conversation(conversation):
        raise KankanError(Status.INTERNAL_ERROR, messages.QA_INVALID_CONVERSATION.format(conversation=conversation))
    return _root(paths) / conversation


class LockBusy(KankanError):
    """The conversation lock was not taken in time: another operation of the conversation holds it."""

    def __init__(self, path: Path):
        super().__init__(Status.INTERNAL_ERROR, messages.QA_LOCK_TIMEOUT.format(path=path))


CONVERSATION_LOCK_TIMEOUT = 60  # seconds a turn start or an end waits for a conversation operation
_HELD: Dict[Tuple[str, int], int] = {}  # (op.lock path, thread ident) -> depth: reentrant within a thread
_HELD_GUARD = threading.Lock()


@contextlib.contextmanager
def conversation_lock(paths: Paths, conversation: str, timeout: float) -> Iterator[None]:
    """The conversation lock (``op.lock``), taken before the project lock; raises ``KankanError`` when not
    taken within ``timeout`` (``LockBusy``). Reentrant within a thread.

    ``discard_conversation`` deletes ``op.lock`` with the directory, so a waiter may obtain the flock of a
    deleted file while another process holds the flock of a re-created one: after every flock the open file
    must still be the one at the path, else the waiter starts again on the current file (re-creating it).
    """
    path = conversation_dir(paths, conversation) / "op.lock"
    key = (str(path), threading.get_ident())
    with _HELD_GUARD:
        reentered = key in _HELD
        if reentered:
            _HELD[key] += 1
    if reentered:
        try:
            yield
        finally:
            with _HELD_GUARD:
                _HELD[key] -= 1
        return
    fd = _flock_current(path, clock.monotonic() + timeout)
    try:
        with _HELD_GUARD:
            _HELD[key] = 1
        try:
            yield
        finally:
            with _HELD_GUARD:
                del _HELD[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _flock_current(path: Path, deadline: float) -> int:
    """An fd of the file now at ``path``, exclusively flocked."""
    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except FileNotFoundError:  # the directory was deleted between mkdir and open
            if clock.monotonic() >= deadline:
                raise LockBusy(path)
            time.sleep(0.05)
            continue
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if clock.monotonic() >= deadline:
                        raise LockBusy(path)
                    time.sleep(0.05)
            try:
                current = os.stat(path).st_ino
            except FileNotFoundError:
                current = None
            if current == os.fstat(fd).st_ino:
                return fd
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)


def conversations(paths: Paths) -> List[str]:
    """The names of the directories under ``<gcd>/kankan/qa`` (none when it is missing)."""
    try:
        return sorted(name for name in os.listdir(_root(paths)) if (_root(paths) / name).is_dir())
    except FileNotFoundError:
        return []


def discard_conversation(paths: Paths, conversation: str) -> bool:
    """Delete a conversation directory without waiting: True when it is gone, False (and a log entry) when
    the ID is invalid, the conversation lock is busy, or the deletion fails.

    Callers may hold the project lock: the conversation lock is only tried (timeout 0), so the lock order
    (conversation lock, then project lock) is never waited on in reverse. The lock file is deleted with the
    directory: the conversation is over, and a later writer recreates what it needs.
    """
    if not valid_conversation(conversation):
        auditlog.append(paths, "qa_discard_invalid", conversation=str(conversation))
        return False
    directory = conversation_dir(paths, conversation)
    if not directory.exists():
        return True
    try:
        with conversation_lock(paths, conversation, 0):
            shutil.rmtree(directory)
        return True
    except KankanError:
        auditlog.append(paths, "qa_discard_busy", conversation=conversation)
    except OSError as err:
        auditlog.append(paths, "qa_discard_failed", conversation=conversation, error=str(err))
    return False


def read_turn(paths: Paths, conversation: str) -> Optional[dict]:
    """The current turn of a conversation (``turn.json``), or None before the first turn."""
    return read_json(conversation_dir(paths, conversation) / "turn.json", None)


def new_conversation() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------- candidates and what is new (T§5)

@dataclass
class Inputs:
    """IDs of comments and attachments: what a turn delivers, or the context of a first turn."""
    comments: List[str] = field(default_factory=list)
    attachments: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"comments": list(self.comments), "attachments": list(self.attachments)}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Inputs":
        data = data or {}
        return cls(list(data.get("comments") or []), list(data.get("attachments") or []))


# the marker of an answer or handoff chunk: ``kankan:qa-<conversation>-<turn>-<i>`` or ``kankan:qh-…``;
# groups: kind (``qa``/``qh``), conversation, turn, chunk index
QA_MARKER = re.compile("`" + re.escape(MARKER_PREFIX) + r"(q[ah])-([0-9a-f]{8,64})-([0-9a-f]+)-([0-9]+)`")


def candidates(project, card: Card, comments: List[Comment]) -> Inputs:
    """The items that can reach the agent: comments and attachments by board members other than kankan's
    account (``notify.input_members``), without comments carrying any kankan marker and without dependency
    attachments (``task`` does not list them). ``card`` comes from ``trello.card`` (``attachment_items``)."""
    members = notify.input_members(project)
    marker = f"`{MARKER_PREFIX}"
    return Inputs([c.id for c in sorted(comments, key=lambda c: c.id)
                   if c.member_id in members and marker not in c.text],
                  [a.id for a in card.attachment_items
                   if a.member_id in members and not deps.attachment_short(a.url)])


def _comment_covered(comment_id: str, covered_comment: Optional[str]) -> bool:
    # Trello action IDs grow with time: everything up to the newest covered comment is covered
    return covered_comment is not None and (len(comment_id), comment_id) <= (len(covered_comment), covered_comment)


def uncovered(cands: Inputs, covered: Optional[dict]) -> Inputs:
    covered = covered or {}
    seen = set(covered.get("attachments") or [])
    return Inputs([c for c in cands.comments if not _comment_covered(c, covered.get("comment"))],
                  [a for a in cands.attachments if a not in seen])


def to_deliver(cands: Inputs, covered: Optional[dict], delivered: Inputs) -> Optional[Inputs]:
    """None when every candidate is covered by the ref's ``covered`` or was delivered in this launcher run;
    otherwise every uncovered candidate, so the inputs of a failed turn come again with the follow-up."""
    pending = uncovered(cands, covered)
    if any(c not in delivered.comments for c in pending.comments) or \
            any(a not in delivered.attachments for a in pending.attachments):
        return pending
    return None


def newest(comment_ids: List[str]) -> Optional[str]:
    """The newest of ``comment_ids`` (Trello action IDs grow with time), None when empty."""
    return max(comment_ids, key=lambda c: (len(c), c)) if comment_ids else None


def coverage(previous: Optional[dict], inputs: Inputs, context: Optional[Inputs] = None) -> dict:
    """The ``covered`` value after an answer: the newest comment among the turn's inputs, its context and the
    previous ``covered``, and the union of the attachments."""
    previous = previous or {}
    context = context or Inputs()
    comments = list(inputs.comments) + list(context.comments)
    if previous.get("comment"):
        comments.append(previous["comment"])
    seen = set(previous.get("attachments") or []) | set(inputs.attachments) | set(context.attachments)
    return {"comment": newest(comments), "attachments": sorted(seen)}


def effective_covered(ref_covered: Optional[dict], turn: Optional[dict]) -> Optional[dict]:
    """What ``to_deliver`` treats as covered: the ref's ``covered`` once an answer completed; before that the
    first turn's context saved in ``turn.json`` (the old thread of a re-taken card), else None."""
    if ref_covered:
        return ref_covered
    if turn and turn.get("context") is not None:
        return coverage(None, Inputs(), Inputs.from_dict(turn["context"]))
    return None


def _when(value: str) -> Optional[datetime]:
    """A Trello date (``2026-11-17T10:00:00.000Z``) as an aware datetime, None when unreadable."""
    try:
        result = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)


def first_turn(project, card: Card, comments: List[Comment]) -> Tuple[Inputs, Inputs]:
    """``(inputs, context)`` of a new conversation's first turn. When an earlier conversation answered the card
    (a comment by kankan's account carrying an answer or handoff marker), the inputs are the candidates dated
    after the latest such comment by Trello's dates, never the local clock (an unreadable date counts as
    after), and the others are context: the newest such comment (it covers the older ones) and every such
    attachment; otherwise every candidate is an input."""
    cands = candidates(project, card, comments)
    me = notify.kankan_member_id(project)
    answers = [c for c in comments if c.member_id == me and QA_MARKER.search(c.text)]
    if not answers:
        return cands, Inputs()
    floor = datetime.min.replace(tzinfo=timezone.utc)
    latest = max(answers, key=lambda c: (_when(c.date) or floor, c.id))
    latest_when = _when(latest.date)
    if latest_when is None:
        return cands, Inputs()
    by_id = {c.id: c for c in comments}
    attachments_by_id = {a.id: a for a in card.attachment_items}

    def comment_new(comment_id: str) -> bool:
        when = _when(by_id[comment_id].date)
        return when is None or (when, comment_id) > (latest_when, latest.id)

    def attachment_new(attachment_id: str) -> bool:
        when = _when(attachments_by_id[attachment_id].date)
        return when is None or when > latest_when

    inputs = Inputs([c for c in cands.comments if comment_new(c)], [a for a in cands.attachments if attachment_new(a)])
    newest_old = newest([c for c in cands.comments if c not in inputs.comments])
    context = Inputs([newest_old] if newest_old else [], [a for a in cands.attachments if a not in inputs.attachments])
    return inputs, context


# ---------------------------------------------------------------------- eligibility, claim, take-over (T§2–T§3)

def _claimable(value: Optional[RefValue]) -> bool:
    """No ref, or a ``parked`` one whose move is done (an unfinished handoff still has ``from_list``)."""
    if value is None:
        return True
    return value.meta.get("phase") == Phase.PARKED.value and not value.meta.get("from_list")


def eligible(project, snap: Snapshot, refs: Dict[str, RefValue]) -> Optional[Card]:
    """The top open card of ``Q&A`` without ``HOLD`` whose ref is absent or a finished ``parked``; card links
    (dependencies) are ignored (questions F§2). A card whose ref is malformed (left out of ``refs`` by
    ``workflow.card_refs``, or given as a ``KankanError``) is skipped: ``reconcile`` reports it, and the cards
    below it are not kept waiting."""
    cfg = project.cfg
    if not cfg.qa_list:
        return None
    malformed = {ref[len(CARD_PREFIX):] for ref, value in parse_refs(project).items()
                 if not isinstance(value, RefValue)}
    for card in sorted(snap.cards.values(), key=lambda c: c.pos):
        value = refs.get(card.id)
        if card.list_id == cfg.qa_list and not card.closed and cfg.label_hold not in card.label_ids \
                and card.id not in malformed and not isinstance(value, KankanError) and _claimable(value):
            return card
    return None


def card_state(project, card_id: str) -> Optional[Card]:
    """One card read (C1, with the attachment fields); None when the card was deleted."""
    return project.trello.card(card_id)


def active_ended(cfg, card: Optional[Card]) -> bool:
    """The conversation of ``card`` is over: deleted, archived, out of ``Q&A`` or on ``HOLD`` (questions F§3.6)."""
    return card is None or card.closed or not cfg.qa_list or card.list_id != cfg.qa_list \
        or cfg.label_hold in card.label_ids


def ours(value: Optional[RefValue], seat: str, conversation: Optional[str]) -> bool:
    return value is not None and value.meta.get("phase") == Phase.QA.value and value.meta.get("owner") == seat \
        and conversation is not None and value.meta.get("conversation") == conversation


def handed_off(value: Optional[RefValue], conversation: Optional[str], posted: Optional[list] = None) -> bool:
    """The handoff transition of ``conversation`` happened: the ref is ``parked`` by a handoff carrying its summary
    comments, or since claimed for implementation (``impl``). With ``posted``, the summary must be those comments."""
    return value is not None and value.meta.get("phase") in (Phase.PARKED.value, Phase.IMPL.value) \
        and value.meta.get("cause", "handoff") == "handoff" \
        and conversation is not None and value.meta.get("conversation") == conversation \
        and bool(value.meta.get("handoff")) and (posted is None or value.meta.get("handoff") == posted)


def check_seat(project, lease) -> None:
    current = project.refs.read(seat_ref(lease.seat))
    if current is None or current.oid != lease.seat_oid:
        raise KankanError(Status.NOT_OWNER, messages.SEAT_NOT_HELD.format(seat=lease.seat))


def _integration_tip(project) -> str:
    tip = gitutil.rev(project.paths.repo, f"refs/heads/{project.cfg.integration_branch}")
    if tip is None:
        raise KankanError(Status.INTERNAL_ERROR,
                          messages.INTEGRATION_BRANCH_MISSING.format(branch=project.cfg.integration_branch))
    return tip


def _store(project, lease, card_id: str, short: str, conversation: str, pair, base: str, reflog: int,
           turn: Optional[str] = None) -> dict:
    task = {"id": uuid.uuid4().hex[:12], "kind": "qa", "card": card_id, "short": short,
            "conversation": conversation, "turn": turn, "role": "questions",
            "model": pair.model if pair else None, "effort": pair.effort if pair else None,
            "base": base, "reflog": reflog, "ended": False}

    def store(state):
        state.task = task
        state.signal = None
    update_state(project.paths, lease.seat, store)
    project.log("task_assigned", seat=lease.seat, **task)
    return task


def claim(project, lease, card: Card, pair) -> Optional[dict]:
    """Claim a ``Q&A`` card for the seat (none/finished ``parked`` → ``qa``) and store the seat task.

    Under the project lock: one card read (still in ``Q&A``, open, no ``HOLD``), the ``qa.json`` entry, then
    one transaction verifying the seat ref; a ``parked`` ref's older conversation directory is deleted, its
    ``branch`` stays out of the new blob and the branch itself is untouched. After the lock: the conversation
    directory and the worktree reset to the integration tip. Returns None when the card is no longer
    claimable; raises ``NOT_OWNER`` when the seat is lost.
    """
    cfg = project.cfg
    ref = card_ref(card.id)
    conversation = new_conversation()
    with project_lock(project.paths):
        fresh = project.trello.card(card.id)
        if active_ended(cfg, fresh):
            project.log("qa_claim_skipped", card=card.id, reason="card left Q&A, closed or on HOLD")
            return None
        try:
            current = project.refs.read(ref)
        except KankanError as err:  # a malformed ref: reconcile reports it
            project.log("qa_claim_skipped", card=card.id, reason=err.message)
            return None
        if not _claimable(current):
            project.log("qa_claim_skipped", card=card.id, reason=f"ref in phase {current.meta.get('phase')}")
            return None
        meta = CardMeta(phase=Phase.QA.value, card=card.id, short=fresh.short or card.short, owner=lease.seat,
                        session=lease.session, target_list=cfg.qa_list, conversation=conversation)
        oid = project.refs.write_blob(meta.to_dict())
        record_add(project.paths, card.id)  # T§2: before the claim's transaction
        ops = [Verify(seat_ref(lease.seat), lease.seat_oid),
               Update(ref, oid, current.oid) if current is not None else Create(ref, oid)]
        if not project.refs.transaction(ops):
            check_seat(project, lease)
            project.log("qa_claim_skipped", card=card.id, reason="ref changed")
            return None
        older = current.meta.get("conversation") if current is not None else None
        if older:
            discard_conversation(project.paths, older)
        project.log("qa_claimed", seat=lease.seat, card=card.id, conversation=conversation,
                    old=current.oid if current else None, new=oid)
    with conversation_lock(project.paths, conversation, CONVERSATION_LOCK_TIMEOUT):
        pass  # creates the conversation directory, now that the ref names it
    base, reflog = worktrees.reset_detached(project.paths.seat_worktree(lease.seat), _integration_tip(project))
    return _store(project, lease, card.id, meta.short, conversation, pair, base, reflog)


def take_over(project, lease, value: RefValue, pair) -> dict:
    """Take up the seat's active conversation (``value``, phase ``qa``) after a restart or a seat replacement,
    with a new task ID and ``pair``. The base commit and reflog length of a task of the same conversation are
    kept; without them the seat resets its own worktree first."""
    meta = value.meta
    conversation = meta.get("conversation")
    current = project.refs.read(card_ref(meta["card"]))
    if not ours(current, lease.seat, conversation):
        raise KankanError(Status.NOT_OWNER, messages.QA_ENDED.format(short=meta.get("short", meta["card"])))
    check_seat(project, lease)  # before the worktree reset and the seat task, as claim, start_turn and end do
    old = load_state(project.paths, lease.seat).task or {}
    same = old.get("kind") == "qa" and old.get("card") == meta["card"] and old.get("conversation") == conversation
    if same and old.get("base"):
        base, reflog = old["base"], old.get("reflog", 0)
    else:
        base, reflog = worktrees.reset_detached(project.paths.seat_worktree(lease.seat), _integration_tip(project))
    return _store(project, lease, meta["card"], meta.get("short", ""), conversation, pair, base, reflog,
                  turn=old.get("turn") if same else None)


# ---------------------------------------------------------------------- turns (T§4–T§5)

def drop_orphan(project, card_id: str, conversation: str) -> None:
    """Delete a conversation directory re-created by a lock taken after the end (no ref names it)."""
    current = project.refs.read(card_ref(card_id))
    if current is None or current.meta.get("conversation") != conversation:
        discard_conversation(project.paths, conversation)


def start_turn(project, lease, task: dict, inputs: Inputs, context: Optional[Inputs] = None) -> str:
    """Start a turn: under the conversation lock, re-validate the ref and the seat, write ``turn.json`` (token,
    inputs, the first turn's context, the launcher run), then the seat task's ``turn``. The caller writes the
    prompt only afterwards. Raises ``NOT_OWNER`` when the conversation ended or the task was replaced."""
    conversation = task["conversation"]
    ended = KankanError(Status.NOT_OWNER, messages.QA_ENDED.format(short=task.get("short") or task["card"]))
    if not ours(project.refs.read(card_ref(task["card"])), lease.seat, conversation):
        raise ended  # checked before the lock too, so an ended conversation's directory is not re-created
    token = uuid.uuid4().hex[:16]
    with conversation_lock(project.paths, conversation, CONVERSATION_LOCK_TIMEOUT):
        value = project.refs.read(card_ref(task["card"]))
        if not ours(value, lease.seat, conversation):
            drop_orphan(project, task["card"], conversation)
            raise ended
        check_seat(project, lease)
        data = {"token": token, "inputs": inputs.to_dict(), "run": task["id"]}
        if context is None and not value.meta.get("covered"):
            # no answer yet: the first turn's context stays uncovered context until an answer covers it
            previous = (read_turn(project.paths, conversation) or {}).get("context")
            context = Inputs.from_dict(previous) if previous is not None else None
        if context is not None:
            data["context"] = context.to_dict()
        atomic_write_json(conversation_dir(project.paths, conversation) / "turn.json", data)
        faults.point("qa_after_turn_saved")

        def set_turn(state) -> bool:
            if state.task and state.task.get("id") == task["id"] and not state.task.get("ended"):
                state.task["turn"] = token
                return True
            return False
        if not update_state(project.paths, lease.seat, set_turn):
            raise ended
    project.log("qa_turn", seat=lease.seat, card=task["card"], conversation=conversation, turn=token,
                comments=len(inputs.comments), attachments=len(inputs.attachments))
    return token


# ---------------------------------------------------------------------- handoff (T§3, T§6)

def handoff_transition(project, lease, task: dict, comment_ids: List[str]) -> None:
    """``qa`` → ``parked`` (``cause: handoff``) and the move to the bottom of ``Backlog``, in one project lock
    hold: one card read (still in ``Q&A``, open, no ``HOLD``, else ``NOT_OWNER``), the blob with ``from_list``
    and ``pos: bottom``, the transaction verifying the seat ref and the ``qa`` ref, the move with its
    verification read, then ``cards.confirm_move``. A crash or a failed move after the transaction is completed
    by convergence (``converge_ref``), which never replays a move once confirmed; ``eligible`` skips the ref
    while ``from_list`` is set. The ``qa.json`` entry and the card's directories stay."""
    from kankan.cards import confirm_move
    cfg = project.cfg
    ref = card_ref(task["card"])
    conversation = task.get("conversation")
    ended = KankanError(Status.NOT_OWNER, messages.QA_ENDED.format(short=task.get("short") or task["card"]))
    with project_lock(project.paths):
        card = project.trello.card(task["card"])
        if active_ended(cfg, card):
            raise ended
        value = project.refs.read(ref)
        if not ours(value, lease.seat, conversation):
            raise ended
        meta = CardMeta.from_dict(value.meta).evolve(
            phase=Phase.PARKED.value, owner=None, session=None, cause="handoff", target_list=cfg.backlog,
            pos="bottom", from_list=cfg.qa_list, handoff=list(comment_ids), covered=None)
        oid = project.refs.write_blob(meta.to_dict())
        if not project.refs.transaction([Verify(seat_ref(lease.seat), lease.seat_oid), Update(ref, oid, value.oid)]):
            check_seat(project, lease)
            raise ended
        project.log("qa_handoff", seat=lease.seat, card=task["card"], conversation=conversation,
                    old=value.oid, new=oid, comments=list(comment_ids))
        faults.point("qa_after_handoff_tx")
        try:
            project.trello.move_card(task["card"], cfg.backlog, pos="bottom")
        except KankanError as err:  # convergence completes the move
            project.log("move_failed", card=task["card"], target=cfg.backlog, error=err.message)
            return
        faults.point("qa_after_handoff_move")
        confirm_move(project, ref, project.refs.read_blob(oid))


# ---------------------------------------------------------------------- end (T§3)

def _discard_card_files(project, short: str, conversation: Optional[str]) -> None:
    """The attachment and conversation directories of an ended conversation (best effort, under the project
    lock with the ref gone; the attachment and conversation locks are only tried)."""
    if short:
        attachments.discard(project.paths, short)
    if conversation:
        discard_conversation(project.paths, conversation)


def _mark_ended(project, seat: str, match) -> None:
    def mark(state):
        if state.task and match(state.task):
            state.task["ended"] = True
    update_state(project.paths, seat, mark)


def end(project, lease, task: dict, *, handed_off: bool = False) -> bool:
    """End the seat's conversation: delete its ``qa`` ref in a transaction verifying the seat ref (so a command
    of a turn still running gets ``not_owner``), delete the card's attachment directory and the conversation
    directory, and mark the seat task ``ended``. After ``handoff`` (``handed_off``) the ref is already
    ``parked`` and both directories stay. True when this call ended it; False when the ref was already gone
    or belongs to another seat or conversation (nothing of theirs is touched)."""
    conversation = task.get("conversation")
    ref = card_ref(task["card"])
    ended = handed_off
    with project_lock(project.paths):
        if not handed_off:
            value = project.refs.read(ref)
            if ours(value, lease.seat, conversation):
                if project.refs.transaction([Verify(seat_ref(lease.seat), lease.seat_oid), Delete(ref, value.oid)]):
                    ended, value = True, None
                else:
                    check_seat(project, lease)
                    value = project.refs.read(ref)
            if value is None:
                _discard_card_files(project, task.get("short", ""), conversation)
        _mark_ended(project, lease.seat, lambda current: current.get("id") == task["id"])
    project.log("qa_ended", seat=lease.seat, card=task["card"], conversation=conversation, ended=ended,
                handed_off=handed_off)
    return ended


def _owned(project, seat: str) -> List[RefValue]:
    """The well-formed ``qa`` refs owned by ``seat``."""
    return [value for value in parse_refs(project).values() if isinstance(value, RefValue)
            and value.meta.get("phase") == Phase.QA.value and value.meta.get("owner") == seat]


def _read_owned(project, card_id: str, seat: str) -> Optional[RefValue]:
    try:
        value = project.refs.read(card_ref(card_id))
    except KankanError:  # malformed meanwhile: reconcile reports it
        return None
    if value is None or value.meta.get("phase") != Phase.QA.value or value.meta.get("owner") != seat:
        return None
    return value


def end_admin(project, seat: str) -> bool:
    """End the conversations held by a free seat (``seat remove``): for each ``qa`` ref it owns, one project lock
    hold deletes the ref in a transaction verifying that the seat has no seat ref, then the card's directories.
    Nothing is ended while the seat ref exists (the seat runs or is dead: ``seat recover`` frees it)."""
    ended = False
    for listed in _owned(project, seat):
        card_id = listed.meta["card"]
        with project_lock(project.paths):
            value = _read_owned(project, card_id, seat)
            if value is None or not project.refs.transaction([Verify(seat_ref(seat), None),
                                                              Delete(card_ref(card_id), value.oid)]):
                continue
            ended = True
            _discard_card_files(project, value.meta.get("short", ""), value.meta.get("conversation"))
            _mark_ended(project, seat, lambda current, card_id=card_id: current.get("card") == card_id)
            project.log("qa_ended_admin", seat=seat, card=card_id, conversation=value.meta.get("conversation"))
    return ended


# ---------------------------------------------------------------------- seat replacement (T§2)

def _check_replaceable(project, old: str, new: str, recover: bool = False) -> None:
    """Under the project lock: ``old`` neither runs nor leaves surviving processes (``NEEDS_ACTION`` otherwise);
    with ``recover``, the leftovers of a dead seat are terminated first."""
    from kankan import seats
    state = seats.liveness(project, old)  # the project lock is reentrant
    if state == "dead" and recover and seats.recover_dead(project, old):
        state = "unavailable"
    if state in ("alive", "unavailable"):
        raise KankanError(Status.NEEDS_ACTION, messages.QA_REPLACE_RUNNING.format(old=old, new=new, state=state),
                          {"seat": old, "liveness": state})


def lifecycle_lock(paths: Paths):
    """Serializes the Q&A seat replacements and ``seat remove <type>-qa`` (``qa-lifecycle.lock``). Lock order:
    ``up.lock`` → lifecycle lock → conversation lock → project lock → seat-state lock. ``kankan up`` holds ``up.lock``
    while it registers the Q&A seat (it cannot take the lifecycle lock first: the launcher it starts takes the
    lifecycle lock in ``replace_seat`` while ``up`` waits for it); nothing takes ``up.lock`` while holding the
    lifecycle lock or a lock after it."""
    from kankan.config import file_lock
    return file_lock(paths.kankan_dir / "qa-lifecycle.lock", timeout=CONVERSATION_LOCK_TIMEOUT,
                     what="Q&A lifecycle lock")


def _replacing_path(paths: Paths) -> Path:
    return paths.kankan_dir / "qa-replacing.json"


def _replacing(paths: Paths, old: str, new: str) -> bool:
    """The replacement record names ``old`` replaced by ``new``."""
    data = read_json(_replacing_path(paths), None) or {}
    return data.get("old") == old and data.get("new") == new


def drop_stale_record(project, registered: Optional[str], starting: Optional[str] = None) -> None:
    """Under the project lock: remove the replacement record when the replacement it names can no longer complete:
    its new seat is neither the registered Q&A seat nor, with none registered, the seat ``starting`` now (e.g. a
    crash after stage 2, then ``seat remove <new>-qa``, then a start of the old type)."""
    data = read_json(_replacing_path(project.paths), None)
    if data is None:
        return
    expected = registered if registered is not None else starting
    if data.get("new") != expected:
        _replacing_path(project.paths).unlink(missing_ok=True)
        project.log("qa_replacement_record_dropped", old=data.get("old"), new=data.get("new"), registered=registered)


def _leftovers(project, old: str, new: str) -> bool:
    """Something of the replaced seat ``old`` is left: a conversation or its seat ref, or, while the replacement
    record names ``old`` replaced by ``new``, its worktree or seat files (a plain ``seat remove`` leaves seat files
    too, and is no replacement)."""
    if _owned(project, old) or project.refs.read(seat_ref(old)) is not None:
        return True
    if not _replacing(project.paths, old, new):
        return False
    seats_dir = project.paths.seats_dir
    return (project.paths.seat_worktree(old) / ".git").exists() \
        or (seats_dir.exists() and any(seats_dir.glob(f"{old}.*")))


def _race(old: str) -> KankanError:
    return KankanError(Status.INELIGIBLE, messages.QA_REPLACE_RACE.format(old=old))


def _check_destination(project, old: str, new: str) -> None:
    """Under the project lock: ``new`` is still the registered Q&A seat (a stage never acts for another one)."""
    if project.reload_config().qa_seat() != new:
        raise _race(old)


def _carry_label_causes(project, old: str, new: str) -> None:
    """The label causes ``old`` recorded for the cards whose conversations ``new`` now holds go to ``new``, so a
    follow-up still clears an ``Error`` of ``old``'s ``fail`` (questions F§3.4)."""
    causes = load_state(project.paths, old).label_causes
    moved = {value.meta["card"] for value in _owned(project, new)}
    carried = {card_id: entries for card_id, entries in causes.items() if card_id in moved}
    if not carried:
        return

    def carry(state):
        for card_id, entries in carried.items():
            for label_id, entry in entries.items():
                state.label_causes.setdefault(card_id, {}).setdefault(label_id, entry)
    update_state(project.paths, new, carry)


def _complete_replacement(project, old: str, new: str) -> None:
    """Stages 3–5 of a replacement, each idempotent and in its own project lock hold, each checking that ``new`` is
    still the registered Q&A seat: every ``qa`` ref of ``old`` gets ``new`` as owner (one transaction per hold,
    verifying the ref and ``old``'s seat ref as read), ``old``'s seat ref is deleted, then ``old``'s label causes for
    the moved cards go to ``new`` and its worktree (if clean), its seat files and the replacement record are
    removed. The conversation directories are keyed by conversation and stay as they are."""
    for listed in _owned(project, old):
        card_id = listed.meta["card"]
        with project_lock(project.paths):
            _check_destination(project, old, new)
            _check_replaceable(project, old, new)
            value = _read_owned(project, card_id, old)
            if value is None:
                continue
            old_seat = project.refs.read(seat_ref(old))
            meta = CardMeta.from_dict(value.meta).evolve(owner=new, session=None)
            oid = project.refs.write_blob(meta.to_dict())
            if not project.refs.transaction([Verify(seat_ref(old), old_seat.oid if old_seat else None),
                                             Update(card_ref(card_id), oid, value.oid)]):
                raise _race(old)
            project.log("qa_replaced_owner", card=card_id, conversation=value.meta.get("conversation"), old=old,
                        new=new, old_oid=value.oid, new_oid=oid)
        faults.point("qa_replace_ref")
    with project_lock(project.paths):
        _check_destination(project, old, new)
        _check_replaceable(project, old, new)
        old_seat = project.refs.read(seat_ref(old))
        if old_seat is not None:
            if _owned(project, old) or not project.refs.delete(seat_ref(old), old_seat.oid):
                raise _race(old)
            project.log("qa_replaced_seat_ref", seat=old, old=old_seat.oid)
            faults.point("qa_replace_seatref")
    with project_lock(project.paths):
        _check_destination(project, old, new)
        _check_replaceable(project, old, new)
        _carry_label_causes(project, old, new)
        worktree = project.paths.seat_worktree(old)
        removed = worktrees.remove_if_clean(project.paths.repo, worktree)
        if not removed and (worktree / ".git").exists():
            project.log("qa_replaced_worktree_kept", seat=old, worktree=str(worktree))
        if project.paths.seats_dir.exists():
            for path in project.paths.seats_dir.glob(f"{old}.*"):
                path.unlink(missing_ok=True)
        if _replacing(project.paths, old, new):
            _replacing_path(project.paths).unlink(missing_ok=True)
        project.log("qa_seat_replaced", seat=old, by=new, worktree_removed=removed)


def replace_seat(project, agent_type: str, *, initiate: bool = True) -> Optional[str]:
    """Make ``<agent_type>-qa`` the Q&A seat when another type's Q&A seat is registered or left something behind
    (questions T§2). Serialized by the lifecycle lock.

    Stages, each idempotent: (1) the registered seat must be ``free`` or ``dead`` with no surviving process
    (``NEEDS_ACTION`` otherwise); (2) the replacement record is written and the configuration entry is swapped for
    the new seat's in one write, so at most one Q&A seat is ever registered and the old seat can no longer start
    (``seats.acquire_qa`` refuses it, which an old supervisor treats as fatal); (3)–(5) ``_complete_replacement``.
    When no Q&A seat is registered (the new seat of an unfinished replacement was removed), the new seat registers
    itself in stage 2 if another seat left a conversation or a seat ref behind, so its stages still complete.
    ``agent --qa`` and ``up qa=<type>`` run every stage; the new seat's launcher runs it with ``initiate=False``
    before occupying its seat, completing only a replacement already registered (so a relaunched old seat never
    replaces the new one back). Returns the replaced seat's name, or None when there was nothing to replace or
    complete.
    """
    from kankan import seats
    from kankan.config import SeatConfig, save_config
    new = seats.qa_seat_name(agent_type)
    olds = [seats.qa_seat_name(other) for other in seats.AGENT_TYPES if seats.qa_seat_name(other) != new]
    replaced = None
    with lifecycle_lock(project.paths):
        with project_lock(project.paths):
            cfg = project.reload_config()
            registered = cfg.qa_seat()
            drop_stale_record(project, registered, new)
            if registered is not None and registered != new and not initiate:
                return None
            if registered is None and not any(_leftovers(project, old, new) for old in olds):
                return None
            if registered != new:
                if registered is not None:
                    _check_replaceable(project, registered, new, recover=True)
                    atomic_write_json(_replacing_path(project.paths), {"old": registered, "new": new})
                    del cfg.seats[registered]
                cfg.seats[new] = SeatConfig(type=agent_type, role="qa")
                save_config(project.paths, cfg)
                project.log("qa_seat_registered", seat=new, replaces=registered)
                replaced = registered
        if replaced:
            faults.point("qa_replace_registered")
        for old in olds:
            if _leftovers(project, old, new):
                _complete_replacement(project, old, new)
                replaced = old
    return replaced
