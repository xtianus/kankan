"""Agent commands (spec §13, §16.2): they act only on the seat's current task."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from kankan import attachments, cards, cardsections, messages, notify, qa, worktrees
from kankan.adapters import adapter_types
from kankan.cards import CardMeta, Phase, implemented_destination, target_list_for, transition
from kankan.config import project_lock
from kankan.errors import KankanError, Status
from kankan.project import Project
from kankan.refstore import RefValue, card_ref
from kankan.seats import AgentEnv, Lease, seat_type, verify_owner
from kankan.seatstate import SeatState, load_state, update_state
from kankan.trello import Comment

QA_PHASES = (Phase.QA.value,)
_MENTIONS = re.compile(r"\n\n@\S+(?: @\S+)*$")
_CHUNK_PREFIX = qa.CHUNK_PREFIX


@dataclass
class AgentContext:
    project: Project
    seat: str
    session: str
    state: SeatState
    lease: Lease

    @property
    def worktree(self) -> Path:
        return self.project.paths.seat_worktree(self.seat)

    @property
    def task(self) -> dict:
        if not self.state.task:
            raise KankanError(Status.NO_WORK, "no task is assigned to this seat")
        return self.state.task


def context(cwd=None) -> AgentContext:
    seat, session, _ = AgentEnv.from_environ(cwd)
    _check_code(cwd, seat, session)
    project = Project.open(cwd or os.getcwd())
    seat_value = verify_owner(project, seat, session)
    lease = Lease(seat=seat, type=seat_type(seat), session=session, seat_oid=seat_value.oid, fd=-1)
    return AgentContext(project, seat, session, load_state(project.paths, seat), lease)


def _check_code(cwd, seat: str, session: str) -> None:
    """Refuse when this command runs different kankan code than the seat's launcher (an upgrade or an edit since,
    or another copy of kankan): the hash compares, the versions and package directories explain."""
    from kankan.build import identity
    from kankan.seats import read_agent_file
    data = read_agent_file(cwd or os.getcwd()) or {}
    recorded, mine = data.get("code"), identity()
    if not recorded or data.get("session") != session or recorded == mine["code"]:
        return
    theirs = {"code": recorded, "version": data.get("version") or "?", "package": data.get("package") or "?"}

    def side(info: dict) -> str:
        text = messages.CODE_SIDE.format(**info)
        return text + f" (code {info['code']})" if theirs["version"] == mine["version"] else text
    raise KankanError(Status.CONFIG_ERROR, messages.CODE_MISMATCH.format(seat=seat, launcher=side(theirs),
                                                                        command=side(mine)),
                      {"launcher": theirs, "command": mine})


def _card_ref(ctx: AgentContext) -> Optional[RefValue]:
    return ctx.project.refs.read(card_ref(ctx.task["card"]))


def _owns(ctx: AgentContext, value: Optional[RefValue], phases) -> bool:
    """The ownership rule of the agent commands: the card ref exists, names this seat and is in ``phases``."""
    return value is not None and value.meta.get("owner") == ctx.seat and value.meta.get("phase") in phases


def _phases(ctx: AgentContext):
    """The phases a command of the current task may hold: ``qa`` for a Q&A task, else impl/review."""
    return QA_PHASES if ctx.task.get("kind") == "qa" else cards.OWNED_PHASES


def refuse_qa(ctx: AgentContext, command: str) -> None:
    """``usage_error`` for a work command in a Q&A task (questions F§4.2); ``no_work`` without a task."""
    if ctx.task.get("kind") == "qa":
        raise KankanError(Status.USAGE_ERROR, messages.QA_REFUSED.format(command=command))


def _qa_ended(ctx: AgentContext) -> KankanError:
    current = ctx.task
    return KankanError(Status.NOT_OWNER, messages.QA_ENDED.format(short=current.get("short") or current["card"]))


def _qa_owns(ctx: AgentContext, value: Optional[RefValue]) -> bool:
    """The Q&A ownership rule: the task is not ``ended`` and the ref is this seat's ``qa`` ref of the task's
    conversation."""
    return not ctx.task.get("ended") and qa.ours(value, ctx.seat, ctx.task.get("conversation"))


def _own_ref(ctx: AgentContext, phases) -> RefValue:
    """The task's card ref when this seat holds it in ``phases``. With ``QA_PHASES`` (a Q&A task) every failure,
    an ended task included, is ``not_owner`` (questions F§4.2); otherwise ``ineligible``."""
    value = _card_ref(ctx)
    if phases == QA_PHASES:
        if not _qa_owns(ctx, value):
            raise _qa_ended(ctx)
        return value
    if not _owns(ctx, value, phases):
        raise KankanError(Status.INELIGIBLE, messages.NOT_HELD, {"phase": value.meta.get("phase") if value else None})
    return value


def _hold(ctx: AgentContext, phases) -> attachments.Hold:
    """``_owns`` for ``attachments.for_task``, without raising: ``OTHER`` also when the ref cannot be read."""
    try:
        value = _card_ref(ctx)
    except KankanError:
        return attachments.Hold.OTHER
    if value is None:
        return attachments.Hold.GONE
    held = _qa_owns(ctx, value) if phases == QA_PHASES else _owns(ctx, value, phases)
    return attachments.Hold.HELD if held else attachments.Hold.OTHER


def _thread(project, comments: List[Comment], new: Optional[set] = None) -> List[dict]:
    """The ``comments`` field of ``task``, oldest first: ``role`` is ``agent`` for kankan's Q&A answers and
    handoff summaries, ``kankan`` for its other comments, ``user`` for board members; comments by people outside
    the board are omitted. With ``new`` (a Q&A task) each entry gets ``new``."""
    me = notify.kankan_member_id(project)
    members = notify.input_members(project)
    result = []
    for comment in comments:
        if comment.member_id == me:
            role = "agent" if qa.QA_MARKER.search(comment.text) else "kankan"
        elif comment.member_id in members:
            role = "user"
        else:
            continue
        entry = {"id": comment.id, "author": comment.username, "date": comment.date, "text": comment.text,
                 "role": role}
        if new is not None:
            entry["new"] = comment.id in new
        result.append(entry)
    return result


def _summary(chunks: List[Comment]) -> str:
    """The text of a handoff summary from its chunk comments: without the markers, the ``i/m`` prefixes and the
    mention of the last chunk."""
    bodies = []
    for comment in chunks:
        body = qa.QA_MARKER.sub("", comment.text).rstrip()
        if len(chunks) > 1:
            body = _CHUNK_PREFIX.sub("", body, count=1)
        bodies.append(body)
    bodies[-1] = _MENTIONS.sub("", bodies[-1])
    return "\n\n".join(bodies)


def _handoff(project, meta: dict, comments: List[Comment]) -> Optional[str]:
    """The handoff summary of an implementation task (questions T§5, T§7): the comments listed in the ref's
    ``handoff`` field; otherwise the latest summary kankan posted (every chunk of it); otherwise None."""
    by_id = {comment.id: comment for comment in comments}
    listed = [by_id[ident] for ident in meta.get("handoff") or [] if ident in by_id]
    if listed:
        return _summary(listed)
    me = notify.kankan_member_id(project)
    marked = []
    for comment in comments:
        found = qa.QA_MARKER.search(comment.text) if comment.member_id == me else None
        if found and found.group(1) == "qh":
            marked.append((comment, found))
    if not marked:
        return None
    latest = max(marked, key=lambda item: (len(item[0].id), item[0].id))[1]
    group = sorted(((int(found.group(4)), comment) for comment, found in marked
                    if found.group(2, 3) == latest.group(2, 3)), key=lambda item: item[0])
    return _summary([comment for _, comment in group])


def _qa_card(ctx: AgentContext):
    """One card read (C1) for a Q&A command: ``not_owner`` when the conversation is over (the card left ``Q&A``,
    was archived or deleted, or got ``HOLD``), even before the launcher's poll has deleted the ref."""
    card = ctx.project.trello.card(ctx.task["card"])
    if qa.active_ended(ctx.project.cfg, card):
        raise _qa_ended(ctx)
    return card


def _qa_still_ours(ctx: AgentContext) -> bool:
    """The ref is still this seat's conversation and the seat task (read again) is the same, not ``ended``."""
    current = load_state(ctx.project.paths, ctx.seat).task or {}
    return current.get("id") == ctx.task["id"] and not current.get("ended") \
        and qa.ours(_card_ref(ctx), ctx.seat, ctx.task.get("conversation"))


def _discard_ended_files(ctx: AgentContext, short: str) -> None:
    """The attachment directory of a ``task`` whose conversation ended meanwhile, only while no newer task can use
    it: under the project lock (tried without waiting, since claims hold it), the card's ref is gone, or is still
    this task's conversation and the seat task is still this one. A newer conversation or task, also of this seat,
    keeps its files; anything left over is removed by the next end or ``reconcile --fix``."""
    from kankan.config import project_lock
    project = ctx.project
    try:
        with project_lock(project.paths, timeout=0):
            value = _card_ref(ctx)
            current = load_state(project.paths, ctx.seat).task or {}
            if value is None or (qa.ours(value, ctx.seat, ctx.task.get("conversation"))
                                 and current.get("id") == ctx.task["id"]):
                attachments.discard(project.paths, short)
    except KankanError as err:  # the project lock is busy, or the ref unreadable: the files stay
        project.log("qa_task_files_kept", card=ctx.task["card"], error=err.message)


def _qa_task_data(ctx: AgentContext) -> dict:
    """``task`` of a Q&A task (questions F§4.1, T§5): ``new`` comes from ``turn.json``, so repeated calls in a turn
    agree. A conversation that ended, before, during or after the attachment download and the thread read, gives
    ``not_owner``; the card read last then also deletes the downloaded files (as ``Hold.GONE`` does) unless a newer
    task or conversation may use them (``_discard_ended_files``)."""
    current = ctx.task
    project = ctx.project
    _own_ref(ctx, QA_PHASES)
    card = _qa_card(ctx)
    turn = qa.read_turn(project.paths, current["conversation"]) or {}
    delivered = qa.Inputs.from_dict(turn.get("inputs"))
    try:
        inputs = attachments.for_task(project, card, still_held=lambda: _hold(ctx, QA_PHASES))
    except KankanError as err:
        if err.status is Status.INELIGIBLE:
            raise _qa_ended(ctx) from err
        raise
    for entry in inputs:
        entry["new"] = entry["id"] in delivered.attachments
    comments = _thread(project, project.trello.comments(card.id), set(delivered.comments))
    latest = project.trello.card(current["card"])
    if qa.active_ended(project.cfg, latest) or not _qa_still_ours(ctx):
        _discard_ended_files(ctx, card.short)
        raise _qa_ended(ctx)
    return {
        "task_id": current["id"], "kind": "qa",
        "card": {"title": card.name, "desc": card.desc, "url": card.url, "shortLink": card.short},
        "turn": turn.get("token") or current.get("turn"), "attachments": inputs, "comments": comments,
        "worktree": str(ctx.worktree), "integration": current.get("base"), "instructions": messages.INSTRUCTIONS["qa"],
    }


def task(ctx: AgentContext) -> dict:
    current = ctx.task
    if current.get("kind") == "qa":
        return _qa_task_data(ctx)
    phases = {"impl": (Phase.IMPL.value,), "review": (Phase.REVIEW.value,)}.get(current["kind"], cards.OWNED_PHASES)
    value = _own_ref(ctx, phases)
    card = ctx.project.trello.card(current["card"])
    if card is None or card.closed:
        raise KankanError(Status.INELIGIBLE, "the card was deleted or archived")
    inputs = attachments.for_task(ctx.project, card, still_held=lambda: _hold(ctx, phases))
    extra = {}
    if current["kind"] == "impl":
        thread = ctx.project.trello.comments(card.id)
        extra = {"comments": _thread(ctx.project, thread), "handoff": _handoff(ctx.project, value.meta, thread)}
    parsed = cardsections.parse(card.desc, adapter_types(ctx.project.paths))
    if current["kind"] in ("impl", "review"):
        # card instructions T§4: the seat's sections, or the problems of a description broken during the task
        phase = "implementation" if current["kind"] == "impl" else "review"
        extra["card_instructions"] = cardsections.for_seat(parsed, ctx.lease.type, phase) if parsed.valid else None
        extra["card_instructions_error"] = None if parsed.valid else cardsections.problems_text(parsed.problems)
    return {
        "task_id": current["id"], "kind": current["kind"], "card_id": card.id, "short": card.short,
        "title": card.name, "url": card.url, "desc": parsed.spec, "phase": value.meta.get("phase"),
        "branch": value.meta.get("branch"), "worktree": str(ctx.worktree), "main": ctx.project.cfg.integration_branch,
        "integration_branch": ctx.project.cfg.integration_branch,
        "dirty_files": worktrees.dirty_files(ctx.worktree), "resumed": bool(current.get("resumed")),
        "hold": ctx.project.cfg.label_hold in card.label_ids,
        "instructions": messages.INSTRUCTIONS.get(current["kind"], ""), "attachments": inputs, **extra,
    }


def note(ctx: AgentContext, text: str) -> dict:
    current = ctx.task
    _own_ref(ctx, _phases(ctx))
    if current.get("kind") == "qa":
        _qa_card(ctx)
    op_id = "note-" + hashlib.sha1(f"{current['id']}\0{text}".encode()).hexdigest()[:16]
    ctx.project.trello.add_comment(current["card"], f"{ctx.seat}: {text}", op_id)
    return {"card": current["card"]}


def signal(ctx: AgentContext, kind: str, payload: Optional[dict] = None) -> None:
    write_signal(ctx.project, ctx.seat, ctx.task["id"], kind, payload)


def write_signal(project, seat: str, task_id: str, kind: str, payload: Optional[dict] = None) -> None:
    """Record the agent's signal for the launcher in the seat state. Q&A signals carry ``turn`` in ``payload``
    (questions T§5), so the launcher drops a signal of an older turn."""
    def write(state):
        state.signal = {"task_id": task_id, "kind": kind, "payload": payload or {}}
    update_state(project.paths, seat, write)
    turn = {"turn": payload["turn"]} if payload and payload.get("turn") else {}
    project.log("signal", seat=seat, task=task_id, kind=kind, **turn)


def require_clean(worktree) -> None:
    dirty = worktrees.dirty_files(worktree)
    if dirty:
        raise KankanError(Status.NEEDS_ACTION, "the worktree is not clean: commit or remove these files: "
                          + ", ".join(dirty), {"files": dirty})


def _converge_quietly(ctx: AgentContext) -> None:
    try:
        cards.converge(ctx.project, ctx.lease)
    except KankanError as err:
        ctx.project.log("converge_failed", error=err.message)


def done(ctx: AgentContext) -> dict:
    refuse_qa(ctx, "done")
    current = ctx.task
    if current["kind"] == "review":
        from kankan import integrate
        return integrate.done_review(ctx)
    if current["kind"] != "impl":
        raise KankanError(Status.NEEDS_ACTION, "this task is not closed with `done`; follow the task instructions")
    value = _card_ref(ctx)
    meta = value.meta if value else {}
    parked_by_removal = meta.get("phase") == Phase.PARKED.value and meta.get("cause") == "seat_removed"
    if (meta.get("phase") == Phase.IMPLEMENTED.value or parked_by_removal) and meta.get("implementer") == ctx.seat:
        if worktrees.current_branch(ctx.worktree) == meta.get("branch"):
            worktrees.detach(ctx.worktree)
        _converge_quietly(ctx)
        signal(ctx, "done")
        return {"card": current["card"], "phase": meta["phase"], "branch": meta.get("branch")}
    value = _own_ref(ctx, (Phase.IMPL.value,))
    branch = value.meta["branch"]
    require_clean(ctx.worktree)
    main = ctx.project.cfg.integration_branch
    if worktrees.commits_ahead(ctx.project.paths.repo, branch, main) < 1:
        raise KankanError(Status.NEEDS_ACTION, f"branch {branch} has no commit beyond {main}: commit your work first")
    if worktrees.current_branch(ctx.worktree) == branch:
        worktrees.detach(ctx.worktree)
    card = _trello_card(ctx)
    with project_lock(ctx.project.paths):  # seat T§3: the destination from the configuration of this moment
        phase, target, cause = implemented_destination(ctx.project.reload_config(), ctx.seat)
        new = CardMeta.from_dict(value.meta).evolve(phase=phase, owner=None, session=None, cause=cause,
                                                    target_list=target)
        text = messages.IMPLEMENTED if phase == Phase.IMPLEMENTED.value else messages.IMPLEMENTED_NO_REVIEWER
        transition(ctx.project, ctx.lease, card, value, new, text.format(seat=ctx.seat, branch=branch))
    signal(ctx, "done")
    return {"card": current["card"], "phase": phase, "branch": branch}


def _trello_card(ctx: AgentContext):
    card = ctx.project.trello.card(ctx.task["card"])
    if card is None:
        from kankan.trello import Card
        card = Card(id=ctx.task["card"], short=ctx.task.get("short", ""), board_id="", name="", desc="", url="",
                    list_id="", pos=0, label_ids=[], attachments=[], closed=True, last_activity="")
    return card


def block(ctx: AgentContext, reason: str) -> dict:
    from kankan import integrate
    return integrate.block(ctx, reason)


def _yield_meta(project, value: RefValue, cause: str = "yield", current_list: Optional[str] = None) -> CardMeta:
    meta = CardMeta.from_dict(value.meta)
    if cause != "yield":
        return meta.evolve(phase=Phase.PARKED.value, owner=None, session=None, cause=cause, target_list=current_list)
    if value.meta.get("phase") == Phase.REVIEW.value:
        phase, target, cause = implemented_destination(project.reload_config(), meta.implementer)
        return meta.evolve(phase=phase, owner=None, session=None, cause=cause, target_list=target)
    return meta.evolve(phase=Phase.PARKED.value, owner=None, session=None, cause="yield",
                       target_list=project.cfg.backlog)


def kankan_yield(project, lease: Lease, card, value: RefValue, cause: str = "yield") -> str:
    """Give the card back (§10.3): impl → Backlog top, review → implementer's Implemented.

    With a user-move cause (§14.6) the ref is parked where the user put the card, without a move.
    """
    worktree = project.paths.seat_worktree(lease.seat)
    require_clean(worktree)
    if worktrees.current_branch(worktree) is not None:
        worktrees.detach(worktree)
    current = card.list_id if (card is not None and not card.closed) else None
    if card is None:
        from kankan.trello import Card
        card = Card(id=value.meta["card"], short=value.meta.get("short", ""), board_id="", name="", desc="", url="",
                    list_id="", pos=0, label_ids=[], attachments=[], closed=True, last_activity="")
    with project_lock(project.paths):  # seat T§3: a review goes back to the destination of this moment
        new = _yield_meta(project, value, cause, current)
        if value.meta.get("phase") != Phase.REVIEW.value:
            text = messages.YIELDED
        else:
            text = messages.REVIEW_YIELDED if new.phase == Phase.IMPLEMENTED.value else messages.REVIEW_YIELDED_BACKLOG
        text = text.format(seat=lease.seat, branch=value.meta.get("branch"))
        transition(project, lease, card, value, new, text if cause == "yield" else messages.USER_MOVED_YIELD.format(
            seat=lease.seat, branch=value.meta.get("branch")))
    return new.phase


def _qa_yield(ctx: AgentContext) -> dict:
    """``yield`` in a Q&A task: end the conversation, the card stays in ``Q&A`` (questions F§4.2). On a task
    already marked ``ended`` it only signals; a ref gone or another conversation's otherwise is ``not_owner``."""
    current = ctx.task
    payload = {"turn": current["turn"]} if current.get("turn") else None
    if not current.get("ended"):
        _own_ref(ctx, QA_PHASES)
        qa.end(ctx.project, ctx.lease, current)
    signal(ctx, "yield", payload)
    return {"card": current["card"], "phase": None}


def yield_(ctx: AgentContext) -> dict:
    """Agent ``yield``: commit WIP first; idempotent after the transition happened."""
    current = ctx.task
    if current.get("kind") == "qa":
        return _qa_yield(ctx)
    value = _card_ref(ctx)
    meta = value.meta if value else {}
    if not _owns(ctx, value, cards.OWNED_PHASES):
        if value is not None and meta.get("owner") is None and meta.get("phase") in (Phase.PARKED.value,
                                                                                     Phase.IMPLEMENTED.value):
            if worktrees.current_branch(ctx.worktree) is not None and not worktrees.dirty_files(ctx.worktree):
                worktrees.detach(ctx.worktree)
            signal(ctx, "yield")
            return {"card": current["card"], "phase": meta.get("phase")}
        raise KankanError(Status.INELIGIBLE, messages.NOT_HELD)
    require_clean(ctx.worktree)
    state = load_state(ctx.project.paths, ctx.seat)
    reason = (state.suspend_requested or {}).get("reason") or (state.task or {}).get("suspend_cause")
    cause = "yield"
    card = ctx.project.trello.card(current["card"])
    if reason == "user_moved" or reason in ("user_deleted", "user_archived"):
        cause = "user_deleted" if card is None else ("user_archived" if card.closed else "user_moved")
    phase = kankan_yield(ctx.project, ctx.lease, card, value, cause)
    signal(ctx, "yield")
    return {"card": current["card"], "phase": phase}


def _qa_turn(ctx: AgentContext, turn: Optional[str]) -> str:
    """The current turn token of a Q&A task (``turn.json``, else the seat task's); ``needs_action`` when
    ``turn`` is given and is not it."""
    current = ctx.task
    token = (qa.read_turn(ctx.project.paths, current["conversation"]) or {}).get("token") or current.get("turn") or ""
    if turn is not None and turn != token:
        raise KankanError(Status.NEEDS_ACTION, messages.QA_STALE_TURN.format(turn=turn),
                          {"turn": turn, "current": token})
    return token


def _ask_or_fail(ctx: AgentContext, text: str, label_id: str, cause: str, template: str,
                 turn: Optional[str] = None) -> dict:
    """``ask`` or ``fail``. In a Q&A task (``fail`` only) the op-id and the signal carry the turn token and the
    comment cursor stays: the failed turn's inputs are delivered again with the follow-up (questions T§5)."""
    project = ctx.project
    current = ctx.task
    if current.get("kind") == "qa":
        _own_ref(ctx, QA_PHASES)  # before the conversation lock, so an ended conversation's directory stays gone
        with qa.card_conversation_lock(project, current["card"], current["conversation"], qa.CONVERSATION_LOCK_TIMEOUT):
            # under the lock no turn starts: the token checked here stays current until the signal is written
            if not _qa_still_ours(ctx):
                qa.drop_orphan(project, current["card"], current["conversation"])
                raise _qa_ended(ctx)
            qa.check_seat(project, ctx.lease)
            _qa_card(ctx)  # not_owner comes before needs_action (a stale turn)
            token = _qa_turn(ctx, turn)
            return _label_and_comment(ctx, text, label_id, cause, template, token)
    _own_ref(ctx, _phases(ctx))
    return _label_and_comment(ctx, text, label_id, cause, template, None)


def _label_and_comment(ctx: AgentContext, text: str, label_id: str, cause: str, template: str,
                       token: Optional[str]) -> dict:
    from kankan import clock
    project = ctx.project
    current = ctx.task
    in_qa = current.get("kind") == "qa"
    card_id = current["card"]
    project.trello.add_label(card_id, label_id)
    mention = notify.mention(project, card_id)
    mention = f" {mention}" if mention else ""
    key = f"{current['id']}\0{token}\0{text}" if in_qa else f"{current['id']}\0{text}"
    op_id = f"{cause}-" + hashlib.sha1(key.encode()).hexdigest()[:16]
    comment = project.trello.comment_once(card_id, template.format(seat=ctx.seat, text=text) + mention, op_id)

    def record(state):
        state.label_causes.setdefault(card_id, {})[label_id] = {"cause": cause, "at": clock.iso(clock.now())}
        if not in_qa:
            state.comment_cursor[card_id] = comment.id
    update_state(project.paths, ctx.seat, record)
    project.log(cause, seat=ctx.seat, card=card_id)
    signal(ctx, cause, {"turn": token} if in_qa else None)
    return {"card": card_id, "comment": comment.id}


def ask(ctx: AgentContext, question: str) -> dict:
    refuse_qa(ctx, "ask")
    return _ask_or_fail(ctx, question, ctx.project.cfg.label_question, "ask", messages.ASK)


def fail(ctx: AgentContext, reason: str, turn: Optional[str] = None) -> dict:
    """``fail``; in a Q&A task an optional ``turn`` refuses a delayed call of an older turn (``needs_action``)."""
    return _ask_or_fail(ctx, reason, ctx.project.cfg.label_error, "fail", messages.FAIL, turn)
