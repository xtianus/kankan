"""``kankan agent``, ``kankan up`` and ``kankan autostart`` (spec §8.1)."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from kankan import seats, worktrees
from kankan.config import file_lock
from kankan.errors import KankanError, Status
from kankan.tmux import Tmux, sanitize, session_name

UP_WAIT_SECONDS = 30


def parse_wanted(items: List[str]) -> Dict[str, int]:
    wanted: Dict[str, int] = {}
    for item in items:
        agent_type, _, count = item.partition("=")
        if agent_type not in seats.AGENT_TYPES or not count.isdigit():
            raise KankanError(Status.USAGE_ERROR, f"expected <type>=<n> with type claude or codex, got {item!r}")
        wanted[agent_type] = int(count)
    if not wanted:
        raise KankanError(Status.USAGE_ERROR, "nothing to start: give e.g. claude=2 codex=1")
    return wanted


def _check_collision(project, tmux: Tmux) -> None:
    """Another repository whose sanitized project name is the same would share session names."""
    prefix = f"ai-{sanitize(project.paths.project)}-"
    mine = str(project.paths.repo)
    for name, owner in tmux.sessions_with_option("@kankan_repo").items():
        if name.startswith(prefix):
            if owner and owner != mine:
                raise KankanError(Status.CONFIG_ERROR,
                                  f"tmux session {name} belongs to {owner}, whose project name collides with this one; "
                                  "set \"project_name\" in this clone's <git dir>/kankan/config.json")


def supervisor_argv(agent_type: str, seat: Optional[str]) -> List[str]:
    argv = [sys.executable, "-m", "kankan", "supervisor", f"--type={agent_type}"]
    if seat:
        argv.append(f"--seat={seat}")
    return argv


def agent_path(path: Optional[str], paths) -> str:
    """PATH for agents: a private directory holding only a ``kankan`` wrapper for this installation comes first,
    so agents run the launcher's kankan without seeing its interpreter's python, pip or pytest."""
    path = os.environ.get("PATH", "/usr/bin:/bin") if path is None else path
    bin_dir = paths.kankan_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "kankan"
    content = f"#!/bin/sh\nexec '{sys.executable}' -m kankan \"$@\"\n"
    if not wrapper.exists() or wrapper.read_text() != content:
        tmp = bin_dir / f".kankan.{os.getpid()}"
        tmp.write_text(content)
        tmp.chmod(0o755)
        os.replace(tmp, wrapper)
    here = str(bin_dir)
    return os.pathsep.join([here] + [p for p in path.split(os.pathsep) if p and p != here])


def agent(project, agent_type: str, seat: Optional[str] = None, attach: bool = False,
          tmux: Optional[Tmux] = None, preflight: bool = True) -> str:
    """Create a tmux session running the supervisor; the launcher renames it to ai-<project>-<seat>."""
    tmux = tmux or Tmux()
    seats.validate_type(agent_type)
    if preflight:
        from kankan import doctor
        doctor.preflight([agent_type], project.paths)
    if seat is not None and seats.seat_type(seat) != agent_type:
        raise KankanError(Status.USAGE_ERROR, f"seat {seat} is not of type {agent_type}")
    _check_collision(project, tmux)
    if seat is not None and tmux.has_session(session_name("ai-", project.paths.project, seat)):
        raise KankanError(Status.CONFIG_ERROR, f"seat {seat} is already running")
    name = f"ai-{sanitize(project.paths.project)}-{agent_type}-starting-{secrets.token_hex(3)}"
    tmux.new_session(name, str(project.paths.repo), supervisor_argv(agent_type, seat), env={"PATH": agent_path(None, project.paths)})
    tmux.set_option(name, "@kankan_repo", str(project.paths.repo))
    project.log("agent_started", session=name, type=agent_type, seat=seat)
    if attach:
        session_id = tmux.run(["display-message", "-p", "-t", f"={name}:", "#{session_id}"]).stdout.strip()
        os.execvp("tmux", tmux._base() + ["attach-session", "-t", session_id])
    return name


def warnings_for(project) -> List[str]:
    return [f"{project.cfg.integration_branch} is checked out in {path}; only kankan moves it: switch that worktree "
            "to one of your branches" for path in worktrees.main_checked_out(project.paths.repo, project.cfg.integration_branch)]


def live_counts(project) -> Dict[str, int]:
    cfg = project.reload_config()
    counts: Dict[str, int] = {}
    for name, seat_cfg in cfg.seats.items():
        if seats.liveness(project, name) == "alive":
            counts[seat_cfg.type] = counts.get(seat_cfg.type, 0) + 1
    return counts


def up(project, wanted: Dict[str, int], tmux: Optional[Tmux] = None) -> List[str]:
    """Start only the missing agents; serialized by ``up.lock`` so concurrent calls never over-start."""
    tmux = tmux or Tmux()
    started: List[str] = []
    with file_lock(project.paths.kankan_dir / "up.lock", timeout=UP_WAIT_SECONDS * 10, what="up lock"):
        live = live_counts(project)
        missing = [t for t, n in wanted.items() if live.get(t, 0) < n]
        if missing:
            from kankan import doctor
            doctor.preflight(missing, project.paths)
        for agent_type, count in wanted.items():
            while live_counts(project).get(agent_type, 0) < count:
                before = live_counts(project).get(agent_type, 0)
                started.append(agent(project, agent_type, tmux=tmux, preflight=False))
                deadline = time.monotonic() + UP_WAIT_SECONDS
                while time.monotonic() < deadline and live_counts(project).get(agent_type, 0) <= before:
                    time.sleep(0.3)
                if live_counts(project).get(agent_type, 0) <= before:
                    raise KankanError(Status.INTERNAL_ERROR,
                                      f"the {agent_type} agent started in tmux session {started[-1]} did not occupy a "
                                      "seat within 30 s; attach to it to see why")
    return started


def unit_name(project) -> str:
    return f"kankan-up@{sanitize(project.paths.project)}.service"


def unit_path(project) -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "systemd" / "user" / unit_name(project)


def unit_content(project, wanted: Dict[str, int]) -> str:
    args = " ".join(f"{t}={n}" for t, n in wanted.items())
    return (f"[Unit]\nDescription=kankan agents for {project.paths.project}\nAfter=network-online.target\n\n"
            f"[Service]\nType=oneshot\nRemainAfterExit=yes\nWorkingDirectory={project.paths.repo}\n"
            f"Environment=PATH={os.environ.get('PATH', '/usr/bin:/bin')}\n"
            f"ExecStart={sys.executable} -m kankan up {args}\n\n[Install]\nWantedBy=default.target\n")


def _systemctl(*args: str) -> None:
    proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise KankanError(Status.CONFIG_ERROR, f"systemctl --user {' '.join(args)} failed: {proc.stderr.strip()}")


def autostart_enable(project, wanted: Dict[str, int]) -> List[str]:
    path = unit_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_content(project, wanted))
    _systemctl("daemon-reload")
    _systemctl("enable", unit_name(project))
    warnings = []
    user = os.environ.get("USER", "")
    if shutil.which("loginctl"):
        proc = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"], capture_output=True, text=True)
        if proc.stdout.strip() == "Linger=no":
            warnings.append("lingering is disabled: agents start only after you log in; enable it with "
                            "`loginctl enable-linger`")
    project.log("autostart_enabled", unit=unit_name(project), wanted=wanted)
    return warnings


def autostart_disable(project) -> None:
    path = unit_path(project)
    if path.exists():
        _systemctl("disable", unit_name(project))
        path.unlink()
        _systemctl("daemon-reload")
    project.log("autostart_disabled", unit=unit_name(project))
