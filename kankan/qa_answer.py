"""Q&A posting (questions T§6): chunking, the pending operation, ``answer``, ``handoff``, the checkout checks and
the completion of a pending operation after a crash.

Lock order: the conversation lock (``op.lock``), then the project lock, then the seat-state lock. The project
lock is never held while posting chunks; each hold is one transaction (``answer``) or ``qa.handoff_transition``
(one card read, one transaction, one verified move).
"""

from __future__ import annotations

import hashlib
import os
from typing import List, Optional, Tuple

from kankan import faults, gitutil, labels, messages, notify, qa, worktrees
from kankan.agentcmds import write_signal
from kankan.cards import CardMeta
from kankan.config import atomic_write_json, project_lock, read_json
from kankan.errors import KankanError, Status
from kankan.refstore import Update, Verify, card_ref, seat_ref
from kankan.seatstate import load_state, update_state
from kankan.trello import COMMENT_MAX, comment_text

# the marker prefix of each kind: ``kankan:qa-<conversation>-<turn>-<i>``, ``kankan:qh-…``
KINDS = {"answer": "qa", "handoff": "qh"}
LOCK_TIMEOUT = qa.CONVERSATION_LOCK_TIMEOUT
# reflog actions that create commits (``git reflog`` subjects such as ``commit (amend): …``)
_COMMIT_ACTIONS = {"commit", "merge", "cherry-pick", "revert", "rebase", "am", "pull"}


# ---------------------------------------------------------------------- chunking

class _TooFewOpIds(Exception):
    """``chunks`` needs more op-ids than it was given."""


def _cut(rest: str, limit: int) -> Tuple[int, int]:
    """Where to split ``rest`` so the head has at most ``limit`` characters: ``(end of head, separator length)``,
    at the last blank line, else the last newline, else a hard cut."""
    for separator in ("\n\n", "\n"):
        index = rest.rfind(separator, 1, limit + len(separator))
        if index > 0:
            return index, len(separator)
    return limit, 0


def chunks(text: str, *, mention: str, op_ids: List[str]) -> List[str]:
    """The comment texts of ``text`` (questions T§6): each ``trello.comment_text(chunk, op_ids[i])`` is at most
    ``COMMENT_MAX`` characters. A text that fits is one comment without a prefix; a longer one gets ``i/m``
    prefixes. Only the last chunk carries ``mention`` (after a blank line). Raises ``USAGE_ERROR`` for an empty
    text and ``_TooFewOpIds`` when ``op_ids`` has fewer entries than the chunks."""
    text = text.rstrip()
    if not text.strip():
        raise KankanError(Status.USAGE_ERROR, messages.QA_EMPTY)
    tail = f"\n\n{mention}" if mention else ""
    if len(comment_text(text + tail, op_ids[0])) <= COMMENT_MAX:
        return [text + tail]
    digits = len(str(len(op_ids)))
    reserve = f"{'9' * digits}/{'9' * digits}\n\n"  # room for the longest prefix ``i/m``
    pieces: List[str] = []
    rest = text
    while True:
        if len(pieces) >= len(op_ids):
            raise _TooFewOpIds()
        op_id = op_ids[len(pieces)]
        last_budget = COMMENT_MAX - len(comment_text(reserve + tail, op_id))
        if len(rest) <= last_budget:
            pieces.append(rest)
            break
        budget = COMMENT_MAX - len(comment_text(reserve, op_id))
        if budget <= 0 or last_budget <= 0:
            raise KankanError(Status.INTERNAL_ERROR, messages.QA_NO_ROOM)
        end, skip = _cut(rest, budget if len(rest) > budget else last_budget)
        pieces.append(rest[:end])
        rest = rest[end + skip:]
    count = len(pieces)
    return [f"{index}/{count}\n\n{piece}" + (tail if index == count else "")
            for index, piece in enumerate(pieces, 1)]


def op_id(kind: str, conversation: str, turn: str, index: int) -> str:
    return f"{KINDS[kind]}-{conversation}-{turn}-{index}"


def _plan(text: str, mention: str, kind: str, conversation: str, turn: str) -> Tuple[List[str], List[str]]:
    """The chunk texts and their op-ids."""
    count = len(text) // 8000 + 2
    while True:
        ops = [op_id(kind, conversation, turn, index) for index in range(1, count + 1)]
        try:
            pieces = chunks(text, mention=mention, op_ids=ops)
        except _TooFewOpIds:
            count *= 2
            continue
        return pieces, ops[:len(pieces)]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------- pending operation (T§6 step 2)

def _pending_path(paths, conversation: str):
    return qa.conversation_dir(paths, conversation) / "pending.json"


def read_pending(paths, conversation: str) -> Optional[dict]:
    """``pending.json``: ``kind``, ``turn``, ``hash``, the turn's ``inputs`` and ``context``, the ``chunks``,
    their ``ops`` and the comment IDs already ``posted``; None when no operation is pending."""
    return read_json(_pending_path(paths, conversation), None)


def _save_pending(paths, pending: dict) -> None:
    atomic_write_json(_pending_path(paths, pending["conversation"]), pending)


def _drop_pending(paths, conversation: str) -> None:
    try:
        os.unlink(_pending_path(paths, conversation))
    except FileNotFoundError:
        pass


def _completed(turn_data: dict, kind: str) -> Optional[dict]:
    """The operation of ``kind`` completed in the current turn (recorded in ``turn.json``), if any."""
    for entry in turn_data.get("completed") or []:
        if entry.get("kind") == kind:
            return entry
    return None


# ---------------------------------------------------------------------- validation (T§6 step 1)

def _ended(task: dict) -> KankanError:
    return KankanError(Status.NOT_OWNER, messages.QA_ENDED.format(short=task.get("short") or task["card"]))


def qa_task(ctx, command: str) -> dict:
    """The seat's task, by the error precedence: ``no_work``, ``usage_error`` (not a Q&A task), ``not_owner``
    (ended, or the ref is not this seat's conversation)."""
    task = ctx.task
    if task.get("kind") != "qa":
        raise KankanError(Status.USAGE_ERROR, messages.QA_ONLY.format(command=command))
    if task.get("ended") or not qa.valid_conversation(task.get("conversation")):
        raise _ended(task)
    if not qa.ours(ctx.project.refs.read(card_ref(task["card"])), ctx.seat, task["conversation"]):
        raise _ended(task)  # checked before the conversation lock, so it is not re-created
    return task


def _validate(ctx, task: dict, turn: str):
    """Under the conversation lock: the ref, the seat and the seat task again, then the turn token, the clean
    checkout and ``HEAD`` at the base commit. Returns ``(ref value, turn.json)``."""
    project = ctx.project
    conversation = task["conversation"]
    value = project.refs.read(card_ref(task["card"]))
    if not qa.ours(value, ctx.seat, conversation):
        qa.drop_orphan(project, task["card"], conversation)
        raise _ended(task)
    current = load_state(project.paths, ctx.seat).task or {}
    if current.get("ended") or current.get("conversation") != conversation or current.get("card") != task["card"]:
        raise _ended(task)
    qa.check_seat(project, ctx.lease)
    turn_data = qa.read_turn(project.paths, conversation) or {}
    if not turn or turn_data.get("token") != turn:
        raise KankanError(Status.NEEDS_ACTION, messages.QA_STALE_TURN.format(turn=turn),
                          {"turn": turn, "current": turn_data.get("token")})
    dirty = worktrees.dirty_files(ctx.worktree)
    if dirty:
        raise KankanError(Status.NEEDS_ACTION, messages.QA_DIRTY.format(files=", ".join(dirty)), {"files": dirty})
    base = current.get("base")
    if base and gitutil.rev(ctx.worktree, "HEAD") != base:
        raise KankanError(Status.NEEDS_ACTION, messages.QA_HEAD_MOVED.format(base=base), {"base": base})
    return value, turn_data


# ---------------------------------------------------------------------- posting (T§6 steps 1–3)

def _post_chunks(project, lease, task: dict, pending: dict, ref_oid: str) -> List[str]:
    """Post the chunks not yet posted, each after one card read (C1): ``NOT_OWNER`` when the card left
    ``Q&A``, is closed or on ``HOLD``, or the ref changed. ``comment_once`` skips a chunk already posted
    before a crash."""
    cfg = project.cfg
    ref = card_ref(task["card"])
    for index, (piece, op) in enumerate(zip(pending["chunks"], pending["ops"])):
        if index < len(pending["posted"]):
            continue
        if qa.active_ended(cfg, qa.card_state(project, task["card"])):
            raise _ended(task)
        current = project.refs.read(ref)
        if current is None or current.oid != ref_oid:
            raise _ended(task)
        qa.check_seat(project, lease)
        comment = project.trello.comment_once(task["card"], piece, op)
        pending["posted"].append(comment.id)
        _save_pending(project.paths, pending)
        faults.point("qa_after_chunk")
    return list(pending["posted"])


def _post(ctx, task: dict, text: str, turn: str, kind: str) -> Tuple[List[str], bool]:
    """Steps 1–3 under the conversation lock (held by the caller). Returns the posted comment IDs and whether
    this operation had already completed in this turn."""
    project = ctx.project
    conversation = task["conversation"]
    value, turn_data = _validate(ctx, task, turn)
    digest = text_hash(text)
    done = _completed(turn_data, kind)
    if done is not None:
        if done.get("hash") != digest:
            raise KankanError(Status.NEEDS_ACTION, messages.QA_ALREADY_POSTED.format(kind=kind))
        return list(done.get("posted") or []), True
    pending = read_pending(project.paths, conversation)
    if pending is not None and pending.get("turn") != turn:
        # an older turn's operation that was never completed: completed first, never dropped (T§6 step 2)
        _complete_locked(project, ctx.lease, task, pending, turn_data)
        value = project.refs.read(card_ref(task["card"]))
        if not qa.ours(value, ctx.seat, conversation):
            raise _ended(task)
        pending = None
    if pending is not None and (pending.get("kind"), pending.get("hash")) != (kind, digest):
        raise KankanError(Status.NEEDS_ACTION, messages.QA_PENDING_OTHER.format(kind=pending.get("kind")))
    if pending is None:
        if not text.strip():
            raise KankanError(Status.USAGE_ERROR, messages.QA_EMPTY)
        pieces, ops = _plan(text, notify.mention(project, task["card"]), kind, conversation, turn)
        pending = {"kind": kind, "turn": turn, "hash": digest, "conversation": conversation,
                   "inputs": turn_data.get("inputs") or {"comments": [], "attachments": []},
                   "context": turn_data.get("context"), "chunks": pieces, "ops": ops, "posted": []}
        _save_pending(project.paths, pending)
    return _post_chunks(project, ctx.lease, task, pending, value.oid), False


def post(ctx, text: str, turn: str, kind: str) -> List[str]:
    """Steps 1–3 of ``answer`` (``kind="answer"``) or ``handoff``: validate, record the pending operation, post
    the chunks not yet posted. Returns the comment IDs of every chunk."""
    task = qa_task(ctx, kind)
    with qa.conversation_lock(ctx.project.paths, task["conversation"], LOCK_TIMEOUT):
        return _post(ctx, task, text, turn, kind)[0]


# ---------------------------------------------------------------------- completion (T§6 step 4)

def _record_completed(project, pending: dict) -> None:
    """Record the operation in ``turn.json`` (a repeated command of this turn finds it), then delete
    ``pending.json``."""
    conversation = pending["conversation"]
    turn_data = qa.read_turn(project.paths, conversation) or {}
    if turn_data.get("token") == pending["turn"]:
        completed = [entry for entry in turn_data.get("completed") or [] if entry.get("kind") != pending["kind"]]
        completed.append({"kind": pending["kind"], "hash": pending["hash"], "posted": pending["posted"]})
        turn_data["completed"] = completed
        atomic_write_json(qa.conversation_dir(project.paths, conversation) / "turn.json", turn_data)
    _drop_pending(project.paths, conversation)


def _finish(project, lease, task: dict, pending: dict) -> None:
    """Step 4 under the project lock (the caller holds the conversation lock, so the turn token is stable):
    ``answer``: the ``covered`` transaction (skipped when already done); ``handoff``: ``qa.handoff_transition``
    (skipped when already done). Then the signal (with ``turn``), the completion record, and, outside the
    project lock, the commit check."""
    ref = card_ref(task["card"])
    with project_lock(project.paths):
        value = project.refs.read(ref)
        if pending["kind"] == "answer":
            if not qa.ours(value, lease.seat, pending["conversation"]):
                raise _ended(task)
            covered = qa.coverage(value.meta.get("covered"), qa.Inputs.from_dict(pending.get("inputs")),
                                  qa.Inputs.from_dict(pending["context"]) if pending.get("context") else None)
            faults.point("qa_before_covered")
            if covered != value.meta.get("covered"):
                oid = project.refs.write_blob(CardMeta.from_dict(value.meta).evolve(covered=covered).to_dict())
                if not project.refs.transaction([Verify(seat_ref(lease.seat), lease.seat_oid),
                                                 Update(ref, oid, value.oid)]):
                    qa.check_seat(project, lease)
                    raise _ended(task)
                project.log("qa_answered", seat=lease.seat, card=task["card"], conversation=pending["conversation"],
                            turn=pending["turn"], covered=covered, old=value.oid, new=oid)
            faults.point("qa_after_covered")
        elif not qa.handed_off(value, pending["conversation"], pending["posted"]):
            qa.handoff_transition(project, lease, task, pending["posted"])
        if (qa.read_turn(project.paths, pending["conversation"]) or {}).get("token") == pending["turn"]:
            # only the current turn is signalled: an older turn is over, its signal would replace a newer one
            write_signal(project, lease.seat, task["id"], pending["kind"],
                         {"turn": pending["turn"], "comments": list(pending["posted"])})
        _record_completed(project, pending)
    _commit_check(project, lease, task, pending["kind"])


def _commit_check(project, lease, task: dict, kind: str = "answer") -> None:
    """When the ``HEAD`` reflog of the seat's checkout shows a commit since the recorded length (the claim's
    reset, or the previous report), set ``Error`` (cause ``qa_commit``) with ``QA_COMMITTED`` (``QA_COMMITTED_HANDOFF``
    after a ``handoff``), then advance the recorded length so each commit is reported once."""
    current = load_state(project.paths, lease.seat).task or {}
    if current.get("conversation") != task.get("conversation"):
        return
    baseline = int(current.get("reflog") or 0)
    worktree = project.paths.seat_worktree(lease.seat)
    subjects = worktrees.reflog_subjects(worktree)
    if len(subjects) <= baseline:
        return
    newer = subjects[:len(subjects) - baseline]  # newest first
    if not any(s.split(":", 1)[0].split(" ", 1)[0] in _COMMIT_ACTIONS for s in newer):
        return
    cfg = project.cfg
    try:
        labels.set_label(project, task["card"], cfg.label_error, "qa_commit", lease.seat,
                         comment=(messages.QA_COMMITTED_HANDOFF if kind == "handoff" else messages.QA_COMMITTED).format(
                             seat=lease.seat),
                         op_id=f"qacommit-{task['conversation']}-{len(subjects)}")
    except KankanError as err:
        project.log("label_failed", card=task["card"], error=err.message)
        return

    def advance(state):
        if state.task and state.task.get("conversation") == task.get("conversation"):
            state.task["reflog"] = len(subjects)
    update_state(project.paths, lease.seat, advance)


# ---------------------------------------------------------------------- commands

def _run(ctx, text: str, turn: str, kind: str) -> dict:
    task = qa_task(ctx, kind)
    project = ctx.project
    with qa.conversation_lock(project.paths, task["conversation"], LOCK_TIMEOUT):
        ids, done = _post(ctx, task, text, turn, kind)
        if not done:
            if kind == "handoff":
                faults.point("qa_after_handoff_summary")
            _finish(project, ctx.lease, task, read_pending(project.paths, task["conversation"]))
    result = {"card": task["card"], "turn": turn, "comments": ids}
    if kind == "handoff":
        result["list"] = project.cfg.backlog
    return result


def answer(ctx, text: str, turn: str) -> dict:
    """``kankan answer --turn <t>``: post the answer (chunked), then mark the turn's inputs covered and signal
    ``answer``; the conversation goes on."""
    return _run(ctx, text, turn, "answer")


def handoff(ctx, text: str, turn: str) -> dict:
    """``kankan handoff --turn <t>``: post the summary (marker ``qh``), then park the card for implementation
    at the bottom of ``Backlog`` and signal ``handoff``; the conversation ends."""
    return _run(ctx, text, turn, "handoff")


def complete_pending(project, lease, task: dict, lock_timeout: float = LOCK_TIMEOUT) -> Optional[str]:
    """Complete the pending operation of ``task``'s conversation: after a crash (launcher restart), or when a turn
    ended without a signal or before a new turn starts while the launcher stays alive (questions T§4). Posts the
    chunks not yet posted, then step 4. A handoff whose transition already happened (ref ``parked`` or ``impl`` with
    the same ``handoff`` IDs) only signals and removes ``pending.json``. An operation of an older turn is completed
    without a signal. Returns the completed kind when it concerns the current turn, ``"handoff"`` for any completed
    handoff (the conversation is over), else None. Raises ``NOT_OWNER`` when the conversation ended and
    ``qa.LockBusy`` when the conversation lock is not taken within ``lock_timeout`` (a command is still posting)."""
    conversation = task.get("conversation")
    if task.get("kind") != "qa" or not qa.valid_conversation(conversation):
        return None
    if not _pending_path(project.paths, conversation).exists():
        return None
    with qa.conversation_lock(project.paths, conversation, lock_timeout):
        pending = read_pending(project.paths, conversation)
        if pending is None:
            return None
        turn_data = qa.read_turn(project.paths, conversation) or {}
        kind = _complete_locked(project, lease, task, pending, turn_data)
        current = turn_data.get("token") == pending.get("turn")
        return kind if kind is not None and (current or kind == "handoff") else None


def _complete_locked(project, lease, task: dict, pending: dict, turn_data: dict) -> Optional[str]:
    """Under the conversation lock: complete ``pending`` (the remaining chunks, then step 4); None when it had
    already completed (``turn.json`` records it) and is only removed."""
    conversation = pending["conversation"]
    done = _completed(turn_data, pending.get("kind"))
    if turn_data.get("token") == pending.get("turn") and done is not None and done.get("hash") == pending.get("hash"):
        project.log("qa_pending_dropped", card=task["card"], conversation=conversation, turn=pending.get("turn"))
        _drop_pending(project.paths, conversation)
        return None
    value = project.refs.read(card_ref(task["card"]))
    if not (pending["kind"] == "handoff" and qa.handed_off(value, pending["conversation"], pending["posted"])):
        if not qa.ours(value, lease.seat, conversation):
            qa.drop_orphan(project, task["card"], conversation)
            raise _ended(task)
        _post_chunks(project, lease, task, pending, value.oid)
    _finish(project, lease, task, pending)
    project.log("qa_pending_completed", seat=lease.seat, card=task["card"], conversation=conversation,
                kind=pending["kind"], turn=pending["turn"])
    return pending["kind"]
