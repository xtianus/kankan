"""Seats: creation, occupation, liveness, recovery, process data (spec §7)."""

from __future__ import annotations

import fcntl
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from kankan import clock, faults, gitutil, procutil
from kankan.config import SeatConfig, atomic_write_json, project_lock, read_json, save_config
from kankan.errors import KankanError, Status
from kankan.refstore import CARD_PREFIX, Create, RefValue, Update, seat_ref, validate_seat
from kankan import messages

AGENT_TYPES = ("claude", "codex")
KILL_GRACE = 5.0
LIST_SUFFIXES = (("in_progress", "In Progress"), ("implemented", "Implemented"), ("review", "Review"))


@dataclass
class Lease:
    seat: str
    type: str
    session: str
    seat_oid: str
    fd: int


def list_name(seat: str, suffix: str) -> str:
    return f"{seat} · {suffix}"


def seat_type(seat: str) -> str:
    return validate_seat(seat).rsplit("-", 1)[0]


def validate_type(agent_type: str) -> str:
    if agent_type not in AGENT_TYPES:
        raise KankanError(Status.USAGE_ERROR, f"unknown agent type {agent_type!r} (expected claude or codex)")
    return agent_type


SUPERVISOR_ENV = "KANKAN_SUPERVISOR_PID"


def pane_sid() -> Optional[int]:
    """Session id of the kankan pane this process runs in, or None.

    Only a session whose leader is the kankan supervisor (tmux starts the pane command
    as a session leader) is recorded, so recovery can never target a session kankan
    did not create, such as the user's shell or a test runner.
    """
    supervisor = os.environ.get(SUPERVISOR_ENV, "")
    sid = os.getsid(0)
    if supervisor.isdigit() and int(supervisor) == sid:
        return sid
    return None


def _lock_path(project, seat: str) -> Path:
    return project.paths.seats_dir / f"{seat}.lock"


def _proc_path(project, seat: str) -> Path:
    return project.paths.seats_dir / f"{seat}.proc.json"


def _try_flock(project, seat: str) -> Optional[int]:
    """Open the seat lock (non-inheritable) and try a non-blocking exclusive flock."""
    path = _lock_path(project, seat)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def any_held(paths) -> List[str]:
    """Seats whose liveness lock is currently held by a launcher (no Project needed)."""
    held = []
    for lock in sorted(paths.seats_dir.glob("*.lock")) if paths.seats_dir.exists() else []:
        fd = os.open(lock, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(lock.stem)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return held


def _release_fd(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def owned_cards(project, seat: str, phases=("impl", "review")) -> List[RefValue]:
    refs = project.refs.list(CARD_PREFIX)
    return [value for value in refs.values() if value.meta.get("owner") == seat and value.meta.get("phase") in phases]


def _candidates(project, agent_type: str) -> List[str]:
    seats = sorted((name for name, sc in project.cfg.seats.items() if sc.type == agent_type),
                   key=lambda n: int(n.rsplit("-", 1)[1]))
    holding = {v.meta.get("owner") for v in project.refs.list(CARD_PREFIX).values()
               if v.meta.get("phase") in ("impl", "review")}
    return [s for s in seats if s in holding] + [s for s in seats if s not in holding]


def acquire(project, agent_type: str, seat: Optional[str] = None, pids: Optional[dict] = None) -> Lease:
    """Occupy a seat (§7.1–7.3) under the project lock and return its lease."""
    validate_type(agent_type)
    if seat is not None and seat_type(seat) != agent_type:
        raise KankanError(Status.USAGE_ERROR, f"seat {seat} is not of type {agent_type}")
    with project_lock(project.paths):
        project.reload_config()
        if seat is not None:
            lease = _occupy(project, seat, agent_type, pids)
            if lease is None:
                raise KankanError(Status.CONFIG_ERROR, f"seat {seat} is in use or unavailable")
            return lease
        for name in _candidates(project, agent_type):
            lease = _occupy(project, name, agent_type, pids)
            if lease is not None:
                return lease
        existing = [n for n, sc in project.cfg.seats.items() if sc.type == agent_type]
        if len(existing) >= project.cfg.seat_cap:
            raise KankanError(Status.CONFIG_ERROR,
                              f"all {len(existing)} {agent_type} seats are busy and the per-type seat cap is "
                              f"{project.cfg.seat_cap}; stop an agent or raise seat_cap in config.json",
                              {"cap": project.cfg.seat_cap})
        numbers = [int(n.rsplit("-", 1)[1]) for n in existing] or [0]
        lease = _occupy(project, f"{agent_type}-{max(numbers) + 1}", agent_type, pids)
        if lease is None:
            raise KankanError(Status.INTERNAL_ERROR, "could not occupy a newly created seat")
        return lease


def _occupy(project, seat: str, agent_type: str, pids: Optional[dict]) -> Optional[Lease]:
    fd = _try_flock(project, seat)
    if fd is None:
        return None
    try:
        ref = seat_ref(seat)
        current = project.refs.read(ref)
        if current is not None:
            survivors = recover_dead(project, seat)
            if survivors:
                _release_fd(fd)
                return None
        ensure_created(project, seat, agent_type)
        session = secrets.token_hex(16)
        oid = project.refs.write_blob({"schema": 1, "kind": "seat", "seat": seat, "type": agent_type,
                                       "session": session, "created": clock.iso(clock.now())})
        ok = project.refs.transaction([Update(ref, oid, current.oid)] if current else [Create(ref, oid)])
        if not ok:
            raise KankanError(Status.NEEDS_RECONCILIATION, f"seat ref {ref} changed during occupation")
        if current is not None:
            project.log("seat_recovered", seat=seat, old=current.oid, new=oid)
            _comment_recovery(project, seat, oid)
        lease = Lease(seat=seat, type=agent_type, session=session, seat_oid=oid, fd=fd)
        info = {"launcher": os.getpid(), "sid": pane_sid()}
        info.update(pids or {})
        write_proc(project, seat, reset=True, **info)
        project.log("seat_acquired", seat=seat, session=session)
        return lease
    except BaseException:
        _release_fd(fd)
        raise


def _comment_recovery(project, seat: str, seat_oid: str) -> None:
    for value in owned_cards(project, seat):
        try:
            project.trello.add_comment(value.meta["card"], messages.SEAT_RECOVERED.format(seat=seat),
                                       f"recover-{seat_oid[:12]}")
        except KankanError as err:
            project.log("comment_failed", card=value.meta.get("card"), error=err.message)


def release(project, lease: Lease, *, retain: bool = False) -> None:
    """Conditionally delete the seat ref, then release the flock."""
    try:
        with project_lock(project.paths):
            if not retain:
                project.refs.delete(seat_ref(lease.seat), lease.seat_oid)
            project.log("seat_released", seat=lease.seat, session=lease.session)
    finally:
        _release_fd(lease.fd)


def ensure_created(project, seat: str, agent_type: str) -> SeatConfig:
    """Idempotently find or create the seat's lists, register them and create its worktree."""
    validate_seat(seat)
    with project_lock(project.paths):
        cfg = project.reload_config()
        existing = cfg.seats.get(seat)
        snap_lists = None
        if existing is None or not _lists_open(project, existing):
            snap_lists = project.trello.snapshot(cfg.board_id).lists
            ids = {}
            for attr, suffix in LIST_SUFFIXES:
                name = list_name(seat, suffix)
                matches = sorted((l for l in snap_lists.values() if l.name == name and not l.closed), key=lambda l: l.pos)
                ids[attr] = matches[0].id if matches else project.trello.create_list(cfg.board_id, name).id
            faults.point("after_seat_lists")
            existing = SeatConfig(type=agent_type, **ids)
            cfg.seats[seat] = existing
            save_config(project.paths, cfg)
            project.log("seat_created", seat=seat)
        ensure_worktree(project, seat)
        return existing


def _lists_open(project, seat_cfg: SeatConfig) -> bool:
    for list_id in (seat_cfg.in_progress, seat_cfg.implemented, seat_cfg.review):
        info = project.trello.list_info(list_id)
        if info is None or info.closed:
            return False
    return True


def ensure_worktree(project, seat: str) -> Path:
    path = project.paths.seat_worktree(seat)
    if path.exists() and (path / ".git").exists():
        return path
    gitutil.run(project.paths.repo, ["worktree", "prune"])
    path.parent.mkdir(parents=True, exist_ok=True)
    gitutil.run(project.paths.repo, ["worktree", "add", "--detach", str(path), project.cfg.integration_branch])
    return path


def read_proc(project, seat: str) -> Optional[dict]:
    try:
        return read_json(_proc_path(project, seat), None)
    except ValueError:
        return None


def write_proc(project, seat: str, reset: bool = False, **pids) -> None:
    """Record pids (with start times), the pane session id and the boot id for recovery."""
    data = {} if reset else (read_proc(project, seat) or {})
    data["boot_id"] = procutil.boot_id()
    for name, pid in pids.items():
        if name == "sid":
            data["sid"] = pid
            data["sid_start"] = procutil.start_time(pid) if pid else None
        elif pid:
            data[name] = {"pid": pid, "start": procutil.start_time(pid)}
        else:
            data.pop(name, None)
    atomic_write_json(_proc_path(project, seat), data)


def _recorded_alive(proc: dict) -> List[tuple]:
    """Recorded ``(pid, start_time)`` pairs whose process still exists with that start time."""
    result = []
    for name in ("supervisor", "launcher", "agent"):
        entry = proc.get(name)
        if isinstance(entry, dict) and procutil.alive(entry.get("pid"), entry.get("start")):
            result.append((entry["pid"], entry.get("start")))
    for entry in proc.get("unavailable") or []:
        # Without a start time the identity cannot be checked: a reused pid must never be signalled.
        if isinstance(entry, dict) and entry.get("start") is not None and procutil.alive(entry.get("pid"), entry["start"]) \
                and (entry["pid"], entry["start"]) not in result:
            result.append((entry["pid"], entry["start"]))
    return result


def recover_dead(project, seat: str, kill_alive: bool = False) -> List[int]:
    """Terminate leftovers of a dead seat's pane session; return survivors (empty = recovered)."""
    proc = read_proc(project, seat)
    own = os.getpid()
    exclude = {own} | procutil.ancestors(own)
    survivors: List[int] = []
    if proc is None:
        project.log("proc_missing", seat=seat)
    elif proc.get("boot_id") != procutil.boot_id():
        project.log("seat_reboot", seat=seat)
    else:
        sid = proc.get("sid")
        leader_start = procutil.start_time(sid) if sid else None
        if sid and leader_start is not None and leader_start != proc.get("sid_start"):
            project.log("sid_reused", seat=seat, sid=sid)  # another process owns that pid now: kill nothing
        elif sid:
            survivors = procutil.terminate_session(sid, exclude, grace=KILL_GRACE)
        remaining = [(pid, start) for pid, start in _recorded_alive(proc) if pid not in exclude and pid not in survivors]
        if kill_alive:
            # only the recorded processes themselves (identity checked): never a whole unvalidated session
            survivors += procutil.terminate_pids(remaining, grace=KILL_GRACE)
        else:
            survivors += [pid for pid, _ in remaining]
        escaped = [pid for pid in procutil.pids_with_cwd_under(str(project.paths.seat_worktree(seat)))
                   if pid not in exclude]
        if escaped:
            project.log("seat_escapees", seat=seat, pids=escaped)
    entries = [{"pid": p, "start": start} for p in survivors if (start := procutil.start_time(p)) is not None]
    survivors = [entry["pid"] for entry in entries]  # a survivor without a start time has exited meanwhile
    if survivors:
        data = read_proc(project, seat) or {}
        data["unavailable"] = entries
        atomic_write_json(_proc_path(project, seat), data)
        project.log("seat_unavailable", seat=seat, pids=survivors)
    return survivors


def liveness(project, seat: str) -> str:
    """``alive`` (flock held), ``free`` (no ref), ``unavailable`` (survivors recorded) or ``dead``.

    Probes run under the project lock: a probe briefly holding a seat flock must never make a
    concurrent occupation believe the seat is alive (and create an extra seat).
    """
    with project_lock(project.paths):
        return _liveness(project, seat)


def _liveness(project, seat: str) -> str:
    fd = _try_flock(project, seat)
    if fd is None:
        return "alive"
    try:
        if project.refs.read(seat_ref(seat)) is None:
            return "free"
        proc = read_proc(project, seat) or {}
        for entry in proc.get("unavailable") or []:
            if isinstance(entry, dict) and entry.get("start") is not None and procutil.alive(entry.get("pid"), entry["start"]):
                return "unavailable"
        return "dead"
    finally:
        _release_fd(fd)


def verify_owner(project, seat: str, session: str) -> RefValue:
    value = project.refs.read(seat_ref(seat))
    if value is None or value.meta.get("session") != session:
        raise KankanError(Status.NOT_OWNER, f"this session no longer owns seat {seat}")
    return value


AGENT_FILE = "kankan-agent.json"


def _worktree_gitdir(worktree) -> Optional[Path]:
    proc = gitutil.run(worktree, ["rev-parse", "--absolute-git-dir"], check=False)
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None


def write_agent_file(project, seat: str, session: str, worktree) -> None:
    """Seat and session for agents whose commands do not inherit the agent's environment (Codex runs them in a
    shared daemon): kept in the seat worktree's private git directory, never in the working tree."""
    gitdir = _worktree_gitdir(worktree)
    if gitdir is not None:
        from kankan.build import code_id
        atomic_write_json(gitdir / AGENT_FILE, {"seat": seat, "session": session, "project": project.paths.project,
                                                "code": code_id()})


def read_agent_file(cwd) -> Optional[dict]:
    gitdir = _worktree_gitdir(cwd)
    return read_json(gitdir / AGENT_FILE, None) if gitdir is not None else None


class AgentEnv:
    @staticmethod
    def from_environ(cwd=None) -> Tuple[str, str, str]:
        seat, session = os.environ.get("KANKAN_SEAT"), os.environ.get("KANKAN_SESSION")
        project = os.environ.get("KANKAN_PROJECT", "")
        if not seat or not session:
            data = read_agent_file(cwd or os.getcwd()) or {}
            seat, session, project = data.get("seat"), data.get("session"), data.get("project", "")
        if not seat or not session:
            raise KankanError(Status.USAGE_ERROR,
                              "agent commands need KANKAN_SEAT and KANKAN_SESSION (run them inside a kankan agent)")
        return validate_seat(seat), session, project
