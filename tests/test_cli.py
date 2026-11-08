import json
import os

import pytest

from kankan import cli, faults, seats, workflow
from kankan.seatstate import load_state
from tests.conftest import git


@pytest.fixture
def agent(kproject, fake_trello, monkeypatch):
    project = kproject
    card = fake_trello.add_card(project.cfg.backlog, "Feature")
    lease = seats.acquire(project, "claude")
    task = workflow.tick(project, lease)
    wt = project.paths.seat_worktree(lease.seat)
    monkeypatch.setenv("KANKAN_SEAT", lease.seat)
    monkeypatch.setenv("KANKAN_SESSION", lease.session)
    monkeypatch.chdir(wt)
    return project, fake_trello, lease, task, wt, card


def run(capsys, *argv):
    code = cli.main(list(argv) + ["--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


def commit(wt, name="f.txt"):
    (wt / name).write_text("x\n")
    git(wt, "add", name)
    git(wt, "commit", "-qm", "work")


def test_task_fields(agent, capsys):
    project, fake, lease, task, wt, card = agent
    code, out = run(capsys, "task")
    assert code == 0 and out["ok"]
    assert set(out["data"]) == {"task_id", "kind", "card_id", "short", "title", "url", "desc", "phase", "branch",
                                "worktree", "main", "dirty_files", "resumed", "hold", "instructions"}
    assert out["data"]["title"] == "Feature" and out["data"]["task_id"] == task["id"]


def test_done_needs_commit(agent, capsys):
    code, out = run(capsys, "done")
    assert code == 14 and out["status"] == "needs_action" and "commit" in out["message"]


def test_done_dirty_lists_files(agent, capsys):
    project, fake, lease, task, wt, card = agent
    commit(wt)
    (wt / "a.txt").write_text("dirty")
    code, out = run(capsys, "done")
    assert code == 14 and out["data"]["files"] == ["a.txt"]


def test_done_moves_to_implemented_and_detaches(agent, capsys):
    project, fake, lease, task, wt, card = agent
    commit(wt)
    code, out = run(capsys, "done")
    assert code == 0, out
    assert fake.card_list(card["id"]) == project.cfg.seats["claude-1"].implemented
    assert git(wt, "symbolic-ref", "-q", "HEAD", check=False) == ""
    state = load_state(project.paths, "claude-1")
    assert state.signal == {"task_id": task["id"], "kind": "done", "payload": {}}


def test_done_rerun_after_crash_is_success(agent, capsys, monkeypatch):
    project, fake, lease, task, wt, card = agent
    commit(wt)
    monkeypatch.setenv("KANKAN_FAULT", "after_ref_tx")
    with pytest.raises(faults.SimulatedCrash):
        cli.main(["done", "--json"])
    capsys.readouterr()
    monkeypatch.delenv("KANKAN_FAULT")
    code, out = run(capsys, "done")
    assert code == 0, out
    assert fake.card_list(card["id"]) == project.cfg.seats["claude-1"].implemented
    assert git(wt, "symbolic-ref", "-q", "HEAD", check=False) == ""


def test_note_comments(agent, capsys):
    project, fake, lease, task, wt, card = agent
    code, _ = run(capsys, "note", "halfway there")
    assert code == 0
    assert any("halfway there" in c for c in fake.card_comments(card["id"]))


def test_agent_cmd_without_env_usage_error(monkeypatch, capsys, git_repo):
    monkeypatch.chdir(git_repo)
    code, out = run(capsys, "task")
    assert code == 41 and out["status"] == "usage_error"


def test_agent_cmd_wrong_session_not_owner(agent, capsys, monkeypatch):
    monkeypatch.setenv("KANKAN_SESSION", "0" * 32)
    code, out = run(capsys, "task")
    assert code == 31


def test_json_envelope_shape(agent, capsys):
    code, out = run(capsys, "task")
    assert list(out) == ["ok", "status", "message", "data"]
    code, out = run(capsys, "done")
    assert list(out) == ["ok", "status", "message", "data"] and out["ok"] is False


def test_unknown_command_usage_error(capsys):
    assert cli.main(["nonsense", "--json"]) == 41
