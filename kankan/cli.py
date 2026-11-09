"""Command line interface: commands, text and JSON output, exit codes (spec §16)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kankan import auditlog
from kankan.errors import KankanError, Status, envelope

HIDDEN = argparse.SUPPRESS


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise KankanError(Status.USAGE_ERROR, message)


def _common() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--json", action="store_true", help="print the JSON envelope")
    return parent


COMMANDS: Dict[str, Callable] = {}


def command(name: str):
    def register(func):
        COMMANDS[name] = func
        return func
    return register


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = _Parser(prog="kankan", description="Coordinate AI coding agents through a Trello board.")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser, metavar="<command>")

    def add(name, help_text, hidden=False):
        kwargs = {"parents": [common]}
        if not hidden:
            kwargs["help"] = help_text
        return sub.add_parser(name, **kwargs)

    # user commands
    p = add("init", "bind this clone to a Trello board")
    p.add_argument("url", nargs="?")
    p.add_argument("--credentials", help=".properties file holding the Trello key and token")
    p.add_argument("--takeover", action="store_true", help="take over a board bound to another clone")
    p.add_argument("--reauth", action="store_true", help="only acquire new credentials")
    p = add("agent", "start an agent in tmux")
    p.add_argument("type", choices=["claude", "codex"])
    p.add_argument("--seat")
    p.add_argument("--attach", action="store_true")
    p = add("up", "ensure the given number of agents run, starting the missing ones")
    p.add_argument("wanted", nargs="+", metavar="<type>=<n>")
    p = add("autostart", "start agents at boot (systemd user service)")
    p.add_argument("action", choices=["enable", "disable"])
    p.add_argument("wanted", nargs="*", metavar="<type>=<n>")
    add("status", "seats, launcher states, tasks and waiting cards")
    p = add("doctor", "check the installation and this project, fix what is safe, list manual steps")
    p.add_argument("--no-fix", action="store_true", help="only report, change nothing")
    p.add_argument("--offline", action="store_true", help="skip the checks that need Trello")
    p = add("merge-request", "ask the agents to merge one of your branches into their integration branch")
    p.add_argument("branch", nargs="?", help="your branch (default: user_branch from the configuration)")
    p.add_argument("--remote", action="store_true", help="take the branch from origin (fetched now)")
    p.add_argument("--bottom", action="store_true", help="put the card at the bottom of the Backlog")
    add("claims", "card refs with phase, holder, age and branch")
    p = add("release", "administration: park a card's ref (needs --force)")
    p.add_argument("card")
    p.add_argument("--force", action="store_true")
    p = add("seat", "seat administration")
    p.add_argument("action", choices=["recover", "remove"])
    p.add_argument("seat")
    p.add_argument("--kill", action="store_true")
    p = add("purge-remote", "delete refs/agent-* published on a remote by mistake")
    p.add_argument("--remote", default="origin")
    p = add("reconcile", "report inconsistencies (and repair the certain ones with --fix)")
    p.add_argument("--fix", action="store_true")
    p = add("skill", "install the shared kankan skill for Claude Code and Codex")
    p.add_argument("action", choices=["install"])
    p.add_argument("--force", action="store_true")
    p = add("stop", "stop a seat's agent after its current task")
    p.add_argument("seat")
    p = add("adapter", "adapter tools")
    p.add_argument("action", choices=["check"])
    p.add_argument("target", help="a seat (e.g. claude-1) or a file with a saved screen")
    p.add_argument("--type", choices=["claude", "codex"], help="agent type for a screen file")
    # agent commands
    add("task", "show the current task (agent command)")
    p = add("note", "comment progress on the card (agent command)")
    p.add_argument("text")
    add("done", "close the current task (agent command)")
    add("sync", "merge main into the review branch (agent command)")
    p = add("verify", "run the test command on a commit (agent command)")
    p.add_argument("--tested", required=True)
    add("yield", "give the card back (agent command)")
    p = add("ask", "ask the user a question (agent command)")
    p.add_argument("question")
    p = add("fail", "report an error that needs the user (agent command)")
    p.add_argument("reason")
    p = add("block", "park the card in Blocked (agent command)")
    p.add_argument("reason")
    p = add("resolve", "resolve a blocked card by hand")
    p.add_argument("short")
    p.add_argument("--done", action="store_true")
    p = add("integrate", "merge the tested branch into main (agent command)")
    p.add_argument("--tested", required=True)
    p = add("tick", "", hidden=True)
    p.add_argument("--seat", required=True)
    for extra in EXTRA_PARSERS:
        extra(add)
    return parser


EXTRA_PARSERS: List[Callable] = []


def _print_text(result: dict) -> None:
    message = result.get("message")
    data = result.get("data")
    if message:
        print(message)
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            print(f"{key}: {value}")
    elif isinstance(data, list):
        for item in data:
            print(item if not isinstance(item, (dict, list)) else json.dumps(item))


def _log_internal(err: BaseException) -> None:
    try:
        from kankan.config import Paths
        auditlog.append(Paths.discover(os.getcwd()), "internal_error", error=repr(err),
                        traceback=traceback.format_exc())
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("supervisor", "launcher"):
        return _internal(argv)
    as_json = "--json" in argv
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if not args.command:
            parser.print_help()
            return Status.USAGE_ERROR.exit_code
        result = COMMANDS[args.command](args)
        if isinstance(result, tuple):
            data, message = result
        else:
            data, message = result, ""
        out = envelope(data, message)
        code = 0
    except KankanError as err:
        out = envelope(err)
        code = err.status.exit_code
    except KeyboardInterrupt:
        raise
    except Exception as err:  # noqa: BLE001 - every unexpected error becomes internal_error
        _log_internal(err)
        out = envelope(KankanError(Status.INTERNAL_ERROR, f"internal error: {type(err).__name__}: {err}"))
        code = Status.INTERNAL_ERROR.exit_code
    if as_json:
        print(json.dumps(out, indent=None, default=str))
    elif out["ok"]:
        _print_text(out)
    else:
        print(f"{out['status']}: {out['message']}", file=sys.stderr)
    return code


def _internal(argv: List[str]) -> int:
    """Hidden process-chain commands: they own the pane, so no envelope is printed."""
    parser = argparse.ArgumentParser(prog=f"kankan {argv[0]}")
    parser.add_argument("--type", required=True, choices=["claude", "codex"])
    parser.add_argument("--seat")
    args = parser.parse_args(argv[1:])
    if argv[0] == "supervisor":
        from kankan import supervisor
        return supervisor.run(os.getcwd(), args.type, args.seat)
    from kankan import launcher
    return launcher.main(args.type, args.seat)


# --------------------------------------------------------------------- agent commands
@command("task")
def _cmd_task(args):
    from kankan import agentcmds
    return agentcmds.task(agentcmds.context())


@command("note")
def _cmd_note(args):
    from kankan import agentcmds
    return agentcmds.note(agentcmds.context(), args.text), "note added"


@command("done")
def _cmd_done(args):
    from kankan import agentcmds
    data = agentcmds.done(agentcmds.context())
    return data, f"done: card is now {data.get('phase')}"


@command("sync")
def _cmd_sync(args):
    from kankan import agentcmds, integrate
    return integrate.sync(agentcmds.context()), "main merged into the branch"


@command("verify")
def _cmd_verify(args):
    from kankan import agentcmds, integrate
    return integrate.verify(agentcmds.context(), args.tested), "tests passed"


@command("integrate")
def _cmd_integrate(args):
    from kankan import agentcmds, integrate
    data = integrate.integrate(agentcmds.context(), args.tested)
    return data, f"integrated: {data['merge']}"


@command("ask")
def _cmd_ask(args):
    from kankan import agentcmds
    return agentcmds.ask(agentcmds.context(), args.question), "question posted; wait for the answer in this terminal"


@command("fail")
def _cmd_fail(args):
    from kankan import agentcmds
    return agentcmds.fail(agentcmds.context(), args.reason), "error reported; wait for the user"


@command("yield")
def _cmd_yield(args):
    from kankan import agentcmds
    data = agentcmds.yield_(agentcmds.context())
    return data, "card given back"


@command("block")
def _cmd_block(args):
    from kankan import agentcmds
    data = agentcmds.block(agentcmds.context(), args.reason)
    return data, "card blocked"


@command("resolve")
def _cmd_resolve(args):
    from kankan import integrate
    from kankan.config import project_lock
    project = _project()
    with project_lock(project.paths):
        return integrate.resolve(project, args.short, args.done)


@command("tick")
def _cmd_tick(args):
    """Hidden: one launcher iteration on ``--seat`` without an agent (tests and debugging).

    The seat stays occupied by the printed session after the command exits, so
    agent commands can be run with KANKAN_SEAT/KANKAN_SESSION; the next
    occupation recovers it.
    """
    import os as _os
    from kankan import seats, workflow
    from kankan.project import Project
    project = Project.open(_os.getcwd())
    seat = seats.validate_seat(args.seat)
    lease = seats.acquire(project, seats.seat_type(seat), seat=seat)
    task = workflow.tick(project, lease)
    _os.close(lease.fd)
    return {"seat": lease.seat, "session": lease.session, "task": task}


# --------------------------------------------------------------------- user commands
@command("init")
def _cmd_init(args):
    from pathlib import Path
    from kankan.init import init
    cfg, warnings = init(os.getcwd(), args.url, Path(args.credentials) if args.credentials else None,
                         args.takeover, args.reauth)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return {"board": cfg.board_id, "registry": cfg.registry_id, "warnings": warnings}, \
        f"kankan initialized for board {cfg.board_short}"


@command("adapter")
def _cmd_adapter(args):
    from pathlib import Path
    from kankan.adapters import classify, load_adapter
    from kankan.config import Paths, load_config
    try:
        paths = Paths.discover(os.getcwd())
    except KankanError:
        paths = None
    target = Path(args.target)
    if target.exists():
        agent_type = args.type or ("codex" if "codex" in target.name else "claude")
        screen = target.read_text(errors="replace")
        tail_lines = 30
    else:
        from kankan import seats, tmux
        seat = seats.validate_seat(args.target)
        agent_type = seats.seat_type(seat)
        if paths is None:
            raise KankanError(Status.CONFIG_ERROR, "not inside a kankan project")
        cfg = load_config(paths)
        session = tmux.session_name("ai-", paths.project, seat)
        screen = tmux.Tmux().capture(tmux.Tmux().pane_of_session(session))
        tail_lines = cfg.tail_lines
    result = classify(load_adapter(paths, agent_type), screen, tail_lines)
    data = {"type": agent_type, "state": result.state, "pattern": result.pattern, "answer": result.answer,
            "resume_at": result.resume_at.isoformat() if result.resume_at else None}
    return data, f"{result.state} (pattern: {result.pattern})" if result.pattern else result.state


@command("stop")
def _cmd_stop(args):
    from kankan import seats
    from kankan.config import Paths, load_config
    from kankan.seatstate import update_state
    paths = Paths.discover(os.getcwd())
    cfg = load_config(paths)
    seat = seats.validate_seat(args.seat)
    if seat not in cfg.seats:
        raise KankanError(Status.USAGE_ERROR, f"unknown seat {seat}")
    update_state(paths, seat, lambda s: setattr(s, "stop_requested", True))
    auditlog.append(paths, "stop_requested", seat=seat)
    return {"seat": seat}, f"{seat} stops after its current task"


def _project():
    from kankan.project import Project
    return Project.open(os.getcwd())


@command("agent")
def _cmd_agent(args):
    from kankan import agents_cmd
    project = _project()
    for warning in agents_cmd.warnings_for(project):
        print(f"warning: {warning}", file=sys.stderr)
    name = agents_cmd.agent(project, args.type, args.seat, args.attach)
    return {"session": name}, f"started tmux session {name}"


@command("up")
def _cmd_up(args):
    from kankan import agents_cmd
    project = _project()
    wanted = agents_cmd.parse_wanted(args.wanted)
    for warning in agents_cmd.warnings_for(project):
        print(f"warning: {warning}", file=sys.stderr)
    started = agents_cmd.up(project, wanted)
    return {"started": started}, (f"started {len(started)} agent(s)" if started else "all requested agents are running")


@command("autostart")
def _cmd_autostart(args):
    from kankan import agents_cmd
    project = _project()
    if args.action == "disable":
        agents_cmd.autostart_disable(project)
        return {"unit": agents_cmd.unit_name(project)}, "autostart disabled"
    warnings = agents_cmd.autostart_enable(project, agents_cmd.parse_wanted(args.wanted))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return {"unit": agents_cmd.unit_name(project), "warnings": warnings}, f"autostart enabled ({agents_cmd.unit_name(project)})"


@command("skill")
def _cmd_skill(args):
    from kankan.skill_install import skill_install
    lines = skill_install(args.force)
    return {"installed": lines}, "\n".join(lines)


@command("status")
def _cmd_status(args):
    from kankan import diag
    data = diag.status(_project())
    return data, diag.status_text(data)


@command("doctor")
def _cmd_doctor(args):
    from dataclasses import asdict
    from kankan import doctor
    checks = doctor.run(Path(os.getcwd()), fix=not args.no_fix, online=not args.offline)
    data = {"checks": [asdict(c) for c in checks]}
    text = doctor.report_text(checks)
    if any(c.status == doctor.MANUAL for c in checks):
        raise KankanError(Status.NEEDS_ACTION, "manual steps needed\n" + text, data)
    return data, text


@command("merge-request")
def _cmd_merge_request(args):
    from kankan.integrate import merge_request
    card = merge_request(_project(), args.branch, remote=args.remote, top=not args.bottom)
    return {"card": card.id, "short": card.short, "url": card.url, "title": card.name}, f"created {card.url}: {card.name}"


@command("claims")
def _cmd_claims(args):
    from kankan import diag
    rows = diag.claims(_project())
    return rows, diag.claims_text(rows)


@command("reconcile")
def _cmd_reconcile(args):
    from kankan import diag
    issues = diag.reconcile(_project(), args.fix)
    return diag.issues_data(issues), diag.issues_text(issues)


@command("release")
def _cmd_release(args):
    from kankan import diag
    if not args.force:
        raise KankanError(Status.USAGE_ERROR, "release needs --force (it overrides the card's holder)")
    return diag.release_force(_project(), args.card), "card released"


@command("seat")
def _cmd_seat(args):
    from kankan import diag
    project = _project()
    if args.action == "recover":
        return diag.seat_recover(project, args.seat, args.kill), f"seat {args.seat} recovered"
    return diag.seat_remove(project, args.seat), f"seat {args.seat} removed"


@command("purge-remote")
def _cmd_purge_remote(args):
    from kankan import diag
    data = diag.purge_remote(_project(), args.remote)
    return data, f"deleted {len(data['deleted'])} ref(s) from {args.remote}"
