"""Linux process helpers based on /proc (start times, sessions, termination, PDEATHSIG)."""

from __future__ import annotations

import ctypes
import os
import signal
import time
from typing import Iterable, List, Optional, Set

PR_SET_PDEATHSIG = 1


def boot_id() -> str:
    with open("/proc/sys/kernel/random/boot_id") as handle:
        return handle.read().strip()


def _stat_fields(pid: int) -> Optional[List[str]]:
    """Fields of /proc/<pid>/stat after the command name; index 0 is field 3 (state)."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            data = handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    return data[data.rindex(")") + 2:].split()


def start_time(pid: int) -> Optional[int]:
    fields = _stat_fields(pid)
    return int(fields[19]) if fields else None


def session_id(pid: int) -> Optional[int]:
    fields = _stat_fields(pid)
    return int(fields[3]) if fields else None


def parent_pid(pid: int) -> Optional[int]:
    fields = _stat_fields(pid)
    return int(fields[1]) if fields else None


def is_zombie(pid: int) -> bool:
    fields = _stat_fields(pid)
    return fields is None or fields[0] in ("Z", "X")


def alive(pid: Optional[int], started: Optional[int]) -> bool:
    """True if ``pid`` exists, is not a zombie and has the recorded start time."""
    if not pid:
        return False
    fields = _stat_fields(pid)
    if fields is None or fields[0] in ("Z", "X"):
        return False
    return started is None or int(fields[19]) == started


def all_pids() -> List[int]:
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def pids_in_session(sid: int) -> List[int]:
    result = []
    for pid in all_pids():
        fields = _stat_fields(pid)
        if fields and int(fields[3]) == sid and fields[0] not in ("Z", "X"):
            result.append(pid)
    return result


def ancestors(pid: int) -> Set[int]:
    result = set()
    current = parent_pid(pid)
    while current and current not in result and current > 1:
        result.add(current)
        current = parent_pid(current)
    return result


def pids_with_cwd_under(path: str) -> List[int]:
    real = os.path.realpath(path)
    result = []
    for pid in all_pids():
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if (cwd == real or cwd.startswith(real + os.sep)) and not is_zombie(pid):
            result.append(pid)
    return result


def _signal_all(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _reap() -> None:
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                return
    except ChildProcessError:
        return


def terminate_session(sid: int, exclude: Set[int], grace: float = 5.0) -> List[int]:
    """SIGTERM every process of session ``sid`` (except ``exclude``), then SIGKILL; return survivors."""
    def targets():
        return [pid for pid in pids_in_session(sid) if pid not in exclude]

    pending = targets()
    if not pending:
        return []
    _signal_all(pending, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        _reap()
        if not targets():
            return []
        time.sleep(0.05)
    _signal_all(targets(), signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        _reap()
        if not targets():
            return []
        time.sleep(0.05)
    return targets()


def terminate_pids(pids: Iterable[tuple], grace: float = 5.0) -> List[int]:
    """SIGTERM then SIGKILL individual ``(pid, start_time)`` processes still matching; return survivors."""
    targets = [(pid, start) for pid, start in pids if alive(pid, start)]
    _signal_all([pid for pid, _ in targets], signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(alive(p, s) for p, s in targets):
        _reap()
        time.sleep(0.05)
    _signal_all([pid for pid, start in targets if alive(pid, start)], signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(alive(p, s) for p, s in targets):
        _reap()
        time.sleep(0.05)
    return [pid for pid, start in targets if alive(pid, start)]


def set_pdeathsig(sig: int = signal.SIGKILL) -> None:
    """Ask the kernel to send ``sig`` to this process when its parent dies."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
