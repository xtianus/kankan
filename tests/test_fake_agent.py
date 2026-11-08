import json
import time

import pytest

from kankan.adapters import classify, load_adapter

pytestmark = pytest.mark.tmux


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize("agent_type", ["claude", "codex"])
def test_fake_agent_reaches_ready_and_runs_command(tmux_server, tmp_path, agent_path, agent_type):
    log = tmp_path / "agent.log"
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"trust": True, "log": str(log), "steps": [
        {"shell": "echo hi > out.txt", "screen": "working", "sleep": 1.5}]}))
    adapter = load_adapter(None, agent_type)
    tmux_server.new_session("fa", str(tmp_path), [agent_type, "--scenario", str(scenario)])
    pane = tmux_server.pane_of_session("fa")
    assert wait_for(lambda: classify(adapter, tmux_server.capture(pane)).state == "trust"), tmux_server.capture(pane)
    tmux_server.send_keys(pane, classify(adapter, tmux_server.capture(pane)).answer)
    assert wait_for(lambda: classify(adapter, tmux_server.capture(pane)).state == "ready")
    tmux_server.paste(pane, "do the task", "Enter")
    assert wait_for(lambda: classify(adapter, tmux_server.capture(pane)).state == "working")
    assert wait_for(lambda: (tmp_path / "out.txt").exists())
    assert wait_for(lambda: classify(adapter, tmux_server.capture(pane)).state == "ready")
    events = [json.loads(l)["event"] for l in log.read_text().splitlines()]
    assert events[0] == "start" and "input" in events and "shell" in events
