import json
import os
from pathlib import Path

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
                                "worktree", "main", "integration_branch", "dirty_files", "resumed", "hold", "instructions", "attachments",
                                "comments", "handoff", "card_instructions", "card_instructions_error"}
    assert out["data"]["attachments"] == [] and out["data"]["handoff"] is None
    assert [c["role"] for c in out["data"]["comments"]] == ["kankan"]  # the claim notice
    assert out["data"]["title"] == "Feature" and out["data"]["task_id"] == task["id"]


def test_task_text_lists_attachments(agent, capsys):
    project, fake, lease, task, wt, card = agent
    fake.upload(card["id"], "spec.txt", b"spec\n", "text/plain")
    fake.add_attachment(card["id"], "https://example.com/spec.html", name="spec")
    assert cli.main(["task"]) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("attachments: "))
    files = json.loads(line[len("attachments: "):])
    assert [(e["kind"], e["status"]) for e in files] == [("file", "ok"), ("link", "ok")]
    assert Path(files[0]["path"]).read_bytes() == b"spec\n"


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


def test_agent_commands_without_environment(agent, capsys, monkeypatch):
    """Codex runs commands through a shared daemon that does not inherit the agent's environment (adapter notes):
    agent commands fall back to the seat file the launcher writes in the worktree's git directory."""
    project, fake, lease, task, wt, card = agent
    monkeypatch.delenv("KANKAN_SEAT")
    monkeypatch.delenv("KANKAN_SESSION")
    code, out = run(capsys, "task")
    assert code == 41 and "KANKAN_SEAT" in out["message"]
    seats.write_agent_file(project, lease.seat, lease.session, wt)
    gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir"))
    assert (gitdir / "kankan-agent.json").exists() and not (wt / "kankan-agent.json").exists()
    code, out = run(capsys, "task")
    assert code == 0 and out["data"]["task_id"] == task["id"]


def test_agent_file_with_old_session_is_not_owner(agent, capsys, monkeypatch):
    project, fake, lease, task, wt, card = agent
    monkeypatch.delenv("KANKAN_SEAT")
    monkeypatch.delenv("KANKAN_SESSION")
    seats.write_agent_file(project, lease.seat, "stale-session", wt)
    code, out = run(capsys, "task")
    assert code == 31 and out["status"] == "not_owner"


def test_agent_command_refuses_other_kankan_code(agent, capsys, monkeypatch):
    """The launcher records the kankan code it runs; an agent command running different code refuses."""
    from kankan import build
    project, fake, lease, task, wt, card = agent
    seats.write_agent_file(project, lease.seat, lease.session, wt)
    code, out = run(capsys, "task")
    assert code == 0
    monkeypatch.setattr(build, "code_id", lambda: "0" * 12)
    code, out = run(capsys, "task")
    assert code == 40 and out["status"] == "config_error" and "Restart the agents" in out["message"]
    # Same version: the message adds the hashes, the only difference left.
    assert f"kankan {build.__version__} from {build.PACKAGE}" in out["message"] and "(code 000000000000)" in out["message"]
    assert out["data"]["command"]["code"] == "0" * 12 and out["data"]["launcher"]["package"] == str(build.PACKAGE)


def test_code_mismatch_names_versions_and_directories(agent, capsys, monkeypatch):
    """A launcher importing another copy of kankan (0.1.6 checkout, 0.1.5 installed): versions and directories."""
    from kankan import build
    project, fake, lease, task, wt, card = agent
    monkeypatch.setattr(build, "identity", lambda: {"code": "1" * 12, "version": "0.1.6", "package": "/repo/kankan"})
    seats.write_agent_file(project, lease.seat, lease.session, wt)
    monkeypatch.setattr(build, "identity", lambda: {"code": "2" * 12, "version": "0.1.5", "package": "/site/kankan"})
    code, out = run(capsys, "task")
    assert code == 40 and "launcher runs kankan 0.1.6 from /repo/kankan, this command runs kankan 0.1.5 from " \
        "/site/kankan." in out["message"] and "code 1" not in out["message"]


def test_code_id_changes_with_the_code(tmp_path, monkeypatch):
    from kankan import build
    pkg = tmp_path / "kankan"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    first = build.code_id(pkg)
    assert first == build.code_id(pkg) and len(first) == 12
    (pkg / "a.py").write_text("x = 2\n")
    assert build.code_id(pkg) != first


# --------------------------------------------------------------------- Q&A commands

@pytest.fixture
def qa_agent(kproject, fake_trello, monkeypatch):
    from kankan import qa
    from kankan.adapters import ModelEffort
    from tests.helpers import add_qa_list
    project = kproject
    qa_list = add_qa_list(project, fake_trello)
    lease = seats.acquire_qa(project, "claude", None)
    card = fake_trello.add_card(qa_list, "Question")
    task = qa.claim(project, lease, project.trello.card(card["id"]), ModelEffort("sonnet", "high"))
    token = qa.start_turn(project, lease, task, qa.Inputs())
    wt = project.paths.seat_worktree(lease.seat)
    monkeypatch.setenv("KANKAN_SEAT", lease.seat)
    monkeypatch.setenv("KANKAN_SESSION", lease.session)
    monkeypatch.chdir(wt)
    return project, fake_trello, card, token


def test_answer_with_text(qa_agent, capsys):
    project, fake, card, token = qa_agent
    code, out = run(capsys, "answer", "--turn", token, "It works like this.")
    assert code == 0 and out["ok"] and out["data"]["comments"]
    assert any(c.startswith("It works like this.") for c in fake.card_comments(card["id"]))


def test_handoff_with_file(qa_agent, capsys, tmp_path):
    project, fake, card, token = qa_agent
    summary = tmp_path / "summary.md"
    summary.write_text("Implement X — with ü.\n", encoding="utf-8")
    code, out = run(capsys, "handoff", "--turn", token, "--file", str(summary))
    assert code == 0 and out["data"]["list"] == project.cfg.backlog
    assert any(c.startswith("Implement X — with ü.") for c in fake.card_comments(card["id"]))
    assert fake.card_list(card["id"]) == project.cfg.backlog


@pytest.mark.parametrize("argv", [
    ["answer", "text"],  # no --turn
    ["answer", "--turn", "t"],  # neither text nor --file
    ["answer", "--turn", "t", "text", "--file", "x.md"],  # both
    ["handoff", "--turn", "t", "--file", "/nonexistent/summary.md"],
])
def test_answer_and_handoff_arguments(qa_agent, capsys, argv):
    code, out = run(capsys, *argv)
    assert code == 41 and out["status"] == "usage_error"


@pytest.mark.parametrize("command", ["answer", "handoff"])
def test_answer_and_handoff_error_precedence_before_their_arguments(qa_agent, capsys, command):
    """no_work, then not_owner, come before the validation of the text arguments (usage_error)."""
    from kankan.refstore import card_ref
    from kankan.seatstate import update_state
    project, fake, card, token = qa_agent
    value = project.refs.read(card_ref(card["id"]))
    assert project.refs.delete(card_ref(card["id"]), value.oid)  # the conversation ended
    code, out = run(capsys, command, "--turn", token, "text", "--file", "/nonexistent/x.md")
    assert out["status"] == "not_owner"
    update_state(project.paths, "claude-qa", lambda state: setattr(state, "task", None))
    code, out = run(capsys, command, "--turn", token, "text", "--file", "/nonexistent/x.md")
    assert out["status"] == "no_work"


def test_answer_in_an_implementation_task_is_usage_error(agent, capsys):
    code, out = run(capsys, "answer", "--turn", "t", "text")
    assert code == 41 and out["status"] == "usage_error"


def test_done_in_a_qa_task_is_usage_error(qa_agent, capsys):
    code, out = run(capsys, "done")
    assert code == 41 and out["status"] == "usage_error"


def test_fail_with_a_stale_turn(qa_agent, capsys):
    code, out = run(capsys, "fail", "--turn", "0123456789abcdef", "broken")
    assert out["status"] == "needs_action"
    project, fake, card, token = qa_agent
    code, out = run(capsys, "fail", "--turn", token, "broken")
    assert code == 0 and out["ok"]


# ---------------------------------------------------------------------- agent --qa, up and autostart with qa=<type>

@pytest.fixture
def calls(kproject, monkeypatch):
    from kankan import agents_cmd
    recorded = {}
    monkeypatch.setattr(agents_cmd, "agent", lambda project, agent_type, seat=None, attach=False, tmux=None,
                        preflight=True, qa=False: recorded.setdefault("agent", (agent_type, seat, qa)) and "s")
    monkeypatch.setattr(agents_cmd, "up", lambda project, wanted, tmux=None, qa=None:
                        recorded.setdefault("up", (wanted, qa)) and [])
    monkeypatch.setattr(agents_cmd, "autostart_enable", lambda project, wanted, qa=None:
                        recorded.setdefault("autostart", (wanted, qa)) and [])
    monkeypatch.chdir(kproject.paths.repo)
    return recorded


def test_agent_qa_flag(calls, capsys):
    code, out = run(capsys, "agent", "claude", "--qa")
    assert code == 0 and calls["agent"] == ("claude", None, True)


def test_up_with_qa(calls, capsys):
    code, out = run(capsys, "up", "claude=2", "codex=1", "qa=codex")
    assert code == 0 and calls["up"] == ({"claude": 2, "codex": 1}, "codex")


def test_up_with_qa_only(calls, capsys):
    code, out = run(capsys, "up", "qa=claude")
    assert code == 0 and calls["up"] == ({}, "claude")


def test_up_without_arguments_starts_every_registered_seat(kproject, calls, capsys):
    from kankan.config import SeatConfig, save_config
    cfg = kproject.reload_config()
    cfg.seats.update({"claude-1": SeatConfig("claude"), "claude-2": SeatConfig("claude"),
                      "codex-1": SeatConfig("codex"), "codex-qa": SeatConfig("codex", role="qa")})
    save_config(kproject.paths, cfg)
    code, out = run(capsys, "up")
    assert code == 0 and calls["up"] == ({"claude": 2, "codex": 1}, "codex")
    assert out["data"]["up"] == "claude=2 codex=1 qa=codex"


def test_up_without_arguments_or_seats_prints_the_commands(calls, capsys):
    code, out = run(capsys, "up")
    assert out["status"] == "needs_action" and "up" not in calls
    assert "no seats registered" in out["message"] and "kankan up claude=<n>" in out["message"]
    assert "kankan autostart enable" in out["message"] and "kankan init" not in out["message"]


def test_up_without_arguments_in_an_unbound_clone_asks_for_init(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    code, out = run(capsys, "up")
    assert out["status"] == "needs_action"
    assert "kankan init <board-url>" in out["message"] and "kankan up claude=<n>" in out["message"]


def test_autostart_enable_with_qa(calls, capsys):
    code, out = run(capsys, "autostart", "enable", "claude=2", "qa=codex")
    assert code == 0 and calls["autostart"] == ({"claude": 2}, "codex")


@pytest.mark.parametrize("argv", [["up", "qa=gpt"], ["up", "qa="], ["up", "qa=claude", "qa=codex"]])
def test_up_with_a_bad_qa_argument_is_usage_error(calls, capsys, argv):
    code, out = run(capsys, *argv)
    assert out["status"] == "usage_error" and "up" not in calls


def test_task_shows_an_edited_comment_edited_with_the_cache_on(agent, capsys, monkeypatch):
    """Board reader functional test 7: the next ``kankan task`` reads the thread live, so a comment edited after
    another read cached the thread is shown edited."""
    from kankan.boardreader import max_age
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE")
    project, fake, lease, task, wt, card = agent
    comment = fake.user_comment(card["id"], "original question")
    assert any(c.text == "original question" for c in project.board.thread(card["id"], max_age(600)))  # cached
    fake.user_edit_comment(comment["id"], "edited question")
    code, out = run(capsys, "task")
    texts = [c["text"] for c in out["data"]["comments"]]
    assert code == 0 and "edited question" in texts and "original question" not in texts
