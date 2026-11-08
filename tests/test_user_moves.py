import json

import pytest

from kankan import agentcmds, cli, seats, workflow
from kankan.refstore import card_ref
from kankan.seatstate import update_state
from tests.conftest import git
from tests.flow import agent_ctx, commit, review_setup
from tests.test_ask import FakeTmux, make_launcher


@pytest.fixture
def working(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    project.cfg.poll_seconds = 0
    card = fake.add_card(project.cfg.backlog, "c")
    lease = seats.acquire(project, "claude")
    task = workflow.tick(project, lease)
    return project, fake, card, lease, task


def writes_after(fake, mark):
    return [r for r in fake.requests[mark:] if r[0] != "GET"]


def test_user_move_yields_and_card_stays(working, monkeypatch):
    project, fake, card, lease, task = working
    ideas = fake.add_list(project.cfg.board_id, "Ideas")
    tmux = FakeTmux()
    tmux.screen = "✶ Working… (3s · esc to interrupt)\n"
    launcher = make_launcher(project, lease, tmux, task)
    fake.user_move(card["id"], ideas["id"])
    launcher.step()
    launcher.step()
    assert sum("must be suspended" in p for p in tmux.pastes) == 1
    assert launcher.state == "suspending"
    ctx = agent_ctx(project, lease, monkeypatch)
    commit(ctx.worktree, "wip.txt")
    agentcmds.yield_(ctx)
    meta = project.refs.read(card_ref(card["id"])).meta
    assert meta["phase"] == "parked" and meta["cause"] == "user_moved" and meta["target_list"] == ideas["id"]
    assert fake.card_list(card["id"]) == ideas["id"]


def test_idle_seat_yields_moved_card_itself(working):
    project, fake, card, lease, task = working
    ideas = fake.add_list(project.cfg.board_id, "Ideas")
    fake.user_move(card["id"], ideas["id"])
    assert workflow.tick(project, lease) is None
    meta = project.refs.read(card_ref(card["id"])).meta
    assert meta["cause"] == "user_moved" and fake.card_list(card["id"]) == ideas["id"]


def run_task(capsys):
    code = cli.main(["task", "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_card_deleted_while_impl(working, monkeypatch, capsys):
    project, fake, card, lease, task = working
    ctx = agent_ctx(project, lease, monkeypatch)
    monkeypatch.chdir(ctx.worktree)
    fake.user_delete(card["id"])
    mark = len(fake.requests)
    code, out = run_task(capsys)
    assert code == 12
    update_state(project.paths, lease.seat, lambda s: s.task.__setitem__("suspend_cause", "user_moved"))
    agentcmds.yield_(agent_ctx(project, lease, monkeypatch))
    meta = project.refs.read(card_ref(card["id"])).meta
    assert meta["phase"] == "parked" and meta["cause"] == "user_deleted" and "target_list" not in meta
    assert writes_after(fake, mark) == []


def test_card_archived_while_review(kproject, fake_trello, monkeypatch, capsys):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    monkeypatch.chdir(ctx.worktree)
    fake.user_archive(card["id"])
    mark = len(fake.requests)
    code, out = run_task(capsys)
    assert code == 12
    assert workflow.tick(project, codex) is None  # the idle path yields it (clean worktree)
    meta = project.refs.read(card_ref(card["id"])).meta
    assert meta["phase"] == "parked" and meta["cause"] == "user_archived"
    assert writes_after(fake, mark) == []
