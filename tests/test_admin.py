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


def test_seat_remove_of_a_free_seat_archives_its_lists(board):
    from tests.helpers import remove_seat
    project, fake = board
    other = seats.acquire(project, "claude")
    seats.release(project, other)
    lists = project.reload_config().seats["claude-1"]
    remove_seat(project, "claude-1")
    assert "claude-1" not in project.reload_config().seats
    assert fake.state["lists"][lists.review]["closed"] is True
    assert not project.paths.seat_worktree("claude-1").exists()


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


def test_seat_remove_of_the_qa_seat(board):
    from kankan import attachments, qa
    from kankan.adapters import ModelEffort
    from tests.helpers import add_qa_list, remove_seat
    project, fake = board
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "claude", None)
    card = fake.add_card(qa_list, "question")
    task = qa.claim(project, lease, project.trello.card(card["id"]), ModelEffort("sonnet", "high"))
    att_dir = attachments.card_dir(project.paths, card["shortLink"])
    att_dir.mkdir(parents=True)
    seats.release(project, lease)
    lists_before = {ident for ident, data in fake.state["lists"].items() if not data["closed"]}
    result = remove_seat(project, "claude-qa")
    assert result["seat"] == "claude-qa" and result["worktree_removed"] is True
    assert project.refs.read(card_ref(card["id"])) is None
    assert not att_dir.exists() and not qa.conversation_dir(project.paths, task["conversation"]).exists()
    assert {ident for ident, data in fake.state["lists"].items() if not data["closed"]} == lists_before
    assert "claude-qa" not in project.reload_config().seats
    assert not project.paths.seat_worktree("claude-qa").exists()
    assert fake.card_list(card["id"]) == qa_list


def test_seat_remove_of_a_dead_qa_seat(board):
    from kankan import qa
    from kankan.adapters import ModelEffort
    from tests.helpers import add_qa_list, remove_seat
    project, fake = board
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "claude", None)
    card = fake.add_card(qa_list, "question")
    task = qa.claim(project, lease, project.trello.card(card["id"]), ModelEffort("sonnet", "high"))
    seats._release_fd(lease.fd)  # the launcher died: its seat ref stays
    assert remove_seat(project, "claude-qa")["seat"] == "claude-qa"
    assert project.refs.read(card_ref(card["id"])) is None
    assert "claude-qa" not in project.reload_config().seats
    assert not qa.conversation_dir(project.paths, task["conversation"]).exists()