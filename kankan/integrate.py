"""``sync``, ``verify``, ``integrate``, review ``done``, ``block`` and ``resolve`` (spec §12)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from kankan import clock, faults, gitutil, labels, messages, worktrees
from kankan.cards import CardMeta, Phase
from kankan.config import project_lock
from kankan.errors import KankanError, Status
from kankan.refstore import Main, Update, Verify, card_ref, seat_ref, validate_short_link
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
    branch, main = value.meta["branch"], ctx.project.cfg.integration_branch
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
    return {"branch": branch, "head": gitutil.rev(ctx.worktree, "HEAD"), "main": gitutil.rev(ctx.worktree, main),
            "integration_branch": main}


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
    proc = gitutil.run(project.paths.repo, ["push", "origin", f"{merge}:refs/heads/{project.cfg.integration_branch}"],
                       check=False, timeout=120, env={**os.environ, "KANKAN_PUSH": "1"})
    if proc.returncode != 0:
        project.log("push_failed", merge=merge, error=proc.stderr.strip()[-500:])
        try:
            project.trello.add_comment(ctx.task["card"], messages.PUSH_FAILED.format(
                main=project.cfg.integration_branch, error=proc.stderr.strip()[-300:]), f"pushfail-{merge}")
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
        from kankan.cards import _where_expected
        if not _where_expected(card, meta):
            raise KankanError(Status.INELIGIBLE, "the card was moved, archived or deleted by the user: commit WIP "
                              "and run `kankan yield`")
        if cfg.label_hold in card.label_ids:
            raise KankanError(Status.ON_HOLD, "the card is on HOLD: commit WIP and run `kankan yield`")
        for merge in merges_on_main(repo, cfg.integration_branch):
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
        main_oid = gitutil.rev(repo, f"refs/heads/{cfg.integration_branch}")
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
                seat=ctx.seat, main=cfg.integration_branch, merge=merge), f"integrated-{merge}")
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
    raise KankanError(Status.MAIN_MOVED, f"{project.cfg.integration_branch} moved: run `kankan sync`, the tests, "
                      "`kankan verify` and `kankan integrate` again", {"attempt": count})


def _record_integrated(ctx, value, merge: str, tested: str, repaired: bool) -> dict:
    """Repair a half-applied commit: main already has the merge, the card ref is still ``review``."""
    project = ctx.project
    card = project.trello.card(value.meta["card"])
    new = CardMeta.from_dict(value.meta).evolve(phase=Phase.INTEGRATED.value, owner=None, session=None,
                                                merge=merge, tested=tested, target_list=project.cfg.done,
                                                from_list=card.list_id if card else None)
    oid = project.refs.write_blob(new.to_dict())
    if not project.refs.transaction([Verify(seat_ref(ctx.seat), ctx.lease.seat_oid),
                                     Update(card_ref(value.meta["card"]), oid, value.oid)]):
        seat_value = project.refs.read(seat_ref(ctx.seat))
        if seat_value is None or seat_value.oid != ctx.lease.seat_oid:
            raise KankanError(Status.NOT_OWNER, "this session no longer owns the seat")
        raise KankanError(Status.INELIGIBLE, "the card's ref changed meanwhile")
    project.log("integrate_repaired", card=value.meta["card"], merge=merge)
    return {"merge": merge, "tested": tested, "repaired": repaired}


def _check_task_owner(ctx) -> None:
    """Under the project lock: this session still owns the seat and the task is still current."""
    verify_owner(ctx.project, ctx.seat, ctx.session)
    state = load_state(ctx.project.paths, ctx.seat)
    if not state.task or state.task.get("id") != ctx.task["id"]:
        raise KankanError(Status.INELIGIBLE, "the task changed meanwhile")


def done_review(ctx) -> dict:
    """Review ``done``: card to Done, verification, ref deleted (or left to convergence)."""
    from kankan.agentcmds import signal as send_signal
    from kankan.cards import _hold_after_integration
    project = ctx.project
    cfg = project.cfg
    ref = card_ref(ctx.task["card"])
    branch = ctx.task.get("branch")
    with project_lock(project.paths):
        _check_task_owner(ctx)
        value = project.refs.read(ref)
        card = project.trello.card(ctx.task["card"])
        if value is None:
            if card is not None and card.list_id == cfg.done and card_merged(project.paths.repo, cfg.integration_branch,
                                                                              ctx.task.get("short", "")):
                _leave_worktree(ctx, branch)
                send_signal(ctx, "done")
                return {"card": ctx.task["card"], "phase": "done"}
            raise KankanError(Status.INELIGIBLE, "the card has no ref and is not done")
        if value.meta.get("phase") != Phase.INTEGRATED.value:
            raise KankanError(Status.NEEDS_ACTION, "the card is not integrated yet: run `kankan integrate --tested <hash>`")
        moved = card is not None and not card.closed and card.list_id == cfg.done
        if card is not None and not card.closed and not moved:
            if card.list_id == value.meta.get("from_list"):
                try:
                    project.trello.move_card(card.id, cfg.done)
                    moved = True
                except KankanError as err:
                    project.log("move_failed", card=card.id, error=err.message)
            else:
                # moved by the user after the integration: leave it where it is (reconcile reports it)
                project.log("human_moved", card=card.id, phase="integrated")
        if moved:
            _hold_after_integration(project, project.trello.card(card.id) or card, value.meta)
            if project.refs.delete(ref, value.oid):
                project.log("ref_closed", card=ctx.task["card"])
            labels.clear_kankan_labels(project, project.trello.card(card.id) or card, None, ctx.seat)
    _leave_worktree(ctx, branch)
    send_signal(ctx, "done")
    return {"card": ctx.task["card"], "phase": "done" if moved else "integrated", "merge": value.meta.get("merge")}


def _leave_worktree(ctx, branch: Optional[str]) -> None:
    """Detach the seat worktree only from this task's branch (never from a newer task's)."""
    if branch and worktrees.current_branch(ctx.worktree) == branch and not worktrees.dirty_files(ctx.worktree):
        worktrees.detach(ctx.worktree)


# ---------------------------------------------------------------------- block
BLOCK_STEPS = ("snapshot", "abort", "blocked_wt", "restore", "transition")
BLOCK_LOCK_TIMEOUT = 30


def _block_lock(project, seat: str):
    """One block journal run per seat at a time: a concurrent run could delete the only snapshot."""
    import contextlib
    from kankan.config import file_lock

    @contextlib.contextmanager
    def guarded():
        try:
            lock = file_lock(project.paths.seats_dir / f"{seat}.block.lock", timeout=BLOCK_LOCK_TIMEOUT,
                             what="block lock")
            lock.__enter__()
        except KankanError as err:
            raise KankanError(Status.NEEDS_ACTION, "another `kankan block` is running for this seat; wait for it "
                              "and retry", {"detail": err.message})
        try:
            yield
        finally:
            lock.__exit__(None, None, None)
    return guarded()


def _git_path(worktree, name: str) -> Path:
    path = Path(gitutil.out(worktree, ["rev-parse", "--git-path", name]))
    return path if path.is_absolute() else Path(worktree) / path


def _status_entries(worktree) -> List[str]:
    raw = gitutil.run(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout
    parts = raw.split("\0")
    paths, index = [], 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if not entry:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":
            paths.append(parts[index])
            index += 1
    return sorted(set(paths))


def _untracked(worktree) -> List[str]:
    out = gitutil.run(worktree, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout
    return sorted(p for p in out.split("\0") if p)


def worktree_digest(worktree) -> str:
    """Digest of the whole uncommitted state: status, unstaged and staged diffs, untracked contents."""
    digest = hashlib.sha256()
    for args in (["status", "--porcelain=v2", "-z", "--untracked-files=all"], ["diff", "--binary"],
                 ["diff", "--cached", "--binary"]):
        digest.update(subprocess.run(["git", "-C", str(worktree), *args], capture_output=True).stdout)
        digest.update(b"\0\0")
    for path in _untracked(worktree):
        full = Path(worktree) / path
        digest.update(path.encode() + b"\0")
        digest.update(os.readlink(full).encode() if full.is_symlink() else full.read_bytes())
        digest.update(str(os.lstat(full).st_mode & 0o111).encode())
    return digest.hexdigest()


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    if source.is_symlink():
        os.symlink(os.readlink(source), target)
    else:
        shutil.copy2(source, target)


def block(ctx, reason: str) -> dict:
    """§12.4: with a merge in progress move it to blocked-<short>; then park the card in Blocked."""
    from kankan.agentcmds import _own_ref, require_clean, signal as send_signal
    project = ctx.project
    with _block_lock(project, ctx.seat):
        result = _block_locked(ctx, reason)
    send_signal(ctx, "block")
    return result


def _block_locked(ctx, reason: str) -> dict:
    from kankan.agentcmds import _own_ref, require_clean
    project = ctx.project
    verify_owner(project, ctx.seat, ctx.session)
    state = load_state(project.paths, ctx.seat)
    if state.block_journal is None:
        value = _own_ref(ctx, (Phase.IMPL.value, Phase.REVIEW.value))
        merging = worktrees.merge_in_progress(ctx.worktree)
        if not merging:
            require_clean(ctx.worktree)
        journal = {"card": ctx.task["card"], "short": value.meta["short"], "branch": value.meta["branch"],
                   "reason": reason, "merging": merging, "done": [], "task_id": ctx.task["id"]}
        update_state(project.paths, ctx.seat, lambda s: setattr(s, "block_journal", journal))
    return run_block_journal(project, ctx.lease)


def resume_block(project, lease) -> Optional[dict]:
    """Finish an interrupted ``block`` (called by the launcher at startup)."""
    with _block_lock(project, lease.seat):
        if load_state(project.paths, lease.seat).block_journal is None:
            return None
        return run_block_journal(project, lease)


def _mark(project, seat: str, step: str, **extra) -> dict:
    def mark(state):
        journal = state.block_journal
        if step not in journal["done"]:
            journal["done"].append(step)
        journal.update(extra)
        return dict(journal)
    return update_state(project.paths, seat, mark)


def run_block_journal(project, lease) -> dict:
    journal = load_state(project.paths, lease.seat).block_journal
    seat_wt = project.paths.seat_worktree(lease.seat)
    short = journal["short"]
    snap_dir = project.paths.kankan_dir / "blocks" / short
    blocked = project.paths.blocked_worktree(short)
    if journal["merging"]:
        if "snapshot" not in journal["done"]:
            if snap_dir.exists():
                shutil.rmtree(snap_dir)
            (snap_dir / "files").mkdir(parents=True)
            head = gitutil.rev(seat_wt, "HEAD")
            merge_head = gitutil.rev(seat_wt, "MERGE_HEAD")
            for name in ("MERGE_MSG", "MERGE_MODE"):
                source = _git_path(seat_wt, name)
                if source.exists():
                    shutil.copy2(source, snap_dir / name)
            shutil.copy2(_git_path(seat_wt, "index"), snap_dir / "index")
            deleted, files = [], _status_entries(seat_wt)
            for path in files:
                full = seat_wt / path
                if full.is_symlink() or full.exists():
                    if full.is_dir() and not full.is_symlink():
                        continue
                    _copy(full, snap_dir / "files" / path)
                else:
                    deleted.append(path)
            conflicted = conflicts(seat_wt)
            faults.point("snapshot")
            journal = _mark(project, lease.seat, "snapshot", head=head, merge_head=merge_head, files=files,
                            deleted=deleted, digest=worktree_digest(seat_wt), conflicts=conflicted)
        if "abort" not in journal["done"]:
            # the snapshot holds every change: discard the merge and the files (reset also ends the merge)
            gitutil.run(seat_wt, ["reset", "-q", "--hard", "HEAD"])
            gitutil.run(seat_wt, ["clean", "-fdq"])
            if worktrees.current_branch(seat_wt) is not None:
                worktrees.detach(seat_wt)
            faults.point("abort")
            journal = _mark(project, lease.seat, "abort")
        if "blocked_wt" not in journal["done"]:
            if not (blocked / ".git").exists():
                gitutil.run(project.paths.repo, ["worktree", "prune"])
                gitutil.run(project.paths.repo, ["worktree", "add", "-q", str(blocked), journal["branch"]])
            if not worktrees.merge_in_progress(blocked):
                gitutil.run(blocked, ["merge", "--no-commit", "--no-ff", journal["merge_head"]], check=False)
            faults.point("blocked_wt")
            journal = _mark(project, lease.seat, "blocked_wt")
        if "restore" not in journal["done"]:
            shutil.copy2(snap_dir / "index", _git_path(blocked, "index"))
            for name in ("MERGE_MSG", "MERGE_MODE"):
                if (snap_dir / name).exists():
                    shutil.copy2(snap_dir / name, _git_path(blocked, name))
            for path in journal["files"]:
                source = snap_dir / "files" / path
                if source.is_symlink() or source.exists():
                    _copy(source, blocked / path)
            for path in journal["deleted"]:
                target = blocked / path
                if target.is_symlink() or target.is_file():
                    target.unlink()
            gitutil.run(blocked, ["update-index", "-q", "--refresh"], check=False)
            if worktree_digest(blocked) != journal["digest"]:
                raise KankanError(Status.NEEDS_RECONCILIATION,
                                  f"the state rebuilt in {blocked} differs from the original; the snapshot is kept in "
                                  f"{snap_dir}", {"blocked": str(blocked), "snapshot": str(snap_dir)})
            faults.point("restore")
            journal = _mark(project, lease.seat, "restore")
    else:
        if worktrees.current_branch(seat_wt) is not None:
            worktrees.detach(seat_wt)
    if "transition" not in journal["done"]:
        _block_transition(project, lease, journal)
        faults.point("transition")
        journal = _mark(project, lease.seat, "transition")
    if snap_dir.exists():
        shutil.rmtree(snap_dir)
    update_state(project.paths, lease.seat, lambda s: setattr(s, "block_journal", None))
    project.log("blocked", card=journal["card"], merging=journal["merging"])
    return {"card": journal["card"], "phase": "parked", "blocked_worktree": str(blocked) if journal["merging"] else None,
            "conflicts": journal.get("conflicts", [])}


def _block_transition(project, lease, journal) -> None:
    from kankan.cards import transition
    value = project.refs.read(card_ref(journal["card"]))
    if value is not None and value.meta.get("phase") == Phase.PARKED.value and value.meta.get("cause") == "block":
        return
    if value is None or value.meta.get("owner") != lease.seat:
        raise KankanError(Status.INELIGIBLE, "the card is no longer held by this seat")
    card = project.trello.card(journal["card"])
    if card is None:
        raise KankanError(Status.INELIGIBLE, "the card was deleted")
    new = CardMeta.from_dict(value.meta).evolve(phase=Phase.PARKED.value, owner=None, session=None, cause="block",
                                                target_list=project.cfg.blocked)
    files = ", ".join(journal.get("conflicts") or []) or "none"
    from kankan import notify
    mention = notify.mention(project, card.id)
    mention = f" {mention}" if mention else ""
    comment = messages.BLOCKED.format(seat=lease.seat, reason=journal["reason"], files=files, short=journal["short"]) + mention
    transition(project, lease, card, value, new, comment)


# ---------------------------------------------------------------------- resolve
def find_card_ref(project, short: str):
    from kankan.refstore import CARD_PREFIX
    validate_short_link(short)
    for ref, value in project.refs.list(CARD_PREFIX).items():
        if value.meta.get("short") == short:
            return value
    return None


def resolve_help(project, short: str, blocked: Path, remaining: List[str]) -> str:
    files = "\n".join(f"  - {path}" for path in remaining) or "  (none left)"
    return messages.RESOLVE_HELP.format(short=short, path=blocked, files=files)


def resolve(project, short: str, done: bool, tmux=None) -> dict:
    from kankan.cards import AdminActor, target_list_for, transition
    value = find_card_ref(project, short)
    if value is None or value.meta.get("phase") != Phase.PARKED.value:
        raise KankanError(Status.INELIGIBLE, f"card {short} is not blocked")
    blocked = project.paths.blocked_worktree(short)
    has_wt = (blocked / ".git").exists()
    if not done:
        if not has_wt:
            return {"short": short, "worktree": None}, messages.RESOLVE_NOTHING.format(short=short)
        remaining = conflicts(blocked)
        help_text = resolve_help(project, short, blocked, remaining)
        from kankan.tmux import Tmux, session_name
        tmux = tmux or Tmux()
        name = session_name("kankan-resolve-", project.paths.project, short)
        if not tmux.has_session(name):
            tmux.new_session(name, str(blocked), ["bash", "-c", 'printf "%s\\n" "$KANKAN_RESOLVE_HELP"; exec "${SHELL:-bash}"'],
                             env={"KANKAN_RESOLVE_HELP": help_text})
        return {"short": short, "worktree": str(blocked), "conflicts": remaining, "tmux_session": name}, \
            help_text + f"\nA shell is open in tmux session {name} (tmux attach -t {name})."
    if has_wt:
        problems = []
        if worktrees.merge_in_progress(blocked):
            problems.append("the merge is not committed yet")
        dirty = worktrees.dirty_files(blocked)
        if dirty:
            problems.append("uncommitted files: " + ", ".join(dirty))
        if problems:
            raise KankanError(Status.NEEDS_ACTION, "; ".join(problems) + "\n" + resolve_help(
                project, short, blocked, conflicts(blocked)), {"problems": problems})
        gitutil.run(project.paths.repo, ["worktree", "remove", str(blocked)])
    card = project.trello.card(value.meta["card"])
    if card is None:
        raise KankanError(Status.INELIGIBLE, f"card {short} no longer exists")
    implementer = value.meta.get("implementer")
    if implementer not in project.cfg.seats:
        raise KankanError(Status.NEEDS_RECONCILIATION, f"the implementer seat {implementer} is not configured")
    new = CardMeta.from_dict(value.meta).evolve(phase=Phase.IMPLEMENTED.value, owner=None, session=None, cause=None,
                                                target_list=target_list_for(project.cfg, Phase.IMPLEMENTED.value, None,
                                                                            implementer))
    transition(project, AdminActor("resolve"), card, value, new, messages.RESOLVED.format(short=short))
    return {"short": short, "phase": "implemented"}, f"card {short} is back in {implementer}'s Implemented list"


def merge_request(project, branch: Optional[str] = None, remote: bool = False, top: bool = True,
                  title_prefix: str = ""):
    """Create a Backlog card asking the agents to merge a snapshot of a user branch (spec §12.5)."""
    cfg = project.cfg
    repo = project.paths.repo
    branch = branch or cfg.user_branch
    if gitutil.run(repo, ["check-ref-format", "--branch", branch], check=False).returncode != 0 or \
            branch.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise KankanError(Status.USAGE_ERROR, f"{branch!r} is not a branch name")
    if worktrees.reserved_branch(cfg, branch):
        raise KankanError(Status.USAGE_ERROR, f"{branch} belongs to kankan: a merge request takes one of your branches")
    local = gitutil.rev(repo, f"refs/heads/{branch}")
    if remote or local is None:
        if "origin" in gitutil.out(repo, ["remote"], check=False).split():
            proc = gitutil.run(repo, ["fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                               check=False, timeout=120)
            if proc.returncode != 0:
                raise KankanError(Status.NETWORK_ERROR, f"cannot fetch {branch} from origin: "
                                  f"{proc.stderr.strip()[-300:]}")
        sha = gitutil.rev(repo, f"refs/remotes/origin/{branch}")
        source = f"origin/{branch}"
    else:
        sha, source = local, branch
    if sha is None:
        raise KankanError(Status.USAGE_ERROR, f"branch {branch} exists neither locally nor on origin")
    sha = gitutil.out(repo, ["rev-parse", f"{sha}^{{commit}}"])
    if gitutil.is_ancestor(repo, sha, f"refs/heads/{cfg.integration_branch}"):
        raise KankanError(Status.NEEDS_ACTION, f"nothing to merge: {cfg.integration_branch} already contains {source}")
    title = messages.MERGE_REQUEST_TITLE.format(prefix=title_prefix, branch=source, integration=cfg.integration_branch)
    desc = messages.MERGE_REQUEST_DESC.format(sha=sha, short_sha=sha[:12], branch=source,
                                              integration=cfg.integration_branch)
    card = project.trello.create_card(cfg.backlog, title, desc=desc, pos="top" if top else "bottom")
    project.log("merge_request", card=card.id, branch=source, sha=sha)
    return card
