"""seat F§3.3 with real launchers in a private tmux server: a seat removed gracefully finishes its card, then
removes itself; after a stop it is restarted by `kankan up`; Ctrl-C of the wait leaves the removal to the launcher."""

import json

import pytest

from kankan import agents_cmd, clock, seatremove, stopall
from kankan.errors import KankanError
from kankan.refstore import card_ref
from tests.helpers import bind_project
from tests.launcher_helpers import launcher_state, wait_for

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=5)
# re-runnable: an agent restarted on the same card commits again (a new line) and finishes
COMMIT_AND_DONE = "date +%s%N >> f.txt && git add f.txt && git commit -qm work && kankan done --json"


@pytest.fixture
def stack(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"steps": [{"sleep": 8, "shell": COMMIT_AND_DONE}, {"sleep": 120}]}))
    monkeypatch.setenv("FAKE_AGENT_SCENARIO", str(scenario))
    return bind_project(git_repo, fake_trello, monkeypatch, **FAST), fake_trello, tmux_server


def _remove_graceful(project, **kwargs):
    return seatremove.remove(project, "claude-1", graceful=True, confirm=lambda text: False, say=lambda text: None,
                             poll=0.2, **kwargs)


def test_removing_seat_finishes_card_then_removes_itself(stack):
    project, fake, server = stack
    card = fake.add_card(project.cfg.backlog, "c")
    later = fake.add_card(project.cfg.backlog, "later")
    agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    report = _remove_graceful(project)
    assert report["seat"] == "claude-1" and "claude-1" not in project.reload_config().seats
    meta = project.refs.read(card_ref(card["id"])).meta
    assert meta["phase"] == "parked" and meta["cause"] == "seat_removed"  # no work seat left to review it
    assert project.refs.read(card_ref(later["id"])) is None  # never taken: the seat took no new card
    assert wait_for(lambda: not [s for s in server.sessions() if s.startswith("ai-proj-")], timeout=20)


def test_ctrl_c_then_autonomous_completion(stack, monkeypatch):
    project, fake, server = stack
    fake.add_card(project.cfg.backlog, "c")
    agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    real = clock.sleep

    def interrupt(seconds):
        monkeypatch.setattr(clock, "sleep", real)
        raise KeyboardInterrupt
    monkeypatch.setattr(clock, "sleep", interrupt)
    with pytest.raises(KankanError):
        _remove_graceful(project)
    assert wait_for(lambda: "claude-1" not in project.reload_config().seats, timeout=60)


def test_removing_seat_restarted_by_up_completes(stack, tmp_path, monkeypatch):
    project, fake, server = stack
    flag = tmp_path / "go"  # the agent works on its card until the flag exists: killed mid-card for sure
    scenario = tmp_path / "flag-scenario.json"
    scenario.write_text(json.dumps({"steps": [
        {"shell": f"while [ ! -f {flag} ]; do sleep 0.2; done; {COMMIT_AND_DONE}"}, {"sleep": 120}]}))
    monkeypatch.setenv("FAKE_AGENT_SCENARIO", str(scenario))
    card = fake.add_card(project.cfg.backlog, "c")
    agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    stopall.stop_all(project, confirm=lambda text: True, say=lambda text: None)  # killed in the middle of the card
    assert project.refs.read(card_ref(card["id"])).meta["owner"] == "claude-1"
    flag.write_text("")
    report = _remove_graceful(project)  # stopped seat with work: started again, finishes, removed
    assert report["seat"] == "claude-1" and "claude-1" not in project.reload_config().seats
    assert project.refs.read(card_ref(card["id"])).meta["cause"] == "seat_removed"
