"""``kankan supervisor``: the pane's first process; starts the launcher and restarts it if it dies (§8.2)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

MAX_RESTARTS = 5
WINDOW_SECONDS = 600


def _ignore_job_signals() -> None:
    for sig in (signal.SIGINT, signal.SIGTSTP, signal.SIGQUIT):
        signal.signal(sig, signal.SIG_IGN)


def _seat_of_supervisor(cwd: str) -> Optional[str]:
    """The seat whose proc.json names this supervisor (to reoccupy it after a launcher crash)."""
    try:
        from kankan.config import Paths
        seats_dir = Paths.discover(Path(cwd)).seats_dir
    except Exception:
        return None
    for path in sorted(seats_dir.glob("*.proc.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (data.get("supervisor") or {}).get("pid") == os.getpid():
            return path.name[:-len(".proc.json")]
    return None


def launcher_argv(agent_type: str, seat: Optional[str]) -> List[str]:
    # The type is passed as --type=<type>: the "=" keeps airesumer from mistaking this process for an agent.
    argv = [sys.executable, "-m", "kankan", "launcher", f"--type={agent_type}"]
    if seat:
        argv.append(f"--seat={seat}")
    return argv


def run(project_cwd: str, agent_type: str, seat: Optional[str]) -> int:
    _ignore_job_signals()
    env = dict(os.environ)
    if os.getsid(0) == os.getpid():
        env["KANKAN_SUPERVISOR_PID"] = str(os.getpid())
    restarts: List[float] = []
    delay = 2
    while True:
        child = subprocess.Popen(launcher_argv(agent_type, seat), cwd=project_cwd, env=env)
        code = child.wait()
        if code == 0:
            return 0
        if code == 3:
            print("\nkankan: the launcher stopped because of a configuration problem (see the message above); "
                  "this window closes in 10 minutes.", flush=True)
            time.sleep(600)
            return 1
        seat = seat or _seat_of_supervisor(project_cwd)
        now = time.monotonic()
        restarts = [t for t in restarts if now - t < WINDOW_SECONDS]
        if len(restarts) >= MAX_RESTARTS:
            print(f"\nkankan: the launcher failed {len(restarts) + 1} times in {WINDOW_SECONDS // 60} minutes "
                  f"(last exit code {code}); giving up. See <git dir>/kankan/log.jsonl.", flush=True)
            # keep the error visible in the pane until the user closes it
            while True:
                time.sleep(3600)
        restarts.append(now)
        print(f"\nkankan: the launcher exited with code {code}; restarting in {delay} s...", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 60)
