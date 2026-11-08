"""Regression tests for the whole-branch review findings (Codex advisor)."""

import json
import multiprocessing
import os
import subprocess
import sys
import time

import pytest

from kankan import agentcmds, cards, diag, faults, gitutil, integrate, refstore, seats, workflow
from kankan.config import file_lock
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref, seat_ref
from kankan.seatstate import load_state, update_state
from tests.conftest import git
from tests.flow import agent_ctx, commit, review_setup
from tests.test_ask import FakeTmux, make_launcher, waiting  # noqa: F401 (fixture)
from tests.test_block_resolve import conflicted  # noqa: F401 (fixture)


# 1 -----------------------------------------------------------------------------------------
VICTIM = """
import subprocess, time
subprocess.Popen(['sleep', '1000'])
print('ready', flush=True)
time.sleep(1000)
"""


def test_recover_kill_never_expands_to_unvalidated_session(kproject):
    project = kproject
    lease = seats.acquire(project, "claude")
    victim = subprocess.Popen([sys.executable, "-c", VICTIM], start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert victim.stdout.readline().strip() == "ready"
        sibling = [p for p in __import__("kankan.procutil", fromlist=["x"]).pids_in_session(victim.pid) if p != victim.pid]
        seats.write_proc(project, "claude-1", launcher=victim.pid)  # a recorded process, no validated session
        diag.seat_recover(project, "claude-1", kill=True)
        victim.wait(10)
        assert sibling and all(os.path.exists(f"/proc/{p}") for p in sibling)
    finally:
        for pid in __import__("kankan.procutil", fromlist=["x"]).pids_in_session(victim.pid):
            os.kill(pid, 9)
        if victim.poll() is None:
            victim.kill()


def test_launcher_records_supervisor_only_when_validated(kproject, monkeypatch):
    monkeypatch.delenv("KANKAN_SUPERVISOR_PID", raising=False)
    from kankan.launcher import _supervisor_pid
    assert _supervisor_pid() is None
    monkeypatch.setenv("KANKAN_SUPERVISOR_PID", str(os.getppid()))
    assert _supervisor_pid() == os.getppid()


# 2 -----------------------------------------------------------------------------------------
def _hold(path, ready, seconds):
    with file_lock(path, timeout=5):
        ready.set()
        time.sleep(seconds)


def test_block_journal_serialized(conflicted, monkeypatch):  # noqa: F811
    project, fake, card, codex, ctx = conflicted
    monkeypatch.setattr(integrate, "BLOCK_LOCK_TIMEOUT", 0.5)
    lock = project.paths.seats_dir / "codex-1.block.lock"
    mp = multiprocessing.get_context("fork")
    ready = mp.Event()
    holder = mp.Process(target=_hold, args=(lock, ready, 3))
    holder.start()
    ready.wait(10)
    with pytest.raises(KankanError) as exc:
        integrate.block(ctx, "stuck")
    assert exc.value.status == Status.NEEDS_ACTION
    assert gitutil.rev(ctx.worktree, "MERGE_HEAD") is not None
    holder.join()


# 3 -----------------------------------------------------------------------------------------
def test_permission_error_not_contention(git_repo, monkeypatch):
    store = refstore.RefStore(git_repo, "main")
    oid = store.write_blob({"schema": 1})
    real = gitutil.run

    def fake_run(repo, args, **kw):
        if args[:2] == ["update-ref", "--stdin"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: cannot lock ref 'refs/heads/main': Unable to "
                                               "create '/x/refs/heads/main.lock': Permission denied")
        return real(repo, args, **kw)
    monkeypatch.setattr(gitutil, "run", fake_run)
    with pytest.raises(KankanError) as exc:
        store.transaction([refstore.Create(card_ref("a" * 24), oid)])
    assert exc.value.status == Status.INTERNAL_ERROR


def test_lock_file_contention_is_mismatch(git_repo):
    store = refstore.RefStore(git_repo, "main")
    oid = store.write_blob({"schema": 1})
    lock = git_repo / ".git" / "refs" / "agent-cards"
    lock.mkdir(parents=True)
    (lock / ("a" * 24 + ".lock")).write_text("")
    assert store.transaction([refstore.Create(card_ref("a" * 24), oid)]) is False


# 4 -----------------------------------------------------------------------------------------
def test_converge_does_not_replay_after_confirmed_move(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = fake.add_card(project.cfg.backlog, "c")
    lease = seats.acquire(project, "claude")
    workflow.tick(project, lease)
    fake.user_move(card["id"], project.cfg.backlog)
    assert cards.converge(project, lease) == [f"human_moved:{card['id']}"]
    assert fake.card_list(card["id"]) == project.cfg.backlog


# 5 -----------------------------------------------------------------------------------------
def test_done_refused_after_human_move(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card = fake.add_card(project.cfg.backlog, "c")
    lease = seats.acquire(project, "claude")
    workflow.tick(project, lease)
    ctx = agent_ctx(project, lease, monkeypatch)
    commit(ctx.worktree, "a.txt")
    ideas = fake.add_list(project.cfg.board_id, "Ideas")
    fake.user_move(card["id"], ideas["id"])
    with pytest.raises(KankanError) as exc:
        agentcmds.done(ctx)
    assert exc.value.status == Status.INELIGIBLE
    assert fake.card_list(card["id"]) == ideas["id"]
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "impl"


def test_integrate_refused_for_archived_card(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    before = git(project.paths.repo, "rev-parse", "main")
    fake.user_archive(card["id"])
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    assert exc.value.status == Status.INELIGIBLE
    assert git(project.paths.repo, "rev-parse", "main") == before


def test_done_review_does_not_move_human_moved_card(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    ideas = fake.add_list(project.cfg.board_id, "Ideas")
    fake.user_move(card["id"], ideas["id"])
    integrate.done_review(ctx)
    assert fake.card_list(card["id"]) == ideas["id"]


# 6 -----------------------------------------------------------------------------------------
def replace_seat(project, seat, old_oid):
    other = project.refs.write_blob({"schema": 1, "kind": "seat", "session": "admin"})
    assert project.refs.update(seat_ref(seat), other, old_oid)


def test_done_review_after_seat_replaced_not_owner(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    replace_seat(project, "codex-1", ctx.lease.seat_oid)
    with pytest.raises(KankanError) as exc:
        integrate.done_review(ctx)
    assert exc.value.status == Status.NOT_OWNER
    assert fake.card_list(card["id"]) != project.cfg.done
    assert project.refs.read(card_ref(card["id"])) is not None


def test_half_applied_repair_checks_seat(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    tested = git(ctx.worktree, "rev-parse", "HEAD")
    repo = project.paths.repo
    old = git(repo, "rev-parse", "main")
    tree = git(repo, "rev-parse", f"{tested}^{{tree}}")
    msg = f"t\n\nu\n\nKankan-Card: {card['shortLink']}\nKankan-Tested: {tested}\n"
    merge = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-p", old, "-p", tested, "-F", "-"],
                           input=msg, capture_output=True, text=True, check=True).stdout.strip()
    git(repo, "update-ref", "refs/heads/main", merge, old)
    replace_seat(project, "codex-1", ctx.lease.seat_oid)
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, tested)
    assert exc.value.status == Status.NOT_OWNER
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "review"


# 7 -----------------------------------------------------------------------------------------
def test_pending_answer_for_other_card_dropped(waiting):  # noqa: F811
    project, fake, card, launcher, tmux = waiting
    update_state(project.paths, "claude-1", lambda s: setattr(s, "pending_forward", {
        "card": "f" * 24, "task_id": "old", "text": "stale answer", "status": "queued", "cursor": "x"}))
    launcher.step()
    assert not any("stale answer" in p for p in tmux.pastes)
    assert load_state(project.paths, "claude-1").pending_forward is None


# 8 -----------------------------------------------------------------------------------------
def test_launcher_exits_when_lease_revoked(kproject):
    project = kproject
    lease = seats.acquire(project, "claude")
    tmux = FakeTmux()
    launcher = make_launcher(project, lease, tmux, None)
    launcher.state = "idle"
    project.cfg.poll_seconds = 3600
    launcher.next_tick = 10 ** 12
    assert launcher.step() is False
    diag.seat_recover(project, "claude-1", kill=False)
    launcher.next_lease_check = 0.0
    assert launcher.step() is True


# 9 -----------------------------------------------------------------------------------------
def test_hold_while_waiting_human_suspends(waiting):  # noqa: F811
    project, fake, card, launcher, tmux = waiting
    fake.user_label(card["id"], project.cfg.label_hold)
    fake.user_comment(card["id"], "an answer")
    launcher.next_board_check = 0.0
    launcher.step()
    launcher.step()
    assert any("must be suspended" in p for p in tmux.pastes)
    assert not any("an answer" in p for p in tmux.pastes)
    assert launcher.state == "suspending"
