"""seat F§2: `kankan seat start` and `kankan seat stop`; `kankan stop` takes no seat any more; flag combinations."""

import json

import pytest

from kankan import agents_cmd, cli, seats
from kankan.config import save_config
from kankan.errors import Status
from kankan.seatstate import load_state
from tests.helpers import hold


def run(capsys, *argv):
    code = cli.main(list(argv) + ["--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def _register(project, *names):
    for name in names:
        seats.release(project, seats.acquire(project, seats.seat_type(name), seat=name))
    project.reload_config()


@pytest.fixture
def here(kproject, monkeypatch):
    monkeypatch.chdir(kproject.paths.repo)
    return kproject


@pytest.fixture
def started(here, monkeypatch):
    calls = []

    def agent(project, agent_type, seat=None, attach=False, tmux=None, preflight=True, qa=False, create=True):
        calls.append((agent_type, seat, qa, create, attach))
        return f"ai-proj-{agent_type}-starting-abc"
    monkeypatch.setattr(agents_cmd, "agent", agent)
    monkeypatch.setattr(agents_cmd, "_wait_started", lambda project, session, what, started: None)
    return calls


def test_stop_with_seat_is_usage_error(here):
    assert cli.main(["stop", "claude-1"]) == Status.USAGE_ERROR.exit_code


def test_seat_stop_sets_stop_requested(here, capsys):
    _register(here, "claude-1")
    code, out = run(capsys, "seat", "stop", "claude-1")
    assert code == 0 and load_state(here.paths, "claude-1").stop_requested


def test_seat_start_unknown_creates_nothing(here, started, capsys):
    code, out = run(capsys, "seat", "start", "claude-9")
    assert out["status"] == "usage_error" and "kankan agent" in out["message"] and "kankan up" in out["message"]
    assert started == [] and "claude-9" not in here.reload_config().seats


def test_seat_start_running_reports_already_running(here, started, holders, capsys):
    holders.append(hold(here, "claude-1", mode="ignore"))
    code, out = run(capsys, "seat", "start", "claude-1")
    assert code == 0 and out["data"]["started"] is False and started == []


def test_seat_start_open_session_names_recover(here, started, monkeypatch, capsys):
    _register(here, "claude-1")
    monkeypatch.setattr("kankan.tmux.Tmux.has_session", lambda self, name: True)
    code, out = run(capsys, "seat", "start", "claude-1")
    assert out["status"] == "needs_action" and "kankan seat recover claude-1 --kill" in out["message"]
    assert started == []


def test_seat_start_type_from_name(here, started, capsys, monkeypatch):
    monkeypatch.setattr("kankan.tmux.Tmux.has_session", lambda self, name: False)
    _register(here, "claude-2")
    cfg = here.reload_config()
    from kankan.config import SeatConfig
    cfg.seats["codex-qa"] = SeatConfig(type="codex", role="qa")
    save_config(here.paths, cfg)
    assert run(capsys, "seat", "start", "claude-2")[0] == 0
    assert run(capsys, "seat", "start", "codex-qa")[0] == 0
    assert started == [("claude", "claude-2", False, False, False), ("codex", "codex-qa", True, False, False)]


def test_seat_start_waits_and_reports_failed_start(here, started, monkeypatch, capsys):
    from kankan.errors import KankanError
    monkeypatch.setattr("kankan.tmux.Tmux.has_session", lambda self, name: False)
    _register(here, "claude-1")

    def fail(project, session, what, started_):
        raise KankanError(Status.INTERNAL_ERROR, "did not occupy a seat")
    monkeypatch.setattr(agents_cmd, "_wait_started", fail)
    code, out = run(capsys, "seat", "start", "claude-1")
    assert out["status"] == "internal_error" and "did not occupy" in out["message"]


def test_seat_start_attach_passed(here, started, monkeypatch, capsys):
    monkeypatch.setattr("kankan.tmux.Tmux.has_session", lambda self, name: False)
    _register(here, "claude-1")
    assert run(capsys, "seat", "start", "claude-1", "--attach")[0] == 0
    assert started[-1][-1] is True


def test_seat_start_immediate_removing_refused(here, started, capsys):
    _register(here, "claude-1")
    cfg = here.reload_config()
    cfg.seats["claude-1"].removing = "immediate"
    save_config(here.paths, cfg)
    code, out = run(capsys, "seat", "start", "claude-1")
    assert out["status"] == "usage_error" and "kankan seat remove claude-1" in out["message"] and started == []


@pytest.mark.parametrize("argv", [["start", "claude-1", "--kill"], ["stop", "claude-1", "--yes"],
                                  ["recover", "claude-1", "--graceful"], ["remove", "claude-1", "--attach"],
                                  ["stop", "claude-1", "--attach"]])
def test_seat_flag_combinations(here, capsys, argv):
    _register(here, "claude-1")
    code, out = run(capsys, "seat", *argv)
    assert out["status"] == "usage_error"
