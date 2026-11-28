"""``kankan statusline`` (qapool T§3): Claude Code's status line command for the Q&A pool's agents.

Claude Code runs it after every request of the agent, with the agent's environment and the worktree as working
directory, and passes a JSON object on standard input whose ``prompt_cache`` carries ``expires_at``. The command
records the reading for the seat, then shows the user's own status line, so the pane looks as before. It never
fails: errors go to the audit log, never to the status line.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from kankan import auditlog, procutil
from kankan.config import Paths, atomic_write_json

USER_TIMEOUT = 5  # seconds the user's own status line may take


def _paths(cwd: str) -> Paths:
    return Paths.discover(Path(cwd))


def _seat(cwd: str) -> Optional[str]:
    """The seat, as agent commands identify it (``KANKAN_SEAT``/``KANKAN_SESSION``, or the agent file)."""
    from kankan.seats import AgentEnv
    try:
        return AgentEnv.from_environ(cwd)[0]
    except Exception:  # noqa: BLE001 - not a kankan agent: nothing to record
        return None


def _agent(paths: Paths, seat: str) -> Optional[dict]:
    """The seat's current agent process (``proc.json``) when it is an ancestor of this command (or this process)."""
    from kankan.config import read_json
    try:
        proc = read_json(paths.seats_dir / f"{seat}.proc.json", None) or {}
    except ValueError:
        return None
    entry = proc.get("agent")
    if not isinstance(entry, dict) or not entry.get("pid"):
        return None
    me = os.getpid()
    if entry["pid"] != me and entry["pid"] not in procutil.ancestors(me):
        return None
    return {"pid": entry["pid"], "start": procutil.start_time(entry["pid"])}


def record(paths: Paths, seat: str, data: dict) -> None:
    """Write ``<seat>.cache.json``: ``expires_at`` (Unix seconds, None before the first request), ``ttl``, the
    agent process and the time of the reading."""
    cache = data.get("prompt_cache") if isinstance(data, dict) else None
    cache = cache if isinstance(cache, dict) else {}
    expires = cache.get("expires_at")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        expires = None
    atomic_write_json(paths.seats_dir / f"{seat}.cache.json",
                      {"expires_at": expires, "ttl": cache.get("ttl"), "agent": _agent(paths, seat),
                       "read_at": time.time()})


def user_command(cwd: str) -> Optional[str]:
    """The first ``statusLine`` of type ``command`` in the worktree's ``.claude/settings.local.json``,
    ``.claude/settings.json``, then ``~/.claude/settings.json`` (Claude Code's precedence)."""
    for path in (Path(cwd) / ".claude" / "settings.local.json", Path(cwd) / ".claude" / "settings.json",
                 Path(os.path.expanduser("~")) / ".claude" / "settings.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        line = data.get("statusLine") if isinstance(data, dict) else None
        if isinstance(line, dict) and line.get("type") == "command" and isinstance(line.get("command"), str) \
                and line["command"].strip():
            return line["command"]
    return None


def run(stdin_text: str, cwd: str) -> str:
    """Record the reading of the seat running this command, then return the user's status line output."""
    paths = None
    try:
        paths = _paths(cwd)
    except Exception:  # noqa: BLE001 - outside a kankan clone: only the user's status line
        paths = None
    seat = _seat(cwd) if paths is not None else None
    if seat is not None:
        try:
            data = json.loads(stdin_text)
            record(paths, seat, data)
        except Exception as err:  # noqa: BLE001 - the status line never fails
            auditlog.append(paths, "statusline_failed", seat=seat, error=f"{type(err).__name__}: {err}")
    command = user_command(cwd)
    if command is None:
        return ""
    try:
        proc = subprocess.run(["sh", "-c", command], input=stdin_text, cwd=cwd, capture_output=True, text=True,
                              timeout=USER_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as err:
        if paths is not None:
            auditlog.append(paths, "statusline_failed", seat=seat, error=f"user status line: {err}")
        return ""
    if proc.returncode != 0 and paths is not None:
        auditlog.append(paths, "statusline_failed", seat=seat,
                        error=f"user status line exited with {proc.returncode}: {proc.stderr[-500:]}")
    return proc.stdout


def main(stdin_text: str, cwd: str) -> int:
    try:
        out = run(stdin_text, cwd)
    except Exception:  # noqa: BLE001 - always exit 0
        out = ""
    if out:
        import sys
        sys.stdout.write(out)
        sys.stdout.flush()
    return 0
