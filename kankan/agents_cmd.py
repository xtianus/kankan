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

from kankan.boardreader import LIVE
from kankan import build, messages, qapool, seats, worktrees
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


def registered_args(cfg) -> str:
    """``<type>=<n> … [qa=<type>]`` for every seat registered in the project (empty when there is none): what
    ``kankan up`` without arguments starts and what ``kankan start`` without a stop record suggests."""
    counts: Dict[str, int] = {}
    qa = None
    for name, seat_cfg in cfg.seats.items():
        if seat_cfg.removing:  # seat T§5: a seat being removed is never asked for
            continue
        if seat_cfg.role == "qa" and qapool.is_member(name):
            continue  # qapool T§2: the pool's members are kankan's, never asked for
        if seat_cfg.role == "qa":
            qa = seat_cfg.type
        else:
            counts[seat_cfg.type] = counts.get(seat_cfg.type, 0) + 1
    return " ".join([f"{t}={n}" for t, n in sorted(counts.items())] + ([f"qa={qa}"] if qa else []))


def start_help(cwd, intro: str) -> str:
    """``intro``, then what to run to start agents: ``init`` first for an unbound clone, then ``up``/``autostart``
    with the registered seats' counts (placeholders when none is registered)."""
    from kankan.config import Paths, load_config
    paths = Paths.discover(cwd)
    try:
        cfg = load_config(paths)
    except KankanError:
        cfg = None
    lines = [intro]
    if cfg is None or not cfg.board_id:
        lines.append("  kankan init <board-url>          (this clone is not bound to a board yet)")
    args = (registered_args(cfg) if cfg is not None else "") or "claude=<n> codex=<n> [qa=<type>]"
    lines.append(f"  kankan up {args}")
    lines.append(f"  kankan autostart enable {args}   (optional: start them at boot)")
    return "\n".join(lines)


def registered_up_args(cwd) -> str:
    """The ``up`` arguments of the seats registered in the clone at ``cwd``; empty when unbound or without seats."""
    from kankan.config import Paths, load_config
    try:
        cfg = load_config(Paths.discover(cwd))
    except KankanError:
        return ""
    return registered_args(cfg) if cfg.board_id else ""


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
    info = project.board.list_info(cfg.qa_list, LIVE) if cfg.qa_list else None
    if info is None or info.closed:
        raise KankanError(Status.CONFIG_ERROR, messages.QA_LIST_UNAVAILABLE)


def _register_qa_seat(project, agent_type: str, change: bool = True) -> None:
    """The ``Q&A`` list check, then the type change of a Q&A pool of another type (qapool T§9: stopped at once and
    removed, its cut-short answers completed) and the registration of ``<type>-qa`` under ``up.lock``, before the
    ``questions`` seed preflight: a start refused for a missing seed leaves the router registered, so
    ``kankan doctor`` asks for the seed and creates the ``questions`` card (questions F§7). ``up`` holds ``up.lock``
    and has done the type change already (``change=False``): a seat removal never runs under ``up.lock``, and a pool
    of another type registered meanwhile stops the command (``check_no_other_type``)."""
    _check_qa_list(project)
    if change:
        qapool.change_type(project, agent_type)
    with file_lock(project.paths.kankan_dir / "up.lock", timeout=UP_WAIT_SECONDS * 10, what="up lock"):
        qapool.check_no_other_type(project, agent_type)
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
          tmux: Optional[Tmux] = None, preflight: bool = True, qa: bool = False, create: bool = True) -> str:
    """Create a tmux session running the supervisor; the launcher renames it to ai-<project>-<seat>.

    With ``qa``, the Q&A seat ``<type>-qa`` (questions F§2): the ``Q&A`` list must be open, a Q&A seat of another
    type is removed first (qapool T§9, the type change) and the router is registered; then only the
    ``questions`` seed is checked."""
    tmux = tmux or Tmux()
    seats.validate_type(agent_type)
    qa = qa or (seat is not None and seats.is_qa(seat))
    if qa:
        if seat is not None and seat != seats.qa_seat_name(agent_type):
            raise KankanError(Status.USAGE_ERROR, messages.QA_SEAT_ARG.format(seat=seats.qa_seat_name(agent_type),
                                                                              given=seat))
        seat = seats.qa_seat_name(agent_type)
        if create:
            _register_qa_seat(project, agent_type)
    if preflight:
        from kankan import doctor
        doctor.preflight([agent_type], project.paths, roles=QA_ROLES if qa else WORK_ROLES)
    if seat is not None and seats.seat_type(seat) != agent_type:
        raise KankanError(Status.USAGE_ERROR, f"seat {seat} is not of type {agent_type}")
    _check_collision(project, tmux)
    if seat is not None and tmux.has_session(session_name("ai-", project.paths.project, seat)):
        raise KankanError(Status.CONFIG_ERROR, f"seat {seat} is already running")
    if qa and create:
        qapool.clear_for_start(project)  # qapool T§9: `kankan agent <type> --qa` and `kankan up` start the router
    name = f"ai-{sanitize(project.paths.project)}-{agent_type}-starting-{secrets.token_hex(3)}"
    env = {"PATH": agent_path(None, project.paths)}
    if not create:  # seat T§5: the launcher occupies only a registered seat, also after its restarts
        env[seats.EXISTING_ONLY_ENV] = "1"
    tmux.new_session(name, str(project.paths.repo), supervisor_argv(agent_type, seat), env=env)
    tmux.set_option(name, "@kankan_repo", str(project.paths.repo))
    project.log("agent_started", session=name, type=agent_type, seat=seat)
    if attach:
        session_id = tmux.run(["display-message", "-p", "-t", f"={name}:", "#{session_id}"]).stdout.strip()
        os.execvp("tmux", tmux._base() + ["attach-session", "-t", session_id])
    return name


def start_member(project, seat: str, tmux: Optional[Tmux] = None) -> str:
    """qapool T§2: start the supervisor of the registered pool member ``seat`` under its exact name in a new tmux
    session (the launcher renames it to ai-<project>-<seat>) and return the session's name at once. It registers
    nothing and runs no preflight (the router's); the launcher occupies only the registered seat, also after its
    supervisor restarts it, and is refused once the member is marked for removal or the pool is stopping."""
    tmux = tmux or Tmux()
    if not qapool.is_member(seat):
        raise KankanError(Status.USAGE_ERROR, messages.QA_NOT_MEMBER.format(seat=seat))
    agent_type = seats.seat_type(seat)
    name = f"ai-{sanitize(project.paths.project)}-{agent_type}-starting-{secrets.token_hex(3)}"
    env = {"PATH": agent_path(None, project.paths), seats.EXISTING_ONLY_ENV: "1"}
    tmux.new_session(name, str(project.paths.repo), supervisor_argv(agent_type, seat), env=env)
    tmux.set_option(name, "@kankan_repo", str(project.paths.repo))
    project.log("qa_member_started", session=name, seat=seat)
    return name


def warnings_for(project) -> List[str]:
    return [f"{project.cfg.integration_branch} is checked out in {path}; only kankan moves it: switch that worktree "
            "to one of your branches" for path in worktrees.main_checked_out(project.paths.repo, project.cfg.integration_branch)]


def live_counts(project) -> Dict[str, int]:
    cfg = project.reload_config()
    counts: Dict[str, int] = {}
    for name, seat_cfg in cfg.work_seats().items():
        if not seat_cfg.removing and seats.liveness(project, name) == "alive":
            counts[seat_cfg.type] = counts.get(seat_cfg.type, 0) + 1
    return counts


def _wait_started(project, session: str, what: str, started) -> None:
    deadline = time.monotonic() + UP_WAIT_SECONDS
    while time.monotonic() < deadline and not started():
        time.sleep(0.3)
    if not started():
        raise KankanError(Status.INTERNAL_ERROR, messages.UP_NOT_STARTED.format(what=what, session=session,
                                                                               seconds=UP_WAIT_SECONDS))


def resume_removals(project, tmux: Optional[Tmux] = None, only: Optional[str] = None, strict: bool = False) -> dict:
    """seat T§5: the only code restarting or completing seats being removed. A graceful one that is not running and
    has work left is started (existing seat only) and awaited; a graceful one without work, and an immediate one
    (running or not: its confirmed stop is replayed), is completed. The starts happen under ``up.lock``, the
    completions after it (lock order: removal lock before ``up.lock``). Returns ``{"acted": [...], "failed":
    {seat: error}}``; with ``strict`` the first failure is raised."""
    from kankan import seatremove
    tmux = tmux or Tmux()
    acted: List[str] = []
    failed: Dict[str, str] = {}
    to_complete: List[str] = []

    def failure(name: str, err: KankanError) -> None:
        if strict:
            raise err
        failed[name] = err.message
        project.log("seat_remove_resume_failed", seat=name, error=err.message)

    with file_lock(project.paths.kankan_dir / "up.lock", timeout=UP_WAIT_SECONDS * 10, what="up lock"):
        cfg = project.reload_config()
        for name, seat_cfg in sorted(cfg.seats.items()):
            if not seat_cfg.removing or (only and name != only):
                continue
            alive = seats.liveness(project, name) == "alive"
            if seat_cfg.removing == "graceful" and alive:
                continue  # draining: its launcher completes the removal
            if seat_cfg.removing == "graceful" and not qapool.is_member(name) and seatremove.has_work(project, name):
                # a member is never started (qapool T§2): its pending answer is completed by the removal itself
                try:
                    session = agent(project, seat_cfg.type, seat=name, tmux=tmux, qa=seat_cfg.role == "qa",
                                    create=False)
                    seatremove.await_start(project, name, session)
                    acted.append(name)
                except KankanError as err:
                    failure(name, err)
            else:
                to_complete.append(name)
    for name in to_complete:
        try:
            if project.reload_config().seats.get(name) is not None \
                    and project.cfg.seats[name].removing == "immediate":
                seatremove.stop_seat(project, name, kill_alive=True)
            seatremove.complete(project, name)
            acted.append(name)
        except KankanError as err:
            failure(name, err)
    return {"acted": acted, "failed": failed}


def up(project, wanted: Dict[str, int], tmux: Optional[Tmux] = None, qa: Optional[str] = None) -> List[str]:
    """Start only the missing agents, and the Q&A agent of type ``qa`` if it does not run (replacing a Q&A seat of
    another type); serialized by ``up.lock`` so concurrent calls never over-start. Seats being removed are
    restarted or completed first (seat T§5)."""
    tmux = tmux or Tmux()
    resume_removals(project, tmux)
    if qa:
        _check_qa_list(project)
        qapool.change_type(project, qa)  # qapool T§9: a pool of another type is removed first, without asking
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
            _register_qa_seat(project, qa, change=False)  # before starting anything
            doctor.preflight([qa], project.paths, roles=QA_ROLES)
        for agent_type, count in wanted.items():
            while live_counts(project).get(agent_type, 0) < count:
                before = live_counts(project).get(agent_type, 0)
                started.append(agent(project, agent_type, tmux=tmux, preflight=False))
                _wait_started(project, started[-1], agent_type,
                              lambda: live_counts(project).get(agent_type, 0) > before)
        if qa_missing:
            qapool.clear_for_start(project)  # qapool T§9: cleared just before the router starts
            # registered above: the launcher occupies only the registered router (no second type change)
            started.append(agent(project, qa, tmux=tmux, preflight=False, qa=True, create=False))
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
