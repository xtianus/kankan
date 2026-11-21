"""``kankan stop`` without a seat: stop every agent of the project and disable autostart (stopall spec)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from kankan import agents_cmd, clock, messages, seats
from kankan.config import atomic_write_json, read_json
from kankan.errors import KankanError, Status
from kankan.seatstate import load_state, update_state
from kankan.tmux import Tmux, session_name

RECORD = "stopped.json"  # <gcd>/kankan/stopped.json: what `kankan start` restores
POLL_SECONDS = 1.0
SETTLE_SECONDS = 5.0  # sessions close shortly after their supervisor exits


@dataclass
class Inventory:
    liveness: Dict[str, str]
    sessions: List[str]
    unit: bool
    unit_args: Optional[str] = None
    alive: List[str] = field(default_factory=list)

    def nothing_to_stop(self) -> bool:
        return not self.alive and not self.sessions and not self.unit \
            and "unavailable" not in self.liveness.values()


def sessions(project, tmux: Tmux) -> List[str]:
    """This clone's tmux sessions, recognised by the ``@kankan_repo`` option (never other projects' or the user's)."""
    mine = str(project.paths.repo)
    return sorted(name for name, owner in tmux.sessions_with_option("@kankan_repo").items() if owner == mine)


def _unit_args(project) -> Optional[str]:
    path = agents_cmd.unit_path(project)
    try:
        text = path.read_text()
    except OSError:
        return None
    match = re.search(r"^ExecStart=.* -m kankan up (.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def inventory(project, tmux: Tmux) -> Inventory:
    cfg = project.reload_config()
    liveness = {name: seats.liveness(project, name) for name in sorted(cfg.seats)}
    unit = agents_cmd.unit_path(project).exists()
    return Inventory(liveness=liveness, sessions=sessions(project, tmux), unit=unit,
                     unit_args=_unit_args(project) if unit else None,
                     alive=[name for name, state in liveness.items() if state == "alive"])


def _args_from_seats(project, alive: List[str]) -> str:
    cfg = project.reload_config()
    counts: Dict[str, int] = {}
    qa = None
    for name in alive:
        seat_cfg = cfg.seats.get(name)
        if seat_cfg is None:
            continue
        if seat_cfg.role == "qa":
            qa = seat_cfg.type
        else:
            counts[seat_cfg.type] = counts.get(seat_cfg.type, 0) + 1
    return " ".join([f"{t}={n}" for t, n in sorted(counts.items())] + ([f"qa={qa}"] if qa else []))


def _request_stop(project, seat: str) -> None:
    update_state(project.paths, seat, lambda s: setattr(s, "stop_requested", True))


def _seat_line(project, seat: str) -> str:
    state = load_state(project.paths, seat)
    task = state.task
    work = f"{task.get('kind', 'task')} {task.get('short', task.get('card', ''))}" if task else "no task"
    return f"  {seat}: {state.launcher_state}, {work}"


def _wait(project, tmux: Tmux, say: Callable[[str], None], poll: float) -> List[str]:
    """Wait until no seat is alive (re-asserting the stop requests); return the seats still alive if interrupted."""
    shown: Dict[str, str] = {}
    starting_deadline = clock.monotonic() + agents_cmd.UP_WAIT_SECONDS
    try:
        while True:
            alive = [name for name in sorted(project.reload_config().seats) if seats.liveness(project, name) == "alive"]
            for seat in alive:
                # Launcher.start clears the request: a launcher restarted by its supervisor must see it again.
                if not load_state(project.paths, seat).stop_requested:
                    _request_stop(project, seat)
                line = _seat_line(project, seat)
                if shown.get(seat) != line:
                    shown[seat] = line
                    say(line)
            for seat in [s for s in shown if s not in alive]:
                del shown[seat]
                say(f"  {seat}: stopped")
            starting = [name for name in sessions(project, tmux) if "-starting-" in name]
            if not alive and (not starting or clock.monotonic() >= starting_deadline):
                return []
            clock.sleep(poll)
    except KeyboardInterrupt:
        say("")
        return [name for name in sorted(project.reload_config().seats) if seats.liveness(project, name) == "alive"]


def _settle(project, tmux: Tmux) -> None:
    """A session closes shortly after its supervisor exits: give the stopped seats' sessions time to go."""
    deadline = clock.monotonic() + SETTLE_SECONDS
    while sessions(project, tmux) and clock.monotonic() < deadline:
        clock.sleep(0.2)


def _quit_now(project, tmux: Tmux, alive: List[str], report: dict) -> None:
    for seat in alive:
        try:
            from kankan import diag
            diag.seat_recover(project, seat, kill=True, comment=messages.STOP_FORCED)
        except KankanError as err:
            report["remaining"].append({"what": f"seat {seat}: {err.message}",
                                        "remedy": f"kankan seat recover {seat} --kill"})
    _settle(project, tmux)
    for name in sessions(project, tmux):
        tmux.kill_session(name)
        report["sessions_closed"].append(name)


def describe(project, inv: Inventory, graceful: bool = False) -> str:
    lines = ["kankan stop will:"]
    if inv.alive:
        lines.append(f"  stop the running agents after their current task: {', '.join(inv.alive)}" if graceful else
                     f"  quit the running agents now, interrupting their work (resumed at the next start): "
                     f"{', '.join(inv.alive)}")
    running = {session_name("ai-", project.paths.project, seat) for seat in inv.alive}
    others = [name for name in inv.sessions if name not in running]
    if others:
        lines.append(f"  close the tmux sessions: {', '.join(others)}")
    unavailable = [name for name, state in inv.liveness.items() if state == "unavailable"]
    if unavailable:
        lines.append(f"  check the seats with leftover processes: {', '.join(unavailable)}")
    if inv.unit:
        lines.append("  disable autostart")
    return "\n".join(lines)


def record_path(paths):
    return paths.kankan_dir / RECORD


def load_record(paths) -> Optional[dict]:
    try:
        record = read_json(record_path(paths), None)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def restart_commands(record: Optional[dict]) -> List[str]:
    if not record or not record.get("up"):
        return []
    commands = [f"kankan up {record['up']}"]
    if record.get("autostart"):
        commands.append(f"kankan autostart enable {record['up']}")
    return commands


def stop_all(project, *, graceful: bool = False, confirm: Callable[[str], bool], say: Callable[[str], None],
             tmux: Optional[Tmux] = None, poll: float = POLL_SECONDS) -> dict:
    tmux = tmux or Tmux()
    inv = inventory(project, tmux)
    already = [name for name, state in inv.liveness.items() if state in ("dead", "free")]
    base = {"stopped": [], "already_stopped": already, "graceful": graceful, "sessions_closed": [],
            "autostart": "already disabled", "restart": [], "remaining": [], "interrupted": False}
    if inv.nothing_to_stop():
        project.log("stop_all", nothing=True)
        return dict(base, nothing_to_stop=True, restart=restart_commands(load_record(project.paths)))
    if not confirm(describe(project, inv, graceful) + "\nProceed? [y/N] "):
        return {"cancelled": True}
    report = dict(base)
    args = inv.unit_args or _args_from_seats(project, inv.alive)
    if args:
        # What `kankan start` restores; a rerun with nothing to stop keeps the previous record.
        record = {"up": args, "autostart": inv.unit, "seats": inv.alive, "at": clock.iso(clock.now())}
        atomic_write_json(record_path(project.paths), record)
        report["restart"] = restart_commands(record)
    if inv.unit:
        try:
            agents_cmd.autostart_disable(project)
            report["autostart"] = "disabled"
        except KankanError as err:
            report["autostart"] = f"error: {err.message}"
    for seat in inv.alive:
        _request_stop(project, seat)
    project.log("stop_all", seats=inv.alive, graceful=graceful, autostart=report["autostart"])
    if not graceful:
        _quit_now(project, tmux, inv.alive, report)
    elif inv.alive or any("-starting-" in name for name in inv.sessions):
        say(messages.STOP_ALL_WAITING)
        still = _wait(project, tmux, say, poll)
        if not still:
            _settle(project, tmux)
        if still:
            report["interrupted"] = True
            report["remaining"].append({"what": f"still running: {', '.join(still)}",
                                        "remedy": "they stop after their current task; rerun "
                                                  "`kankan stop --graceful` to wait, or `kankan stop` to quit them now"})
    final = inventory(project, tmux)
    report["stopped"] = [seat for seat in inv.alive if final.liveness.get(seat) in ("dead", "free")]
    if not report["interrupted"]:
        for seat in final.alive:
            report["remaining"].append({"what": f"seat {seat} is still running",
                                        "remedy": f"kankan seat recover {seat} --kill"})
    for seat, state in final.liveness.items():
        if state == "unavailable":
            report["remaining"].append({"what": f"seat {seat} has leftover processes",
                                        "remedy": f"kankan seat recover {seat} --kill"})
    running_sessions = {session_name("ai-", project.paths.project, seat) for seat in final.alive}
    for name in final.sessions:
        if name not in running_sessions:
            report["remaining"].append({"what": f"tmux session {name} is still open (no agent runs in it)",
                                        "remedy": f"tmux kill-session -t {name}, or kankan stop"})
    if final.unit:
        report["remaining"].append({"what": f"autostart unit {agents_cmd.unit_path(project)} is still installed",
                                    "remedy": "kankan autostart disable"})
    if report["autostart"].startswith("error") and not final.unit:
        report["remaining"].append({"what": f"autostart: {report['autostart']}",
                                    "remedy": f"systemctl --user disable {agents_cmd.unit_name(project)}"})
    if report["remaining"]:
        raise KankanError(Status.NEEDS_ACTION, report_text(report), report)
    return report


def report_text(report: dict) -> str:
    if report.get("cancelled"):
        return "cancelled: nothing changed"
    lines = []
    if report.get("nothing_to_stop"):
        lines.append("nothing to stop: no agent is running and autostart is already disabled")
        if report["already_stopped"]:
            lines.append(f"already stopped: {', '.join(report['already_stopped'])}")
    else:
        verb = "stopped" if report["graceful"] else "stopped immediately"
        lines.append(f"{verb}: {', '.join(report['stopped']) or 'none'}")
        if report["already_stopped"]:
            lines.append(f"already stopped: {', '.join(report['already_stopped'])}")
        if report["sessions_closed"]:
            lines.append(f"tmux sessions closed: {', '.join(report['sessions_closed'])}")
        lines.append(f"autostart: {report['autostart']}")
        for entry in report["remaining"]:
            lines.append(f"not stopped: {entry['what']} → {entry['remedy']}")
    if report["restart"]:
        lines.append("run `kankan start` to resume everything (" + "; ".join(report["restart"]) + ")")
    return "\n".join(lines)


# ---------------------------------------------------------------------- kankan start
def _registered_args(cfg) -> str:
    counts: Dict[str, int] = {}
    qa = None
    for seat_cfg in cfg.seats.values():
        if seat_cfg.role == "qa":
            qa = seat_cfg.type
        else:
            counts[seat_cfg.type] = counts.get(seat_cfg.type, 0) + 1
    return " ".join([f"{t}={n}" for t, n in sorted(counts.items())] + ([f"qa={qa}"] if qa else []))


def no_record_help(cwd) -> str:
    """What to run when there is no stop record: ``init`` first for an unbound clone, then ``up``/``autostart``."""
    from kankan.config import Paths, load_config
    paths = Paths.discover(cwd)
    try:
        cfg = load_config(paths)
    except KankanError:
        cfg = None
    lines = ["no stop record: `kankan start` restores only what `kankan stop` stopped. To start agents:"]
    if cfg is None or not cfg.board_id:
        lines.append("  kankan init <board-url>          (this clone is not bound to a board yet)")
    args = (_registered_args(cfg) if cfg is not None else "") or "claude=<n> codex=<n> [qa=<type>]"
    lines.append(f"  kankan up {args}")
    lines.append(f"  kankan autostart enable {args}   (optional: start them at boot)")
    return "\n".join(lines)


def start_all(project, say: Callable[[str], None] = lambda text: None) -> dict:
    """Restore what the last ``kankan stop`` stopped: ``up`` with its counts and autostart if it was enabled."""
    record = load_record(project.paths)
    if record is None or not record.get("up"):
        raise KankanError(Status.NEEDS_ACTION, no_record_help(project.paths.repo))
    wanted, qa = agents_cmd.parse_wanted(record["up"].split())
    started = agents_cmd.up(project, wanted, qa=qa)
    warnings: List[str] = []
    autostart = "unchanged"
    if record.get("autostart"):
        warnings = agents_cmd.autostart_enable(project, wanted, qa=qa)
        autostart = "enabled"
    record_path(project.paths).unlink(missing_ok=True)
    project.log("start_all", up=record["up"], autostart=autostart, started=started)
    return {"up": record["up"], "started": started, "autostart": autostart, "warnings": warnings}


def start_text(report: dict) -> str:
    lines = [f"started {len(report['started'])} agent(s) for `kankan up {report['up']}`"
             if report["started"] else f"all agents of `kankan up {report['up']}` are running"]
    lines.append(f"autostart: {report['autostart']}")
    return "\n".join(lines)
