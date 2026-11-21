"""``kankan agent``, ``kankan up`` and ``kankan autostart`` (spec §8.1)."""

from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kankan import build, messages, seats, worktrees
from kankan.adapters import WORK_ROLES
from kankan.config import file_lock
from kankan.errors import KankanError, Status
from kankan.tmux import Tmux, sanitize, session_name

UP_WAIT_SECONDS = 30
QA_ROLES = ("questions",)  # the Q&A agent needs only the questions seed (questions F§7)


def parse_wanted(items: List[str]) -> Tuple[Dict[str, int], Optional[str]]:
    """``<type>=<n>`` arguments and an optional ``qa=<type>`` (questions F§2): the work agents wanted and the type
    of the Q&A agent, if requested."""
    wanted: Dict[str, int] = {}
    qa: Optional[str] = None
    for item in items:
        agent_type, _, count = item.partition("=")
        if agent_type == "qa":
            if count not in seats.AGENT_TYPES or qa is not None:
                raise KankanError(Status.USAGE_ERROR, messages.QA_ARG.format(item=item))
            qa = count
            continue
        if agent_type not in seats.AGENT_TYPES or not count.isdigit():
            raise KankanError(Status.USAGE_ERROR, messages.UP_ARG.format(item=item))
        wanted[agent_type] = int(count)
    if not wanted and qa is None:
        raise KankanError(Status.USAGE_ERROR, messages.UP_NOTHING)
    return wanted, qa


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


def _check_qa_list(project) -> None:
    cfg = project.reload_config()
    info = project.trello.list_info(cfg.qa_list) if cfg.qa_list else None
    if info is None or info.closed:
        raise KankanError(Status.CONFIG_ERROR, messages.QA_LIST_UNAVAILABLE)


def _register_qa_seat(project, agent_type: str) -> None:
    """The ``Q&A`` list check, then the replacement of a Q&A seat of another type (refused while it runs) and the
    registration of ``<type>-qa``, before the ``questions`` seed preflight: a start refused for a missing seed
    leaves the seat registered, so ``kankan doctor`` asks for the seed and creates the ``questions`` card
    (questions F§7)."""
    from kankan import qa as qa_policy
    _check_qa_list(project)
    qa_policy.replace_seat(project, agent_type)
    seats.ensure_created(project, seats.qa_seat_name(agent_type), agent_type)


def supervisor_argv(agent_type: str, seat: Optional[str]) -> List[str]:
    argv = build.command("supervisor", f"--type={agent_type}")
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
    content = f"#!/bin/sh\nexec {' '.join(shlex.quote(arg) for arg in build.command())} \"$@\"\n"
    if not wrapper.exists() or wrapper.read_text() != content:
        tmp = bin_dir / f".kankan.{os.getpid()}"
        tmp.write_text(content)
        tmp.chmod(0o755)
        os.replace(tmp, wrapper)
    here = str(bin_dir)
    return os.pathsep.join([here] + [p for p in path.split(os.pathsep) if p and p != here])


def agent(project, agent_type: str, seat: Optional[str] = None, attach: bool = False,
          tmux: Optional[Tmux] = None, preflight: bool = True, qa: bool = False) -> str:
    """Create a tmux session running the supervisor; the launcher renames it to ai-<project>-<seat>.

    With ``qa``, the Q&A seat ``<type>-qa`` (questions F§2): the ``Q&A`` list must be open, a Q&A seat of another
    type is replaced first (``qa.replace_seat``: refused while it runs) and the seat is registered; then only the
    ``questions`` seed is checked."""
    tmux = tmux or Tmux()
    seats.validate_type(agent_type)
    qa = qa or (seat is not None and seats.is_qa(seat))
    if qa:
        if seat is not None and seat != seats.qa_seat_name(agent_type):
            raise KankanError(Status.USAGE_ERROR, messages.QA_SEAT_ARG.format(seat=seats.qa_seat_name(agent_type),
                                                                              given=seat))
        seat = seats.qa_seat_name(agent_type)
        _register_qa_seat(project, agent_type)
    if preflight:
        from kankan import doctor
        doctor.preflight([agent_type], project.paths, roles=QA_ROLES if qa else WORK_ROLES)
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
    for name, seat_cfg in cfg.work_seats().items():
        if seats.liveness(project, name) == "alive":
            counts[seat_cfg.type] = counts.get(seat_cfg.type, 0) + 1
    return counts


def _wait_started(project, session: str, what: str, started) -> None:
    deadline = time.monotonic() + UP_WAIT_SECONDS
    while time.monotonic() < deadline and not started():
        time.sleep(0.3)
    if not started():
        raise KankanError(Status.INTERNAL_ERROR, messages.UP_NOT_STARTED.format(what=what, session=session,
                                                                               seconds=UP_WAIT_SECONDS))


def up(project, wanted: Dict[str, int], tmux: Optional[Tmux] = None, qa: Optional[str] = None) -> List[str]:
    """Start only the missing agents, and the Q&A agent of type ``qa`` if it does not run (replacing a Q&A seat of
    another type); serialized by ``up.lock`` so concurrent calls never over-start."""
    tmux = tmux or Tmux()
    started: List[str] = []
    with file_lock(project.paths.kankan_dir / "up.lock", timeout=UP_WAIT_SECONDS * 10, what="up lock"):
        live = live_counts(project)
        missing = [t for t, n in wanted.items() if live.get(t, 0) < n]
        qa_seat = seats.qa_seat_name(qa) if qa else None
        qa_missing = qa_seat is not None and seats.liveness(project, qa_seat) != "alive"
        from kankan import doctor
        if missing:
            doctor.preflight(missing, project.paths)
        if qa_missing:
            _register_qa_seat(project, qa)  # refused while the other Q&A seat runs: before starting anything
            doctor.preflight([qa], project.paths, roles=QA_ROLES)
        for agent_type, count in wanted.items():
            while live_counts(project).get(agent_type, 0) < count:
                before = live_counts(project).get(agent_type, 0)
                started.append(agent(project, agent_type, tmux=tmux, preflight=False))
                _wait_started(project, started[-1], agent_type,
                              lambda: live_counts(project).get(agent_type, 0) > before)
        if qa_missing:
            started.append(agent(project, qa, tmux=tmux, preflight=False, qa=True))
            _wait_started(project, started[-1], f"{qa} Q&A", lambda: seats.liveness(project, qa_seat) == "alive")
    return started


def unit_name(project) -> str:
    return f"kankan-up@{sanitize(project.paths.project)}.service"


def unit_path(project) -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "systemd" / "user" / unit_name(project)


# The ExecStart line of an autostart unit and its arguments; units written before kankan 0.1.7 ran
# `<python> -m kankan up`, which imports a `kankan/` package of the working directory (the repository).
UNIT_UP = re.compile(r"^ExecStart=.*?(?: -m kankan| \"[^\"]*\") up (.+)$", re.M)


def unit_exec() -> str:
    """The unit's command before ``up``: this installation's kankan, the bootstrap quoted for systemd."""
    return f'{sys.executable} -c "{build.BOOTSTRAP}"'


def unit_legacy(text: str) -> bool:
    """The unit starts kankan with ``-m kankan`` (written before 0.1.7)."""
    match = UNIT_UP.search(text)
    return match is not None and " -m kankan up " in match.group(0)


def unit_content(project, wanted: Dict[str, int], qa: Optional[str] = None) -> str:
    args = " ".join([f"{t}={n}" for t, n in wanted.items()] + ([f"qa={qa}"] if qa else []))
    return (f"[Unit]\nDescription=kankan agents for {project.paths.project}\nAfter=network-online.target\n\n"
            f"[Service]\nType=oneshot\nRemainAfterExit=yes\nWorkingDirectory={project.paths.repo}\n"
            f"Environment=PATH={os.environ.get('PATH', '/usr/bin:/bin')}\n"
            f"ExecStart={unit_exec()} up {args}\n\n[Install]\nWantedBy=default.target\n")


def _systemctl(*args: str) -> None:
    proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise KankanError(Status.CONFIG_ERROR, f"systemctl --user {' '.join(args)} failed: {proc.stderr.strip()}")


def autostart_enable(project, wanted: Dict[str, int], qa: Optional[str] = None) -> List[str]:
    path = unit_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_content(project, wanted, qa))
    _systemctl("daemon-reload")
    _systemctl("enable", unit_name(project))
    warnings = []
    user = os.environ.get("USER", "")
    if shutil.which("loginctl"):
        proc = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"], capture_output=True, text=True)
        if proc.stdout.strip() == "Linger=no":
            warnings.append("lingering is disabled: agents start only after you log in; enable it with "
                            "`loginctl enable-linger`")
    project.log("autostart_enabled", unit=unit_name(project), wanted=wanted, qa=qa)
    return warnings


def autostart_disable(project) -> None:
    path = unit_path(project)
    if path.exists():
        _systemctl("disable", unit_name(project))
        path.unlink()
        _systemctl("daemon-reload")
    project.log("autostart_disabled", unit=unit_name(project))
