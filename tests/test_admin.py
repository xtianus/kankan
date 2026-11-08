import os
import subprocess
import sys
import time

import pytest

from kankan import diag, faults, integrate, seats, workflow
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref, seat_ref
from tests.conftest import ROOT, git
from tests.flow import review_setup


@pytest.fixture
def board(kproject, fake_trello):
    return kproject, fake_trello


def test_release_force_parks_without_move(board):
    project, fake = board
    card = fake.add_card(project.cfg.backlog, "c")
    lease = seats.acquire(project, "claude")
    workflow.tick(project, lease)
    in_progress = fake.card_list(card["id"])
    diag.release_force(project, card["shortLink"])
    meta = project.refs.read(card_ref(card["id"])).meta
    assert meta["phase"] == "parked" and meta["cause"] == "admin_release"
    assert fake.card_list(card["id"]) == in_progress


def race(monkeypatch, action):
    real = faults.point

    def point(name):
        if name == "before_integrate_tx":
            action()
        real(name)
    monkeypatch.setattr(faults, "point", point)


def test_integrate_racing_release_force(board, monkeypatch):
    project, fake = board
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    before = git(project.paths.repo, "rev-parse", "main")
    race(monkeypatch, lambda: diag.release_force(project, card["id"]))
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    assert exc.value.status in (Status.INELIGIBLE, Status.NOT_OWNER)
    assert git(project.paths.repo, "rev-parse", "main") == before


def test_integrate_racing_seat_recover(board, monkeypatch):
    project, fake = board
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    before = git(project.paths.repo, "rev-parse", "main")
    race(monkeypatch, lambda: diag.seat_recover(project, "codex-1", kill=False))
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    assert exc.value.status == Status.NOT_OWNER
    assert git(project.paths.repo, "rev-parse", "main") == before


HOLDER = """
import os, sys, time
sys.path.insert(0, {root!r})
os.environ["KANKAN_SUPERVISOR_PID"] = str(os.getpid())
from kankan import seats
from kankan.project import Project
lease = seats.acquire(Project.open({repo!r}), "claude", seat="claude-1")
print("held", flush=True)
time.sleep(1000)
"""


def test_seat_recover_kill(board):
    project, fake = board
    holder = subprocess.Popen([sys.executable, "-c", HOLDER.format(root=str(ROOT), repo=str(project.paths.repo))],
                              start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert seats.liveness(project, "claude-1") == "alive"
        diag.seat_recover(project, "claude-1", kill=True)
        holder.wait(10)
        assert seats.liveness(project, "claude-1") == "free"
        assert project.refs.read(seat_ref("claude-1")) is None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_seat_remove_preconditions(board):
    project, fake = board
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c")
    workflow.tick(project, lease)
    with pytest.raises(KankanError) as exc:
        diag.seat_remove(project, "claude-1")
    assert exc.value.status == Status.NEEDS_ACTION
    blockers = " ".join(exc.value.data["blockers"])
    assert "alive" in blockers and "holds cards" in blockers and "not empty" in blockers
    other = seats.acquire(project, "claude")
    seats.release(project, other)
    lists = project.reload_config().seats["claude-2"]
    diag.seat_remove(project, "claude-2")
    assert "claude-2" not in project.reload_config().seats
    assert fake.state["lists"][lists.review]["closed"] is True
    assert not project.paths.seat_worktree("claude-2").exists()


def test_purge_remote(board, tmp_path):
    project, fake = board
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(project.paths.repo, "remote", "add", "origin", str(remote))
    head = git(project.paths.repo, "rev-parse", "HEAD")
    git(project.paths.repo, "update-ref", "refs/agent-cards/" + "a" * 24, head)
    git(project.paths.repo, "push", "-q", "--no-verify", "origin", f"refs/agent-cards/{'a' * 24}")
    git(project.paths.repo, "push", "-q", "origin", "main")
    assert diag.purge_remote(project)["deleted"] == ["refs/agent-cards/" + "a" * 24]
    assert git(project.paths.repo, "ls-remote", "origin", "refs/agent-*") == ""
    assert git(project.paths.repo, "ls-remote", "origin", "main") != ""
