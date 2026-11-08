import os
import signal
import subprocess
import textwrap
from pathlib import Path

import pytest

from kankan import auditlog
from kankan.config import Paths, load_config, save_config
from tests.helpers import bind_project
from tests.launcher_helpers import (agent_events, launcher_state, pid_alive, proc_info, seat_session, start_agent,
                                    wait_for)

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=0.2)


@pytest.fixture
def chain(git_repo, fake_trello, monkeypatch, agent_path, tmux_server):
    project = bind_project(git_repo, fake_trello, monkeypatch, **FAST)
    TMUX_DEBUG[:] = [tmux_server]
    return project, fake_trello, tmux_server


def started(project, seat="claude-1", tmux=None):
    ok = wait_for(lambda: launcher_state(project, seat) == "idle")
    if not ok:
        print("SESSIONS", TMUX_DEBUG[0].sessions() if TMUX_DEBUG else None)
        for name in (TMUX_DEBUG[0].sessions() if TMUX_DEBUG else []):
            print("SCREEN", name, TMUX_DEBUG[0].capture(TMUX_DEBUG[0].pane_of_session(name)))
        print("LOG", auditlog.read(project.paths)[-8:])
    return ok


TMUX_DEBUG = []


def test_kill9_launcher_agent_dies_new_launcher_same_seat(chain):
    project, fake, tmux = chain
    start_agent(tmux, project)
    assert started(project)
    assert tmux.has_session(seat_session(project, "claude-1"))
    info = proc_info(project, "claude-1")
    launcher, agent, supervisor = info["launcher"]["pid"], info["agent"]["pid"], info["supervisor"]["pid"]
    os.kill(launcher, signal.SIGKILL)
    assert wait_for(lambda: not pid_alive(agent), timeout=10)
    assert wait_for(lambda: proc_info(project, "claude-1").get("launcher", {}).get("pid") not in (None, launcher)
                    and "agent" in proc_info(project, "claude-1"), timeout=30)
    assert pid_alive(supervisor)
    assert started(project)
    assert proc_info(project, "claude-1")["supervisor"]["pid"] == supervisor
    assert [e for e in auditlog.read(project.paths) if e["event"] == "seat_recovered" and e.get("seat") == "claude-1"]


def test_agent_receives_ctrl_c(chain):
    project, fake, tmux = chain
    _, log = start_agent(tmux, project)
    assert started(project)
    launcher = proc_info(project, "claude-1")["launcher"]["pid"]
    pane = tmux.pane_of_session(seat_session(project, "claude-1"))
    tmux.run(["send-keys", "-t", pane, "C-c"])
    assert wait_for(lambda: any(e["event"] == "sigint" for e in agent_events(log)), timeout=10)
    assert pid_alive(launcher)


def test_orphan_grandchild_killed_on_agent_exit(chain):
    project, fake, tmux = chain
    fake.add_card(project.cfg.backlog, "c")
    _, log = start_agent(tmux, project, scenario={"steps": [{"spawn": "plain", "sleep": 0.5, "exit": True}]})
    assert wait_for(lambda: any(e["event"] == "spawned" for e in agent_events(log)), timeout=30)
    child = [e for e in agent_events(log) if e["event"] == "spawned"][0]["pid"]
    assert wait_for(lambda: not tmux.has_session(seat_session(project, "claude-1")), timeout=20)
    assert wait_for(lambda: not pid_alive(child), timeout=10)


def test_trust_auto_answer_only_under_root(chain, tmp_path):
    project, fake, tmux = chain
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root)
    cfg = load_config(project.paths)
    cfg.worktree_root = str(link_root)
    save_config(project.paths, cfg)
    project.paths = Paths.discover(project.paths.repo)
    _, log = start_agent(tmux, project, scenario={"trust": True})
    assert started(project)
    assert any(e["event"] == "trust_answer" for e in agent_events(log))


def test_trust_not_answered_outside_root(chain, tmp_path):
    project, fake, tmux = chain
    outside = tmp_path / "outside-wt"
    subprocess.run(["git", "-C", str(project.paths.repo), "worktree", "add", "-q", "--detach", str(outside)], check=True)
    project.paths.worktree_root.mkdir(parents=True, exist_ok=True)
    (project.paths.worktree_root / "claude-1").symlink_to(outside)
    _, log = start_agent(tmux, project, scenario={"trust": True})
    assert wait_for(lambda: any(e["event"] == "trust_not_answered" for e in auditlog.read(project.paths)), timeout=20)
    assert not any(e["event"] == "trust_answer" for e in agent_events(log))
    assert launcher_state(project, "claude-1") != "idle"


REAL_AIRESUMER = Path(os.path.expanduser("~")) / ".local" / "bin" / "airesumer.sh"  # evaluated before HOME isolation


def discover(tmux):
    script = REAL_AIRESUMER
    socket = f"/tmp/tmux-{os.getuid()}/{tmux.socket}"
    code = textwrap.dedent(f"""
        set -u
        eval "$(sed -n '/^SESSION_PREFIX=/p' {script})"
        eval "$(awk '/^AGENT_PROCESS_PATTERNS=\\(/,/^\\)/' {script})"
        for fn in matches_pattern_array agent_running list_all_pane_records discover_panes; do
            eval "$(awk -v f="$fn" '$0 ~ "^"f"\\\\(\\\\) \\\\{{" {{p=1}} p {{print}} p && /^}}/ {{p=0}}' {script})"
        done
        shopt -s nocasematch
        discover_panes
    """)
    env = dict(os.environ, TMUX=f"{socket},0,0")
    return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=env).stdout


@pytest.mark.skipif(not REAL_AIRESUMER.exists(), reason="airesumer.sh not installed")
def test_airesumer_discovers_agent_not_launcher(chain, tmp_path):
    project, fake, tmux = chain
    start_agent(tmux, project)
    assert started(project)
    found = discover(tmux)
    assert seat_session(project, "claude-1") in found
    tmux.new_session("ai-fake-launcher-only", str(tmp_path),
                     ["python3", "-c", "import time; time.sleep(30)", "kankan", "launcher", "--type=claude"])
    tmux.new_session(f"kankan-resolve-{project.paths.project}-Ab12Cd34", str(tmp_path), ["claude"])
    found = discover(tmux)
    assert "ai-fake-launcher-only" not in found
    assert "kankan-resolve" not in found
