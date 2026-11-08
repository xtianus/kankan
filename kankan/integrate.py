"""``sync``, ``verify``, ``integrate``, review ``done``, ``block`` and ``resolve`` (spec §12)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from typing import List

from kankan import clock, gitutil, worktrees
from kankan.cards import Phase
from kankan.errors import KankanError, Status
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
    value = ctx.project.refs.read(__import__("kankan.refstore", fromlist=["card_ref"]).card_ref(ctx.task["card"]))
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
