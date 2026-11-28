"""``kankan stop`` without a seat (stopall spec F§7)."""

import os
import sys

import pytest

from kankan import agents_cmd, cli, seats, stopall
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref
from kankan.seatstate import load_state, update_state
from tests.helpers import NoTmux, bind_project, hold
from tests.launcher_helpers import launcher_state, wait_for


@pytest.fixture
def systemctl(tmp_path, monkeypatch):
    """A stub ``systemctl``; write "fail" to ``mode`` to make it fail."""
    calls, mode, stub = tmp_path / "systemctl.log", tmp_path / "systemctl.mode", tmp_path / "stubbin"
    stub.mkdir()
    (stub / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {calls}\n'
                                    f'if [ "$(cat {mode} 2>/dev/null)" = fail ]; then echo boom >&2; exit 1; fi\n')
    (stub / "systemctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    return calls, mode


def install_unit(project, args="claude=2 codex=1 qa=claude"):
    path = agents_cmd.unit_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[Service]\nExecStart={sys.executable} -m kankan up {args}\n")
    return path


def run(project, **kwargs):
    asked, said = [], []
    kwargs.setdefault("confirm", lambda text: asked.append(text) or True)
    report = stopall.stop_all(project, say=said.append, tmux=NoTmux(), poll=0.05, **kwargs)
    return report, asked, said


def test_nothing_to_stop_asks_nothing(kproject):
    report, asked, _ = run(kproject)
    assert report["nothing_to_stop"] and asked == []
    assert "nothing to stop" in stopall.report_text(report)


def test_graceful_stop_disables_autostart_and_is_idempotent(kproject, holders, systemctl):
    unit = install_unit(kproject)
    holders += [hold(kproject, "claude-1"), hold(kproject, "codex-1")]
    report, asked, said = run(kproject, graceful=True)
    assert len(asked) == 1 and "claude-1" in asked[0] and "disable autostart" in asked[0]
    assert sorted(report["stopped"]) == ["claude-1", "codex-1"] and report["remaining"] == []
    assert report["autostart"] == "disabled" and not unit.exists()
    assert report["restart"] == ["kankan up claude=2 codex=1 qa=claude",
                                 "kankan autostart enable claude=2 codex=1 qa=claude"]
    assert any("without --graceful" in line for line in said)
    assert "kankan start" in stopall.report_text(report)
    for proc in holders:
        assert proc.wait(10) == 0
    again, asked, _ = run(kproject)
    assert again["nothing_to_stop"] and asked == []
    assert sorted(again["already_stopped"]) == ["claude-1", "codex-1"]
    # the rerun keeps the record of the first stop
    assert again["restart"] == report["restart"] and "kankan start" in stopall.report_text(again)


def test_stop_survives_a_launcher_clearing_the_request(kproject, holders):
    """A launcher restarted by its supervisor clears stop_requested at start: the wait asserts it again."""
    holders.append(hold(kproject, "claude-1"))
    original = stopall._request_stop
    calls = []

    def request_then_clear(project, seat):
        original(project, seat)
        calls.append(seat)
        if len(calls) == 1:  # the "restarted launcher" clears the first request
            update_state(project.paths, seat, lambda s: setattr(s, "stop_requested", False))
    stopall._request_stop = request_then_clear
    try:
        report, _, _ = run(kproject, graceful=True)
    finally:
        stopall._request_stop = original
    assert report["stopped"] == ["claude-1"] and len(calls) >= 2


def test_default_quits_immediately(kproject, fake_trello, holders):
    holders.append(hold(kproject, "claude-1", mode="ignore"))
    report, asked, said = run(kproject)
    assert "interrupting" in asked[0]
    assert report["stopped"] == ["claude-1"] and not report["graceful"]
    # the kill reaps its victims, so the holder's exit status may be gone: check that it has exited
    assert wait_for(lambda: holders[0].poll() is not None, timeout=10)
    assert seats.liveness(kproject, "claude-1") == "free"
    assert not any("without --graceful" in line for line in said)


def test_quit_keeps_the_claim_and_comments(kproject, fake_trello, holders):
    from kankan import workflow
    card = fake_trello.add_card(kproject.cfg.backlog, "c")
    lease = seats.acquire(kproject, "claude", seat="claude-1")
    workflow.tick(kproject, lease)
    seats.release(kproject, lease, retain=True)
    holders.append(hold(kproject, "claude-1", mode="ignore"))
    run(kproject)
    meta = kproject.refs.read(card_ref(card["id"])).meta
    assert meta["owner"] == "claude-1" and meta["phase"] == "impl"
    comments = fake_trello.card_comments(card["id"])
    assert any("`kankan stop` stopped seat claude-1" in text for text in comments)


def test_declined_confirmation_changes_nothing(kproject, holders):
    unit = install_unit(kproject)
    holders.append(hold(kproject, "claude-1", mode="ignore"))
    report, _, _ = run(kproject, confirm=lambda text: False)
    assert report == {"cancelled": True} and unit.exists()
    assert not load_state(kproject.paths, "claude-1").stop_requested
    assert seats.liveness(kproject, "claude-1") == "alive"


def test_systemctl_failure_is_reported_but_seats_stop(kproject, holders, systemctl):
    install_unit(kproject)
    systemctl[1].write_text("fail")
    holders.append(hold(kproject, "claude-1"))
    with pytest.raises(KankanError) as exc:
        run(kproject)
    assert exc.value.status == Status.NEEDS_ACTION
    data = exc.value.data
    assert data["stopped"] == ["claude-1"] and data["autostart"].startswith("error")
    assert any("autostart" in entry["what"] for entry in data["remaining"])


def test_cli_without_terminal_needs_yes(kproject, monkeypatch, capsys):
    install_unit(kproject)
    monkeypatch.chdir(kproject.paths.repo)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["stop"]) == Status.USAGE_ERROR.exit_code
    assert "--yes" in capsys.readouterr().err
    assert agents_cmd.unit_path(kproject).exists()
    assert cli.main(["stop", "claude-1", "--graceful"]) == Status.USAGE_ERROR.exit_code  # no seat argument any more


# ---------------------------------------------------------------------- kankan start
def test_start_restores_the_record(kproject, holders, systemctl, monkeypatch):
    install_unit(kproject, "claude=2 qa=codex")
    holders.append(hold(kproject, "claude-1", mode="ignore"))
    run(kproject)
    calls = {}
    monkeypatch.setattr(agents_cmd, "up", lambda project, wanted, qa=None: calls.setdefault("up", (wanted, qa)) and ["s1"])
    monkeypatch.setattr(agents_cmd, "autostart_enable",
                        lambda project, wanted, qa=None: calls.setdefault("autostart", (wanted, qa)) and [])
    report = stopall.start_all(kproject)
    assert calls == {"up": ({"claude": 2}, "codex"), "autostart": ({"claude": 2}, "codex")}
    assert report["autostart"] == "enabled" and stopall.load_record(kproject.paths) is None


def test_start_without_record_prints_the_commands(kproject, holders, monkeypatch, capsys):
    holders.append(hold(kproject, "claude-1", mode="ignore"))  # registers claude-1
    monkeypatch.chdir(kproject.paths.repo)
    assert cli.main(["start"]) == Status.NEEDS_ACTION.exit_code
    err = capsys.readouterr().err
    assert "no stop record" in err and "kankan up claude=1" in err and "kankan autostart enable claude=1" in err
    assert "kankan init" not in err


def test_start_in_an_unbound_clone_asks_for_init(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    assert cli.main(["start"]) == Status.NEEDS_ACTION.exit_code
    err = capsys.readouterr().err
    assert "kankan init <board-url>" in err and "kankan up claude=<n>" in err


# ---------------------------------------------------------------------- real launchers in tmux
FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=5)


@pytest.fixture
def tmux_project(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    scenario = tmp_path / "scenario.json"
    scenario.write_text('{"steps": [{"sleep": 120}]}')
    monkeypatch.setenv("FAKE_AGENT_SCENARIO", str(scenario))
    return bind_project(git_repo, fake_trello, monkeypatch, **FAST), tmux_server


@pytest.mark.tmux
@pytest.mark.slow
def test_launchers_stop_and_sessions_close(tmux_project):
    project, server = tmux_project
    agents_cmd.up(project, {"claude": 1, "codex": 1})
    assert wait_for(lambda: {"ai-proj-claude-1", "ai-proj-codex-1"} <= set(server.sessions()), timeout=15)
    report = stopall.stop_all(project, graceful=True, confirm=lambda text: True, say=lambda text: None, poll=0.2)
    assert sorted(report["stopped"]) == ["claude-1", "codex-1"] and report["remaining"] == []
    assert not [s for s in server.sessions() if s.startswith("ai-proj-")]
    assert report["restart"] == ["kankan up claude=1 codex=1"]
    started = stopall.start_all(project)
    assert len(started["started"]) == 2 and started["autostart"] == "unchanged"
    assert wait_for(lambda: {"ai-proj-claude-1", "ai-proj-codex-1"} <= set(server.sessions()), timeout=15)
    assert stopall.load_record(project.paths) is None


@pytest.mark.tmux
@pytest.mark.slow
def test_stop_quits_a_working_launcher(tmux_project, fake_trello):
    project, server = tmux_project
    card = fake_trello.add_card(project.cfg.backlog, "c")
    agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    report = stopall.stop_all(project, confirm=lambda text: True, say=lambda text: None)
    assert report["stopped"] == ["claude-1"] and report["remaining"] == []
    assert not [s for s in server.sessions() if s.startswith("ai-proj-")]
    assert project.refs.read(card_ref(card["id"])).meta["owner"] == "claude-1"


def record_launcher_code(project, seat, session=None):
    """The seat file a launcher writes when it starts its agent, for the seat ref's session by default."""
    session = session or project.refs.read(seats.seat_ref(seat)).meta["session"]
    seats.write_agent_file(project, seat, session, project.paths.seat_worktree(seat))


def test_graceful_refuses_agents_of_another_kankan(kproject, holders, systemctl, monkeypatch):
    """After an upgrade the agents' commands are refused, so they could never finish: --graceful refuses (F§2.2)."""
    from kankan import build
    unit = install_unit(kproject)
    holders += [hold(kproject, "claude-1"), hold(kproject, "codex-1")]
    record_launcher_code(kproject, "claude-1")
    monkeypatch.setattr(build, "code_id", lambda: "0" * 12)
    asked = []
    with pytest.raises(KankanError) as exc:
        run(kproject, graceful=True, confirm=lambda text: asked.append(text) or True)
    assert exc.value.status == Status.USAGE_ERROR and asked == []
    message = exc.value.message
    assert "claude-1 runs kankan" in message and "codex-1" not in message  # codex-1 started no agent yet
    assert "without --graceful" in message and "kankan start" in message
    assert f"kankan {build.__version__} from {build.PACKAGE} (code 000000000000)" in message
    assert [seat["seat"] for seat in exc.value.data["seats"]] == ["claude-1"]
    assert exc.value.data["command"]["code"] == "0" * 12
    # nothing changed
    assert unit.exists() and stopall.load_record(kproject.paths) is None
    assert not load_state(kproject.paths, "claude-1").stop_requested
    assert seats.liveness(kproject, "claude-1") == "alive"
    # the immediate stop still works
    report, _, _ = run(kproject)
    assert sorted(report["stopped"]) == ["claude-1", "codex-1"]


def test_graceful_ignores_the_seat_file_of_an_earlier_launcher(kproject, holders, monkeypatch):
    from kankan import build
    holders.append(hold(kproject, "claude-1"))
    record_launcher_code(kproject, "claude-1", session="earlier-session")
    monkeypatch.setattr(build, "code_id", lambda: "0" * 12)
    report, _, _ = run(kproject, graceful=True)
    assert report["stopped"] == ["claude-1"]


def test_graceful_with_the_same_kankan_stops(kproject, holders):
    holders.append(hold(kproject, "claude-1"))
    record_launcher_code(kproject, "claude-1")
    report, _, _ = run(kproject, graceful=True)
    assert report["stopped"] == ["claude-1"]


def test_stop_seat_refuses_an_agent_of_another_kankan(kproject, holders, monkeypatch, capsys):
    """`kankan seat stop <seat>` also waits for the task to end: refused when the agent runs another kankan (F§1)."""
    from kankan import build
    holders += [hold(kproject, "claude-1", mode="ignore"), hold(kproject, "codex-1", mode="ignore")]
    record_launcher_code(kproject, "claude-1")
    record_launcher_code(kproject, "codex-1")
    monkeypatch.setattr(build, "code_id", lambda: "0" * 12)
    record_launcher_code(kproject, "codex-1")  # codex-1 was restarted after the upgrade
    monkeypatch.chdir(kproject.paths.repo)
    assert cli.main(["seat", "stop", "claude-1"]) == Status.USAGE_ERROR.exit_code
    err = capsys.readouterr().err
    assert "claude-1 runs kankan" in err and "kankan seat recover claude-1 --kill" in err
    assert not load_state(kproject.paths, "claude-1").stop_requested
    assert cli.main(["seat", "stop", "codex-1"]) == 0
    assert load_state(kproject.paths, "codex-1").stop_requested


def test_stop_of_a_stopped_seat_is_not_checked(kproject, monkeypatch):
    from kankan import build
    holder = hold(kproject, "claude-1")
    record_launcher_code(kproject, "claude-1")
    update_state(kproject.paths, "claude-1", lambda s: setattr(s, "stop_requested", True))
    assert holder.wait(10) == 0
    monkeypatch.setattr(build, "code_id", lambda: "0" * 12)
    monkeypatch.chdir(kproject.paths.repo)
    update_state(kproject.paths, "claude-1", lambda s: setattr(s, "stop_requested", False))
    assert cli.main(["seat", "stop", "claude-1"]) == 0
    assert load_state(kproject.paths, "claude-1").stop_requested
