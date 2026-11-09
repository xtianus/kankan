"""Agent commands (spec §13, §16.2): they act only on the seat's current task."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kankan import attachments, cards, labels, messages, worktrees
from kankan.cards import CardMeta, Phase, target_list_for, transition
from kankan.errors import KankanError, Status
from kankan.project import Project
from kankan.refstore import RefValue, card_ref
from kankan.seats import AgentEnv, Lease, seat_type, verify_owner
from kankan.seatstate import SeatState, load_state, update_state


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
    """Refuse when this command runs different kankan code than the seat's launcher (an edit or upgrade since)."""
    from kankan.build import code_id
    from kankan.seats import read_agent_file
    data = read_agent_file(cwd or os.getcwd()) or {}
    recorded = data.get("code")
    if recorded and data.get("session") == session and recorded != code_id():
        raise KankanError(Status.CONFIG_ERROR,
                          f"kankan changed since the launcher of {seat} started (launcher code {recorded}, this "
                          f"command {code_id()}): restart the agents (kill their tmux sessions, then `kankan up`) "
                          "so that launcher and agent commands run the same kankan", {"launcher": recorded,
                                                                                     "command": code_id()})


def _card_ref(ctx: AgentContext) -> Optional[RefValue]:
    return ctx.project.refs.read(card_ref(ctx.task["card"]))


def _owns(ctx: AgentContext, value: Optional[RefValue], phases) -> bool:
    """The ownership rule of the agent commands: the card ref exists, names this seat and is in ``phases``."""
    return value is not None and value.meta.get("owner") == ctx.seat and value.meta.get("phase") in phases


def _own_ref(ctx: AgentContext, phases) -> RefValue:
    value = _card_ref(ctx)
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
    return attachments.Hold.HELD if _owns(ctx, value, phases) else attachments.Hold.OTHER


def task(ctx: AgentContext) -> dict:
    current = ctx.task
    phases = {"impl": (Phase.IMPL.value,), "review": (Phase.REVIEW.value,)}.get(current["kind"], cards.OWNED_PHASES)
    value = _own_ref(ctx, phases)
    card = ctx.project.trello.card(current["card"])
    if card is None or card.closed:
        raise KankanError(Status.INELIGIBLE, "the card was deleted or archived")
    inputs = attachments.for_task(ctx.project, card, still_held=lambda: _hold(ctx, phases))
    return {
        "task_id": current["id"], "kind": current["kind"], "card_id": card.id, "short": card.short,
        "title": card.name, "url": card.url, "desc": card.desc, "phase": value.meta.get("phase"),
        "branch": value.meta.get("branch"), "worktree": str(ctx.worktree), "main": ctx.project.cfg.integration_branch,
        "integration_branch": ctx.project.cfg.integration_branch,
        "dirty_files": worktrees.dirty_files(ctx.worktree), "resumed": bool(current.get("resumed")),
        "hold": ctx.project.cfg.label_hold in card.label_ids,
        "instructions": messages.INSTRUCTIONS.get(current["kind"], ""), "attachments": inputs,
    }


def note(ctx: AgentContext, text: str) -> dict:
    current = ctx.task
    _own_ref(ctx, cards.OWNED_PHASES)
    op_id = "note-" + hashlib.sha1(f"{current['id']}\0{text}".encode()).hexdigest()[:16]
    ctx.project.trello.add_comment(current["card"], f"{ctx.seat}: {text}", op_id)
    return {"card": current["card"]}


def signal(ctx: AgentContext, kind: str, payload: Optional[dict] = None) -> None:
    task_id = ctx.task["id"]

    def write(state):
        state.signal = {"task_id": task_id, "kind": kind, "payload": payload or {}}
    update_state(ctx.project.paths, ctx.seat, write)
    ctx.project.log("signal", seat=ctx.seat, task=task_id, kind=kind)


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
    current = ctx.task
    if current["kind"] == "review":
        from kankan import integrate
        return integrate.done_review(ctx)
    if current["kind"] != "impl":
        raise KankanError(Status.NEEDS_ACTION, "this task is not closed with `done`; follow the task instructions")
    value = _card_ref(ctx)
    meta = value.meta if value else {}
    if meta.get("phase") == Phase.IMPLEMENTED.value and meta.get("implementer") == ctx.seat:
        if worktrees.current_branch(ctx.worktree) == meta.get("branch"):
            worktrees.detach(ctx.worktree)
        _converge_quietly(ctx)
        signal(ctx, "done")
        return {"card": current["card"], "phase": "implemented", "branch": meta.get("branch")}
    value = _own_ref(ctx, (Phase.IMPL.value,))
    branch = value.meta["branch"]
    require_clean(ctx.worktree)
    main = ctx.project.cfg.integration_branch
    if worktrees.commits_ahead(ctx.project.paths.repo, branch, main) < 1:
        raise KankanError(Status.NEEDS_ACTION, f"branch {branch} has no commit beyond {main}: commit your work first")
    if worktrees.current_branch(ctx.worktree) == branch:
        worktrees.detach(ctx.worktree)
    card = _trello_card(ctx)
    new = CardMeta.from_dict(value.meta).evolve(
        phase=Phase.IMPLEMENTED.value, owner=None, session=None,
        target_list=target_list_for(ctx.project.cfg, Phase.IMPLEMENTED.value, None, ctx.seat))
    transition(ctx.project, ctx.lease, card, value, new, messages.IMPLEMENTED.format(seat=ctx.seat, branch=branch))
    signal(ctx, "done")
    return {"card": current["card"], "phase": "implemented", "branch": branch}


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
        return meta.evolve(phase=Phase.IMPLEMENTED.value, owner=None, session=None,
                           target_list=target_list_for(project.cfg, Phase.IMPLEMENTED.value, None, meta.implementer))
    return meta.evolve(phase=Phase.PARKED.value, owner=None, session=None, cause="yield",
                       target_list=project.cfg.backlog)


def kankan_yield(project, lease: Lease, card, value: RefValue, cause: str = "yield") -> None:
    """Give the card back (§10.3): impl → Backlog top, review → implementer's Implemented.

    With a user-move cause (§14.6) the ref is parked where the user put the card, without a move.
    """
    worktree = project.paths.seat_worktree(lease.seat)
    require_clean(worktree)
    if worktrees.current_branch(worktree) is not None:
        worktrees.detach(worktree)
    current = card.list_id if (card is not None and not card.closed) else None
    new = _yield_meta(project, value, cause, current)
    if card is None:
        from kankan.trello import Card
        card = Card(id=value.meta["card"], short=value.meta.get("short", ""), board_id="", name="", desc="", url="",
                    list_id="", pos=0, label_ids=[], attachments=[], closed=True, last_activity="")
    text = (messages.REVIEW_YIELDED if value.meta.get("phase") == Phase.REVIEW.value else messages.YIELDED).format(
        seat=lease.seat, branch=value.meta.get("branch"))
    transition(project, lease, card, value, new, text if cause == "yield" else messages.USER_MOVED_YIELD.format(
        seat=lease.seat, branch=value.meta.get("branch")))


def yield_(ctx: AgentContext) -> dict:
    """Agent ``yield``: commit WIP first; idempotent after the transition happened."""
    current = ctx.task
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
    kankan_yield(ctx.project, ctx.lease, card, value, cause)
    signal(ctx, "yield")
    return {"card": current["card"], "phase": "parked" if cause != "yield" or meta.get("phase") == "impl" else "implemented"}


def _ask_or_fail(ctx: AgentContext, text: str, label_id: str, cause: str, template: str) -> dict:
    from kankan import clock
    project = ctx.project
    current = ctx.task
    _own_ref(ctx, cards.OWNED_PHASES)
    card_id = current["card"]
    project.trello.add_label(card_id, label_id)
    from kankan import notify
    mention = notify.mention(project, card_id)
    mention = f" {mention}" if mention else ""
    op_id = f"{cause}-" + hashlib.sha1(f"{current['id']}\0{text}".encode()).hexdigest()[:16]
    comment = project.trello.comment_once(card_id, template.format(seat=ctx.seat, text=text) + mention, op_id)

    def record(state):
        state.label_causes.setdefault(card_id, {})[label_id] = {"cause": cause, "at": clock.iso(clock.now())}
        state.comment_cursor[card_id] = comment.id
    update_state(project.paths, ctx.seat, record)
    project.log(cause, seat=ctx.seat, card=card_id)
    signal(ctx, cause)
    return {"card": card_id, "comment": comment.id}


def ask(ctx: AgentContext, question: str) -> dict:
    return _ask_or_fail(ctx, question, ctx.project.cfg.label_question, "ask", messages.ASK)


def fail(ctx: AgentContext, reason: str) -> dict:
    return _ask_or_fail(ctx, reason, ctx.project.cfg.label_error, "fail", messages.FAIL)
