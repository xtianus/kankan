"""``sync``, ``verify``, ``integrate``, review ``done``, ``block`` and ``resolve`` (spec §12)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from typing import List, Optional

from kankan import clock, faults, gitutil, labels, messages, worktrees
from kankan.cards import CardMeta, Phase
from kankan.config import project_lock
from kankan.errors import KankanError, Status
from kankan.refstore import Main, Update, Verify, card_ref, seat_ref
from kankan.seats import verify_owner
from kankan.seatstate import load_state, update_state

OUTPUT_TAIL = 4000
_HASH = re.compile(r"^[0-9a-f]{40}$")


def conflicts(worktree) -> List[str]:
    out = gitutil.out(worktree, ["diff", "--name-only", "--diff-filter=U", "-z"])
    return [path for path in out.split("\0") if path]


def _review_ref(ctx):
    from kankan.agentcmds import _own_ref
    if ctx.task["kind"] != "review":
        raise KankanError(Status.NEEDS_ACTION, "this command is for review tasks")
    return _own_ref(ctx, (Phase.REVIEW.value,))


def _resolve_hash(ctx, tested: str) -> str:
    full = gitutil.rev(ctx.worktree, f"{tested}^{{commit}}") if tested else None
    if not full or not _HASH.match(full):
        raise KankanError(Status.USAGE_ERROR, f"--tested must name a commit, got {tested!r}")
    return full


def sync(ctx) -> dict:
    """Merge the current main into the review branch (§12.2 step 1)."""
    value = _review_ref(ctx)
    branch, main = value.meta["branch"], ctx.project.cfg.main_branch
    if worktrees.merge_in_progress(ctx.worktree):
        found = conflicts(ctx.worktree)
        raise KankanError(Status.NEEDS_ACTION, "a merge is in progress: resolve the conflicts, `git add` and "
                          "`git commit`, then run `kankan sync` again", {"conflicts": found})
    if worktrees.current_branch(ctx.worktree) != branch:
        raise KankanError(Status.NEEDS_ACTION, f"check out {branch} in your worktree first")
    from kankan.agentcmds import require_clean
    require_clean(ctx.worktree)
    proc = gitutil.run(ctx.worktree, ["merge", "--no-edit", main], check=False)
    if proc.returncode != 0:
        found = conflicts(ctx.worktree)
        if found:
            raise KankanError(Status.NEEDS_ACTION, "merge conflicts: resolve them on the branch, `git add` and "
                              "`git commit` (or `kankan block` if you cannot resolve them safely)", {"conflicts": found})
        raise KankanError(Status.INTERNAL_ERROR, f"git merge failed: {proc.stderr.strip()}")
    return {"branch": branch, "head": gitutil.rev(ctx.worktree, "HEAD"), "main": gitutil.rev(ctx.worktree, main)}


def _preconditions(ctx, tested: str) -> List[str]:
    """Why a verification of ``tested`` cannot be recorded (empty list: it can)."""
    problems = []
    value = ctx.project.refs.read(card_ref(ctx.task["card"]))
    branch = value.meta.get("branch") if value else None
    if value is None or value.meta.get("owner") != ctx.seat or value.meta.get("phase") != Phase.REVIEW.value:
        problems.append("the card is no longer under review by this seat")
    if worktrees.current_branch(ctx.worktree) != branch:
        problems.append(f"HEAD is not on {branch}")
    if gitutil.rev(ctx.worktree, "HEAD") != tested:
        problems.append(f"the branch tip is not {tested}")
    if worktrees.merge_in_progress(ctx.worktree):
        problems.append("a merge is in progress")
    dirty = worktrees.dirty_files(ctx.worktree)
    if dirty:
        problems.append("the worktree is not clean: " + ", ".join(dirty))
    try:
        verify_owner(ctx.project, ctx.seat, ctx.session)
    except KankanError:
        problems.append("this session no longer owns the seat")
    state = load_state(ctx.project.paths, ctx.seat)
    if not state.task or state.task.get("id") != ctx.task["id"]:
        problems.append("the task changed")
    return problems


def _kill_group(pgid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig == signal.SIGTERM:
            clock.sleep(0.5)


def verify(ctx, tested: str) -> dict:
    """Run test_command on the worktree at ``tested`` and record the outcome (§12.2 step 3).

    Runs without the project lock; the result is recorded only if nothing changed meanwhile.
    """
    _review_ref(ctx)
    tested = _resolve_hash(ctx, tested)
    command = ctx.project.cfg.test_command
    problems = _preconditions(ctx, tested)
    if problems:
        raise KankanError(Status.NEEDS_ACTION, "cannot verify: " + "; ".join(problems), {"problems": problems})
    if not command:
        return {"tested": tested, "ok": True, "skipped": True, "message": "no test_command configured"}
    timed_out = False
    with tempfile.TemporaryFile() as output_file:
        # output goes to a file, not a pipe: a background child keeping it open must not block verify
        proc = subprocess.Popen(["bash", "-c", command], cwd=str(ctx.worktree), stdout=output_file,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            proc.wait(timeout=ctx.project.cfg.test_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        _kill_group(proc.pid)
        proc.wait()
        output_file.seek(0, os.SEEK_END)
        size = output_file.tell()
        output_file.seek(max(0, size - OUTPUT_TAIL * 4))
        output = output_file.read()
    tail = (output or b"").decode("utf-8", "replace")[-OUTPUT_TAIL:]
    ok = proc.returncode == 0 and not timed_out
    problems = _preconditions(ctx, tested)
    if problems:
        raise KankanError(Status.NEEDS_ACTION, "the worktree changed while the tests ran; result not recorded: "
                          + "; ".join(problems), {"problems": problems, "output_tail": tail})
    record = {"ok": ok, "at": clock.iso(clock.now()), "output_tail": tail}

    def save(state):
        state.verifications[tested] = record
    update_state(ctx.project.paths, ctx.seat, save)
    ctx.project.log("verify", seat=ctx.seat, tested=tested, ok=ok, timed_out=timed_out)
    if not ok:
        reason = f"timed out after {ctx.project.cfg.test_timeout} s" if timed_out else f"exit code {proc.returncode}"
        raise KankanError(Status.NEEDS_ACTION, f"tests failed ({reason}); fix them, commit and verify the new tip",
                          {"tested": tested, "output_tail": tail})
    return {"tested": tested, "ok": True}


# ---------------------------------------------------------------------- integrate
def merges_on_main(repo, main: str, limit: int = 200) -> List[dict]:
    """First-parent merges on main with their kankan trailers, newest first."""
    fmt = "%H%x1f%(trailers:key=Kankan-Card,valueonly,separator=%x2c)%x1f%(trailers:key=Kankan-Tested,valueonly,separator=%x2c)%x1e"
    out = gitutil.run(repo, ["log", "--first-parent", f"-n{limit}", main, f"--format={fmt}"]).stdout
    result = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, card, tested = (record.split("\x1f") + ["", ""])[:3]
        result.append({"merge": sha.strip(), "card": card.strip(), "tested": tested.strip()})
    return result


def card_merged(repo, main: str, short: str) -> Optional[str]:
    for merge in merges_on_main(repo, main, 10000):
        if merge["card"] == short:
            return merge["merge"]
    return None


def _merge_message(card, short: str, tested: str) -> str:
    title = (card.name if card else "") or short
    url = (card.url if card else "") or ""
    return f"{title}\n\n{url}\n\nKankan-Card: {short}\nKankan-Tested: {tested}\n"


def _push(ctx, merge: str) -> None:
    project = ctx.project
    proc = gitutil.run(project.paths.repo, ["push", "origin", f"{merge}:refs/heads/{project.cfg.main_branch}"],
                       check=False, timeout=120)
    if proc.returncode != 0:
        project.log("push_failed", merge=merge, error=proc.stderr.strip()[-500:])
        try:
            project.trello.add_comment(ctx.task["card"], messages.PUSH_FAILED.format(
                main=project.cfg.main_branch, error=proc.stderr.strip()[-300:]), f"pushfail-{merge}")
        except KankanError as err:
            project.log("comment_failed", error=err.message)


def integrate(ctx, tested: str) -> dict:
    """§12.2 step 4: atomic merge of the tested tip into main with a single ref transaction."""
    project = ctx.project
    cfg = project.cfg
    repo = project.paths.repo
    tested = _resolve_hash(ctx, tested)
    ref = card_ref(ctx.task["card"])
    with project_lock(project.paths):
        value = project.refs.read(ref)
        if value is not None and value.meta.get("phase") == Phase.INTEGRATED.value \
                and value.meta.get("implementer") and value.meta.get("merge"):
            return {"merge": value.meta["merge"], "tested": value.meta.get("tested")}
        if value is None or value.meta.get("phase") != Phase.REVIEW.value or value.meta.get("owner") != ctx.seat:
            raise KankanError(Status.INELIGIBLE, "the card is not under review by this seat")
        meta = value.meta
        short = meta["short"]
        card = project.trello.card(ctx.task["card"])
        if card is not None and cfg.label_hold in card.label_ids:
            raise KankanError(Status.ON_HOLD, "the card is on HOLD: commit WIP and run `kankan yield`")
        for merge in merges_on_main(repo, cfg.main_branch):
            if merge["card"] == short and merge["tested"] == tested:
                return _record_integrated(ctx, value, merge["merge"], tested, repaired=True)
        branch = meta["branch"]
        problems = []
        if gitutil.rev(repo, f"refs/heads/{branch}") != tested:
            problems.append(f"the tip of {branch} is not {tested}")
        if worktrees.merge_in_progress(ctx.worktree):
            problems.append("a merge is in progress")
        dirty = worktrees.dirty_files(ctx.worktree)
        if dirty:
            problems.append("the worktree is not clean: " + ", ".join(dirty))
        if cfg.test_command:
            record = load_state(project.paths, ctx.seat).verifications.get(tested)
            if not record or not record.get("ok"):
                problems.append(f"no successful `kankan verify --tested {tested}`")
        if problems:
            raise KankanError(Status.NEEDS_ACTION, "cannot integrate: " + "; ".join(problems), {"problems": problems})
        main_oid = gitutil.rev(repo, f"refs/heads/{cfg.main_branch}")
        if not gitutil.is_ancestor(repo, main_oid, tested):
            _main_moved(ctx, card)
        tree = gitutil.out(repo, ["rev-parse", f"{tested}^{{tree}}"])
        merge = gitutil.out(repo, ["commit-tree", tree, "-p", main_oid, "-p", tested, "-F", "-"],
                            input=_merge_message(card, short, tested))
        new = CardMeta.from_dict(meta).evolve(phase=Phase.INTEGRATED.value, owner=None, session=None,
                                              merge=merge, tested=tested, target_list=cfg.done,
                                              from_list=card.list_id if card else None)
        oid = project.refs.write_blob(new.to_dict())
        faults.point("before_integrate_tx")
        ok = project.refs.transaction([Verify(seat_ref(ctx.seat), ctx.lease.seat_oid), Update(ref, oid, value.oid),
                                       Main(merge, main_oid)])
        if not ok:
            seat_value = project.refs.read(seat_ref(ctx.seat))
            if seat_value is None or seat_value.oid != ctx.lease.seat_oid:
                raise KankanError(Status.NOT_OWNER, "this session no longer owns the seat")
            current = project.refs.read(ref)
            if current is None or current.oid != value.oid:
                raise KankanError(Status.INELIGIBLE, "the card's ref changed meanwhile")
            _main_moved(ctx, card)
        faults.point("after_main")
        update_state(project.paths, ctx.seat, lambda s: setattr(s, "main_moved_count", 0))
        project.log("integrated", card=ctx.task["card"], merge=merge, tested=tested, main_old=main_oid)
        try:
            project.trello.add_comment(ctx.task["card"], messages.INTEGRATED.format(
                seat=ctx.seat, main=cfg.main_branch, merge=merge), f"integrated-{merge}")
        except KankanError as err:
            project.log("comment_failed", error=err.message)
    if cfg.push_main:
        _push(ctx, merge)
    return {"merge": merge, "tested": tested}


def _main_moved(ctx, card) -> None:
    project = ctx.project

    def bump(state):
        state.main_moved_count += 1
        return state.main_moved_count
    count = update_state(project.paths, ctx.seat, bump)
    if count > project.cfg.main_moved_retries:
        try:
            labels.set_label(project, ctx.task["card"], project.cfg.label_error, "main_moved", ctx.seat,
                             comment=messages.MAIN_MOVED_LIMIT.format(count=count), op_id=f"mainmoved-{ctx.task['id']}")
        except KankanError as err:
            project.log("label_failed", error=err.message)
    raise KankanError(Status.MAIN_MOVED, f"{project.cfg.main_branch} moved: run `kankan sync`, the tests, "
                      "`kankan verify` and `kankan integrate` again", {"attempt": count})


def _record_integrated(ctx, value, merge: str, tested: str, repaired: bool) -> dict:
    """Repair a half-applied commit: main already has the merge, the card ref is still ``review``."""
    project = ctx.project
    card = project.trello.card(value.meta["card"])
    new = CardMeta.from_dict(value.meta).evolve(phase=Phase.INTEGRATED.value, owner=None, session=None,
                                                merge=merge, tested=tested, target_list=project.cfg.done,
                                                from_list=card.list_id if card else None)
    oid = project.refs.write_blob(new.to_dict())
    if not project.refs.transaction([Update(card_ref(value.meta["card"]), oid, value.oid)]):
        raise KankanError(Status.INELIGIBLE, "the card's ref changed meanwhile")
    project.log("integrate_repaired", card=value.meta["card"], merge=merge)
    return {"merge": merge, "tested": tested, "repaired": repaired}


def done_review(ctx) -> dict:
    """Review ``done``: card to Done, verification, ref deleted (or left to convergence)."""
    from kankan.agentcmds import signal as send_signal
    project = ctx.project
    cfg = project.cfg
    ref = card_ref(ctx.task["card"])
    value = project.refs.read(ref)
    card = project.trello.card(ctx.task["card"])
    if value is None:
        if card is not None and card.list_id == cfg.done and card_merged(project.paths.repo, cfg.main_branch,
                                                                          ctx.task.get("short", "")):
            _leave_worktree(ctx)
            send_signal(ctx, "done")
            return {"card": ctx.task["card"], "phase": "done"}
        raise KankanError(Status.INELIGIBLE, "the card has no ref and is not done")
    if value.meta.get("phase") != Phase.INTEGRATED.value:
        raise KankanError(Status.NEEDS_ACTION, "the card is not integrated yet: run `kankan integrate --tested <hash>`")
    with project_lock(project.paths):
        moved = card is not None and card.list_id == cfg.done
        if card is not None and not moved:
            try:
                project.trello.move_card(card.id, cfg.done)
                moved = True
            except KankanError as err:
                project.log("move_failed", card=card.id, error=err.message)
        if moved:
            if project.refs.delete(ref, value.oid):
                project.log("ref_closed", card=ctx.task["card"])
            labels.clear_kankan_labels(project, project.trello.card(card.id) or card, None, ctx.seat)
    _leave_worktree(ctx)
    send_signal(ctx, "done")
    return {"card": ctx.task["card"], "phase": "done" if moved else "integrated", "merge": value.meta.get("merge")}


def _leave_worktree(ctx) -> None:
    if worktrees.current_branch(ctx.worktree) is not None and not worktrees.dirty_files(ctx.worktree):
        worktrees.detach(ctx.worktree)
