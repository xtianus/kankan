"""Agent commands (spec §13, §16.2): they act only on the seat's current task."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kankan import cards, labels, messages, worktrees
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
    seat, session, _ = AgentEnv.from_environ()
    project = Project.open(cwd or os.getcwd())
    seat_value = verify_owner(project, seat, session)
    lease = Lease(seat=seat, type=seat_type(seat), session=session, seat_oid=seat_value.oid, fd=-1)
    return AgentContext(project, seat, session, load_state(project.paths, seat), lease)


def _card_ref(ctx: AgentContext) -> Optional[RefValue]:
    return ctx.project.refs.read(card_ref(ctx.task["card"]))


def _own_ref(ctx: AgentContext, phases) -> RefValue:
    value = _card_ref(ctx)
    if value is None or value.meta.get("owner") != ctx.seat or value.meta.get("phase") not in phases:
        raise KankanError(Status.INELIGIBLE, "the card of the current task is no longer held by this seat",
                          {"phase": value.meta.get("phase") if value else None})
    return value


def task(ctx: AgentContext) -> dict:
    current = ctx.task
    phases = {"impl": (Phase.IMPL.value,), "review": (Phase.REVIEW.value,)}.get(current["kind"], cards.OWNED_PHASES)
    value = _own_ref(ctx, phases)
    card = ctx.project.trello.card(current["card"])
    if card is None or card.closed:
        raise KankanError(Status.INELIGIBLE, "the card was deleted or archived")
    return {
        "task_id": current["id"], "kind": current["kind"], "card_id": card.id, "short": card.short,
        "title": card.name, "url": card.url, "desc": card.desc, "phase": value.meta.get("phase"),
        "branch": value.meta.get("branch"), "worktree": str(ctx.worktree), "main": ctx.project.cfg.main_branch,
        "dirty_files": worktrees.dirty_files(ctx.worktree), "resumed": bool(current.get("resumed")),
        "hold": ctx.project.cfg.label_hold in card.label_ids,
        "instructions": messages.INSTRUCTIONS.get(current["kind"], ""),
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
    main = ctx.project.cfg.main_branch
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
