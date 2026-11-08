import json
import os
import signal
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from kankan import auditlog, cli, clock, labels, seats
from kankan.adapters import Classification, load_adapter
from kankan.config import write_registry_block
from kankan.launcher import Launcher
from kankan.refstore import card_ref
from kankan.seatstate import load_state, update_state
from tests.helpers import bind_project
from tests.launcher_helpers import (agent_events, launcher_state, pid_alive, proc_info, seat_session, start_agent,
                                    wait_for)

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=0.05)
COMMIT_AND_DONE = "echo x > f.txt && git add f.txt && git commit -qm work && kankan done --json"


@pytest.fixture
def chain(git_repo, fake_trello, monkeypatch, agent_path, tmux_server):
    project = bind_project(git_repo, fake_trello, monkeypatch, **FAST)
    return project, fake_trello, tmux_server


def card_labels(fake, card):
    return fake.state["cards"][card["id"]]["idLabels"]


def label_names(fake, card):
    return [fake.state["labels"][l]["name"] for l in card_labels(fake, card) if l in fake.state["labels"]]


def inputs(log):
    return [e["line"] for e in agent_events(log) if e["event"] == "input"]


def test_assignment_sends_new_conversation_then_prompt(chain):
    project, fake, tmux = chain
    card = fake.add_card(project.cfg.backlog, "c")
    _, log = start_agent(tmux, project, scenario={"steps": [{"shell": COMMIT_AND_DONE, "sleep": 0.5}]})
    assert wait_for(lambda: fake.card_list(card["id"]) == project.reload_config().seats["claude-1"].implemented, timeout=40)
    assert inputs(log)[:2] == ["/clear", "Run the current kankan task (kankan skill)."]
    assert wait_for(lambda: launcher_state(project, "claude-1") == "idle", timeout=20)
    assert load_state(project.paths, "claude-1").task is None


def test_codex_new_conversation_dialog_answered(chain):
    project, fake, tmux = chain
    card = fake.add_card(project.cfg.backlog, "c")
    _, log = start_agent(tmux, project, agent_type="codex", scenario={"steps": [{"shell": COMMIT_AND_DONE}]})
    assert wait_for(lambda: fake.card_list(card["id"]) == project.reload_config().seats["codex-1"].implemented, timeout=40)
    events = [e["event"] for e in agent_events(log)]
    assert "new_dialog_answer" in events


def test_limit_sets_resume_label_and_clears_on_work(chain):
    project, fake, tmux = chain
    card = fake.add_card(project.cfg.backlog, "c")
    start_agent(tmux, project, scenario={"steps": [
        {"screen": "limit", "limit_text": "  ⎿  You've hit your session limit · resets 11:58pm", "sleep": 6,
         "after_screen": "working", "after_sleep": 6}]})
    assert wait_for(lambda: any(n.startswith("Resume ") for n in label_names(fake, card)), timeout=40)
    assert wait_for(lambda: launcher_state(project, "claude-1") == "limited", timeout=5)
    assert wait_for(lambda: not any(n.startswith("Resume ") for n in label_names(fake, card)), timeout=20)


def test_resume_labels_gc(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = fake.add_card(project.cfg.backlog, "c")
    now = clock.now()
    old = fake.add_label(project.cfg.board_id, labels.resume_label_name(now - timedelta(hours=2)))
    unused = fake.add_label(project.cfg.board_id, labels.resume_label_name(now + timedelta(hours=2)))
    used = fake.add_label(project.cfg.board_id, "Resume " + (now + timedelta(hours=3)).astimezone().strftime("%Y-%m-%d %H:%M"))
    fake.user_label(card["id"], old["id"])
    fake.user_label(card["id"], used["id"])
    deleted = labels.gc_resume_labels(project)
    assert sorted(deleted) == sorted([old["id"], unused["id"]])
    assert used["id"] in fake.state["labels"]
    assert labels.resume_label(project, now + timedelta(hours=3)) == used["id"]


def test_ready_without_signal_sets_error(chain):
    project, fake, tmux = chain
    card = fake.add_card(project.cfg.backlog, "c")
    start_agent(tmux, project, scenario={"steps": [{"sleep": 0.2}]})
    assert wait_for(lambda: project.cfg.label_error in card_labels(fake, card), timeout=40)
    assert wait_for(lambda: any("without a signal" in c and "@kankanbot" in c for c in fake.card_comments(card["id"])))


def test_unknown_screen_no_write(chain):
    project, fake, tmux = chain
    card = fake.add_card(project.cfg.backlog, "c")
    _, log = start_agent(tmux, project, scenario={"steps": [{"screen": "unknown", "sleep": 12}]})
    assert wait_for(lambda: project.cfg.label_error in card_labels(fake, card), timeout=40)
    assert wait_for(lambda: any("not recognized" in c for c in fake.card_comments(card["id"])))
    assert len(inputs(log)) == 2  # only /clear and the prompt


def make_launcher(project, tmux_obj, task=None):
    launcher = Launcher(project, load_adapter(project.paths, "claude"), tmux_obj, "claude")
    launcher.lease = seats.acquire(project, "claude")
    launcher.session = "s"
    launcher.task = task
    return launcher


def test_attached_typing_defers_writes(kproject):
    project = kproject
    project.cfg.write_quiet_seconds = 20
    activity = [time.time()]
    fake_tmux = SimpleNamespace(client_activity=lambda session: activity[0])
    launcher = make_launcher(project, fake_tmux)
    ready = Classification("ready", "x")
    assert not launcher.can_write(ready)
    activity[0] = time.time() - 30
    assert launcher.can_write(ready)
    activity[0] = None
    assert launcher.can_write(ready)
    assert not launcher.can_write(Classification("working", "x"))
    assert launcher.can_write(Classification("working", "x"), queue=True)  # claude queues while working


def test_stale_task_signal_ignored(kproject):
    project = kproject
    launcher = make_launcher(project, SimpleNamespace(client_activity=lambda s: None), task={"id": "new", "card": "x"})
    update_state(project.paths, "claude-1", lambda s: setattr(s, "signal", {"task_id": "old", "kind": "done"}))
    assert launcher.take_signal() is None
    assert any(e["event"] == "stale_signal_ignored" for e in auditlog.read(project.paths))
    update_state(project.paths, "claude-1", lambda s: setattr(s, "signal", {"task_id": "new", "kind": "done"}))
    assert launcher.take_signal()["kind"] == "done"


def test_stop_after_current_task(chain, capsys, monkeypatch):
    project, fake, tmux = chain
    card = fake.add_card(project.cfg.backlog, "c")
    _, log = start_agent(tmux, project, scenario={"steps": [{"sleep": 5, "shell": COMMIT_AND_DONE}]})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    monkeypatch.chdir(project.paths.repo)
    assert cli.main(["stop", "claude-1"]) == 0
    assert wait_for(lambda: not tmux.has_session(seat_session(project, "claude-1")), timeout=40)
    assert fake.card_list(card["id"]) == project.reload_config().seats["claude-1"].implemented


def test_kill9_launcher_card_task_reassigned(chain):
    project, fake, tmux = chain
    card = fake.add_card(project.cfg.backlog, "c")
    _, log = start_agent(tmux, project, scenario={"steps": [{"sleep": 60}]})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    first = load_state(project.paths, "claude-1").task
    os.kill(proc_info(project, "claude-1")["launcher"]["pid"], signal.SIGKILL)
    assert wait_for(lambda: (load_state(project.paths, "claude-1").task or {}).get("id") not in (None, first["id"]),
                    timeout=40)
    task = load_state(project.paths, "claude-1").task
    assert task["card"] == card["id"] and task["kind"] == "impl" and task["resumed"] is True
    assert wait_for(lambda: inputs(log).count("Run the current kankan task (kankan skill).") == 2, timeout=30)


def test_registry_takeover_stops_running_launcher(chain):
    project, fake, tmux = chain
    start_agent(tmux, project)
    assert wait_for(lambda: launcher_state(project, "claude-1") == "idle", timeout=30)
    fake.state["boards"][project.cfg.board_id]["desc"] = write_registry_block("", {"registry-id": "other"})
    assert wait_for(lambda: not tmux.has_session(seat_session(project, "claude-1")), timeout=30)
    assert any(e["event"] == "tick_failed" and e["status"] == "config_error" for e in auditlog.read(project.paths))


def test_known_dialog_answered(chain):
    project, fake, tmux = chain
    (project.paths.kankan_dir / "agents.json").write_text(json.dumps(
        {"codex": {"known": [{"name": "model_switch", "all": True, "answer": ["3", "Enter"],
                              "patterns": ["switch to .{1,60} for lower credit usage\\?",
                                           "keep current model \\(never show again\\)"]},
                             {"name": "new", "all": True, "answer": ["Enter"],
                              "patterns": ["where should the new conversation run\\?"]}]}}))
    fake.add_card(project.cfg.backlog, "c")
    _, log = start_agent(tmux, project, agent_type="codex",
                         scenario={"steps": [{"screen": "known", "sleep": 0.2, "wait_input": True, "after_screen": "working",
                                              "after_sleep": 3}]})
    assert wait_for(lambda: "3" in inputs(log), timeout=40)
    time.sleep(3)
    assert inputs(log).count("3") == 1
