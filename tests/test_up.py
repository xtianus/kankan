import os
import signal
import subprocess
import sys
import threading

import pytest

from kankan import agents_cmd, seats
from kankan.errors import KankanError, Status
from kankan.seatstate import load_state
from tests.conftest import ROOT, git
from tests.helpers import bind_project
from tests.launcher_helpers import launcher_state, pid_alive, proc_info, wait_for

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=5)


@pytest.fixture
def env(monkeypatch, agent_path, tmux_server, tmp_path):
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    scenario = tmp_path / "scenario.json"
    scenario.write_text('{"steps": [{"sleep": 120}]}')
    monkeypatch.setenv("FAKE_AGENT_SCENARIO", str(scenario))
    return tmux_server


@pytest.fixture
def project(git_repo, fake_trello, monkeypatch, env):
    return bind_project(git_repo, fake_trello, monkeypatch, **FAST)


def alive(project, agent_type):
    return agents_cmd.live_counts(project).get(agent_type, 0)


def test_up_starts_only_missing(project, env):
    started = agents_cmd.up(project, {"claude": 1, "codex": 1})
    assert len(started) == 2
    assert agents_cmd.up(project, {"claude": 1, "codex": 1}) == []
    assert len(agents_cmd.up(project, {"claude": 2})) == 1
    assert alive(project, "claude") == 2 and alive(project, "codex") == 1
    # up() returns once the seats are taken; each launcher renames its session right after
    assert wait_for(lambda: {"ai-proj-claude-1", "ai-proj-claude-2", "ai-proj-codex-1"} <= set(env.sessions()),
                    timeout=15)


def test_kill9_supervisor_and_launcher_up_recovers(project, fake_trello, env):
    card = fake_trello.add_card(project.cfg.backlog, "c")
    agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    first = load_state(project.paths, "claude-1").task
    info = proc_info(project, "claude-1")
    os.kill(info["supervisor"]["pid"], signal.SIGKILL)
    os.kill(info["launcher"]["pid"], signal.SIGKILL)
    assert wait_for(lambda: not pid_alive(info["agent"]["pid"]), timeout=10)
    assert seats.liveness(project, "claude-1") == "dead"
    agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: (load_state(project.paths, "claude-1").task or {}).get("id") not in (None, first["id"]),
                    timeout=40)
    task = load_state(project.paths, "claude-1").task
    assert task["card"] == card["id"] and task["resumed"] is True


def test_autostart_unit_content(project, tmp_path, monkeypatch, isolated_home):
    calls = tmp_path / "systemctl.log"
    stub = tmp_path / "stubbin"
    stub.mkdir()
    for name, body in (("systemctl", f'echo "$@" >> {calls}'), ("loginctl", "echo Linger=no")):
        (stub / name).write_text(f"#!/bin/sh\n{body}\n")
        (stub / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    warnings = agents_cmd.autostart_enable(project, {"claude": 2, "codex": 1})
    unit = isolated_home / ".config" / "systemd" / "user" / "kankan-up@proj.service"
    text = unit.read_text()
    assert f"WorkingDirectory={project.paths.repo}" in text
    assert "-m kankan up claude=2 codex=1" in text and "WantedBy=default.target" in text
    assert calls.read_text().splitlines() == ["--user daemon-reload", "--user enable kankan-up@proj.service"]
    assert warnings and "linger" in warnings[0]
    agents_cmd.autostart_disable(project)
    assert not unit.exists()


def test_two_projects_isolated(tmp_path, fake_trello, monkeypatch, env):
    repos = []
    for name in ("proj1", "proj2"):
        repo = tmp_path / "coding" / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        (repo / "f").write_text(name)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        repos.append(bind_project(repo, fake_trello, monkeypatch, **FAST))
    for project in repos:
        agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: {"ai-proj1-claude-1", "ai-proj2-claude-1"} <= set(env.sessions()), timeout=15)
    lists = [p.reload_config().seats["claude-1"].in_progress for p in repos]
    assert lists[0] != lists[1]
    assert all(alive(p, "claude") == 1 for p in repos)


def test_concurrent_up_no_overstart(project, env):
    cmd = [sys.executable, "-m", "kankan", "up", "claude=2"]
    environment = dict(os.environ, PYTHONPATH=str(ROOT))
    procs = [subprocess.Popen(cmd, cwd=project.paths.repo, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, err
    assert alive(project, "claude") == 2
    assert wait_for(lambda: len([s for s in env.sessions() if s.startswith("ai-proj-claude")]) == 2, timeout=15)


def test_sanitized_name_collision_refused(tmp_path, fake_trello, monkeypatch, env):
    projects = []
    for name in ("a.b", "a_b"):
        repo = tmp_path / "coding" / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        (repo / "f").write_text(name)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        projects.append(bind_project(repo, fake_trello, monkeypatch, **FAST))
    agents_cmd.up(projects[0], {"claude": 1})
    with pytest.raises(KankanError) as exc:
        agents_cmd.up(projects[1], {"claude": 1})
    assert exc.value.status == Status.CONFIG_ERROR and "project_name" in exc.value.message


def test_parse_wanted():
    assert agents_cmd.parse_wanted(["claude=2", "codex=1"]) == {"claude": 2, "codex": 1}
    with pytest.raises(KankanError):
        agents_cmd.parse_wanted(["gpt=1"])
