"""Helpers to run the real supervisor → launcher → fake agent chain in a private tmux server."""

import json
import os
import secrets
import sys
import time
from pathlib import Path

from kankan.config import read_json
from kankan.tmux import session_name


def wait_for(predicate, timeout=20.0, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def start_agent(tmux, project, agent_type="claude", seat=None, scenario=None, tmp_path=None):
    """Start ``kankan supervisor`` in a new session; return (session name, agent log path)."""
    tmp_path = Path(tmp_path or project.paths.repo.parent)
    tag = secrets.token_hex(3)
    log = tmp_path / f"agent-{tag}.log"
    scenario = dict(scenario or {})
    scenario.setdefault("log", str(log))
    scenario_path = tmp_path / f"scenario-{tag}.json"
    scenario_path.write_text(json.dumps(scenario))
    temp = f"ai-{project.paths.project}-{agent_type}-starting-{tag}"
    argv = [sys.executable, "-m", "kankan", "supervisor", f"--type={agent_type}"]
    if seat:
        argv.append(f"--seat={seat}")
    tmux.new_session(temp, str(project.paths.repo), argv, env={"FAKE_AGENT_SCENARIO": str(scenario_path)})
    return temp, Path(scenario["log"])


def agent_events(log: Path):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def proc_info(project, seat):
    return read_json(project.paths.seats_dir / f"{seat}.proc.json", {}) or {}


def seat_session(project, seat):
    return session_name("ai-", project.paths.project, seat)


def launcher_state(project, seat):
    return (read_json(project.paths.seats_dir / f"{seat}.state.json", {}) or {}).get("launcher_state")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
