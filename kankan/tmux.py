"""tmux sessions, screen capture and buffered pastes (spec §8.6)."""

from __future__ import annotations

import re
import subprocess
import uuid
from typing import Dict, List, Optional

from kankan.errors import KankanError, Status

FORBIDDEN_KEYS = {"escape", "esc", "c-c", "c-z", "c-\\", "c-d"}


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def session_name(prefix: str, project: str, suffix: str) -> str:
    return f"{prefix}{sanitize(project)}-{sanitize(suffix)}"


class Tmux:
    def __init__(self, socket: Optional[str] = None):
        # KANKAN_TMUX_SOCKET selects a private server (tests); inside a pane $TMUX is used.
        import os
        self.socket = socket or os.environ.get("KANKAN_TMUX_SOCKET") or None

    def _base(self) -> List[str]:
        return ["tmux"] + (["-L", self.socket] if self.socket else [])

    def run(self, args: List[str], input: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(self._base() + args, input=input, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise KankanError(Status.INTERNAL_ERROR, f"tmux {args[0]} failed: {proc.stderr.strip()}")
        return proc

    def has_session(self, name: str) -> bool:
        return self.run(["has-session", "-t", f"={name}"], check=False).returncode == 0

    def new_session(self, name: str, cwd: str, argv: List[str], env: Optional[Dict[str, str]] = None,
                    width: int = 200, height: int = 50) -> None:
        args = ["new-session", "-d", "-s", name, "-c", str(cwd), "-x", str(width), "-y", str(height)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        self.run(args + ["--"] + list(argv))

    def rename_session(self, old: str, new: str) -> None:
        self.run(["rename-session", "-t", f"={old}", new])

    def kill_session(self, name: str) -> None:
        self.run(["kill-session", "-t", f"={name}"], check=False)

    def set_option(self, session: str, option: str, value: str) -> None:
        self.run(["set-option", "-t", f"={session}:", option, value])

    def get_option(self, session: str, option: str) -> Optional[str]:
        proc = self.run(["show-options", "-v", "-t", f"={session}:", option], check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def sessions(self) -> List[str]:
        proc = self.run(["list-sessions", "-F", "#{session_name}"], check=False)
        return proc.stdout.split() if proc.returncode == 0 else []

    def pane_of_session(self, name: str) -> str:
        proc = self.run(["list-panes", "-t", f"={name}", "-F", "#{pane_id}"], check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise KankanError(Status.CONFIG_ERROR, f"tmux session {name} not found")
        return proc.stdout.split()[0]

    def capture(self, pane: str) -> str:
        return self.run(["capture-pane", "-p", "-t", pane]).stdout

    def paste(self, pane: str, text: str, submit_key: Optional[str] = "Enter") -> None:
        """Send ``text`` whole through a uniquely named buffer, deleted after use, then submit."""
        buffer = f"kankan-{uuid.uuid4().hex}"
        self.run(["load-buffer", "-b", buffer, "-"], input=text)
        try:
            self.run(["paste-buffer", "-d", "-p", "-b", buffer, "-t", pane])
        finally:
            self.run(["delete-buffer", "-b", buffer], check=False)
        if submit_key:
            self.send_keys(pane, [submit_key])

    def send_keys(self, pane: str, keys: List[str]) -> None:
        """Send named keys one by one; interrupt keys are refused (§8.6)."""
        for key in keys:
            if key.lower() in FORBIDDEN_KEYS:
                raise KankanError(Status.INTERNAL_ERROR, f"kankan never sends {key} to an agent")
            self.run(["send-keys", "-t", pane, key])

    def client_activity(self, session: str) -> Optional[float]:
        """Latest keyboard activity (epoch seconds) of the clients attached to ``session``."""
        proc = self.run(["list-clients", "-t", f"={session}", "-F", "#{client_activity}"], check=False)
        values = [float(v) for v in proc.stdout.split() if v.strip().isdigit()] if proc.returncode == 0 else []
        return max(values) if values else None

    def buffers(self) -> List[str]:
        proc = self.run(["list-buffers", "-F", "#{buffer_name}"], check=False)
        return proc.stdout.split() if proc.returncode == 0 else []

    def pane_pid(self, pane: str) -> Optional[int]:
        proc = self.run(["display-message", "-p", "-t", pane, "#{pane_pid}"], check=False)
        return int(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip().isdigit() else None
