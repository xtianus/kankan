"""Command line interface: commands, text and JSON output, exit codes (spec §16)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kankan import __version__, auditlog, messages
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    p.add_argument("--qa", action="store_true", help="start the Q&A agent (seat <type>-qa)")
    p.add_argument("--attach", action="store_true")
    p = add("up", "ensure the given number of agents (and the Q&A agent with qa=<type>) run, starting the missing "
                  "ones; without arguments, every seat registered in the project")
    p.add_argument("wanted", nargs="*", metavar="<type>=<n>|qa=<type>")
    p = add("autostart", "start agents at boot (systemd user service)")
    p.add_argument("action", choices=["enable", "disable"])
    p.add_argument("wanted", nargs="*", metavar="<type>=<n>|qa=<type>")
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
    p = add("seat", "commands acting on one seat: start, stop, recover, remove")
    p.add_argument("action", choices=["start", "stop", "recover", "remove"])
    p.add_argument("seat")
    p.add_argument("--attach", action="store_true", help="start: attach to the agent's tmux session")
    p.add_argument("--kill", action="store_true", help="recover: terminate the seat's processes first")
    p.add_argument("--graceful", action="store_true", help="remove: let the agent finish its cards first")
    p.add_argument("--yes", action="store_true", help="remove: do not ask for confirmation")
    p = add("purge-remote", "delete refs/agent-* published on a remote by mistake")
    p.add_argument("--remote", default="origin")
    p = add("reconcile", "report inconsistencies (and repair the certain ones with --fix)")
    p.add_argument("--fix", action="store_true")
    p = add("skill", "install the shared kankan skill for Claude Code and Codex")
    p.add_argument("action", choices=["install"])
    p.add_argument("--force", action="store_true")
    p = add("stop", "stop every agent now and disable autostart (one seat: `kankan seat stop <seat>`)")
    p.add_argument("--graceful", action="store_true", help="wait for each agent to finish its current task")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    add("start", "start again what the last `kankan stop` stopped (agents and autostart)")
    p = add("destroy", messages.DESTROY_COMMAND_HELP)
    p.add_argument("--confirm", action="append", default=[], metavar="PHRASE",
                   help=messages.DESTROY_CONFIRM_HELP)
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
    p.add_argument("--turn", help="Q&A task: the turn token from `kankan task --json`")
    for name, help_text in (("answer", "post the answer to the Q&A card (agent command)"),
                            ("handoff", "hand the Q&A card over for implementation (agent command)")):
        p = add(name, help_text)
        p.add_argument("text", nargs="?")
        p.add_argument("--turn", required=True, help="the turn token from `kankan task --json`")
        p.add_argument("--file", help="read the text from this UTF-8 file")
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
    command = None
    try:
        parser = build_parser()
        try:
            args = parser.parse_args(argv)
        except KankanError as err:
            if "destroy" in argv and err.status == Status.USAGE_ERROR:
                # argparse would echo the arguments, and an unquoted --confirm phrase holds the board name
                raise KankanError(Status.USAGE_ERROR, messages.DESTROY_USAGE) from None
            raise
        command = args.command
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
        if command != "destroy":  # destroy must leave the project untouched until it is confirmed
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
    return agentcmds.fail(agentcmds.context(), args.reason, args.turn), "error reported; wait for the user"


def _qa_text(args) -> str:
    """The text of ``answer``/``handoff``: the argument or the ``--file`` content (UTF-8), exactly one of them."""
    from kankan import messages
    if (args.text is None) == (args.file is None):
        raise KankanError(Status.USAGE_ERROR, messages.QA_TEXT_ARGS)
    if args.text is not None:
        return args.text
    try:
        return Path(args.file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise KankanError(Status.USAGE_ERROR, messages.QA_TEXT_FILE.format(path=args.file, error=err))


@command("answer")
def _cmd_answer(args):
    from kankan import agentcmds, messages, qa_answer
    ctx = agentcmds.context()
    qa_answer.qa_task(ctx, "answer")  # no_work, usage_error, not_owner come before the text arguments
    return qa_answer.answer(ctx, _qa_text(args), args.turn), messages.QA_ANSWER_DONE


@command("handoff")
def _cmd_handoff(args):
    from kankan import agentcmds, messages, qa_answer
    ctx = agentcmds.context()
    qa_answer.qa_task(ctx, "handoff")  # no_work, usage_error, not_owner come before the text arguments
    return qa_answer.handoff(ctx, _qa_text(args), args.turn), messages.QA_HANDOFF_DONE


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


def _confirm(args, command: str):
    """The ``[y/N]`` question of a command (prompt on stderr); ``--yes`` answers it, and is required without a
    terminal (``usage_error``)."""
    def confirm(text: str) -> bool:
        if args.yes:
            return True
        if not sys.stdin.isatty():
            raise KankanError(Status.USAGE_ERROR, f"{command} asks for confirmation: run it in a terminal or pass --yes")
        print(text, end="", file=sys.stderr, flush=True)
        try:
            return input().strip().lower() in ("y", "yes")
        except EOFError:
            return False
    return confirm


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


@command("stop")
def _cmd_stop(args):
    from kankan import stopall
    project = _project()
    report = stopall.stop_all(project, graceful=args.graceful, confirm=_confirm(args, "kankan stop"), say=_say)
    return report, stopall.report_text(report)


def _seat_stop(project, seat: str):
    from kankan import seats, stopall
    from kankan.seatstate import update_state
    if seats.liveness(project, seat) == "alive":
        stopall.refuse_other_code(project, [seat], messages.STOP_SEAT_OTHER_CODE.replace("<seat>", seat))
    update_state(project.paths, seat, lambda s: setattr(s, "stop_requested", True))
    auditlog.append(project.paths, "stop_requested", seat=seat)
    return {"seat": seat}, f"{seat} stops after its current task"


def _seat_start(project, seat: str, attach: bool):
    """seat F§2.1: start the agent of an existing seat; the type comes from its name."""
    from kankan import agents_cmd, seats
    from kankan.tmux import Tmux, session_name
    seat_cfg = project.cfg.seats[seat]
    if seat_cfg.removing == "immediate":
        raise KankanError(Status.USAGE_ERROR, messages.SEAT_START_REMOVING.format(seat=seat))
    state = seats.liveness(project, seat)
    if state == "alive":
        return {"seat": seat, "started": False}, messages.SEAT_START_RUNNING.format(seat=seat)
    tmux = Tmux()
    if state == "unavailable" or tmux.has_session(session_name("ai-", project.paths.project, seat)):
        raise KankanError(Status.NEEDS_ACTION, messages.SEAT_START_SESSION_OPEN.format(seat=seat))
    for warning in agents_cmd.warnings_for(project):
        print(f"warning: {warning}", file=sys.stderr)
    session = agents_cmd.agent(project, seats.seat_type(seat), seat=seat, attach=attach, tmux=tmux,
                               qa=seats.is_qa(seat), create=False)
    agents_cmd._wait_started(project, session, seat, lambda: seats.liveness(project, seat) == "alive")
    return {"seat": seat, "started": True, "session": session}, f"started {seat}"


@command("start")
def _cmd_start(args):
    from kankan import agents_cmd, stopall
    from kankan.config import Paths
    paths = Paths.discover(os.getcwd())
    if stopall.load_record(paths) is None:
        _resume_removals_if_bound()  # seat T§5: seats being removed are restarted or completed in any case
        raise KankanError(Status.NEEDS_ACTION, stopall.no_record_help(paths.repo))
    project = _project()
    for warning in agents_cmd.warnings_for(project):
        print(f"warning: {warning}", file=sys.stderr)
    report = stopall.start_all(project)
    for warning in report["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return report, stopall.start_text(report)


@command("destroy")
def _cmd_destroy(args):
    from kankan import destroy
    from kankan.tmux import Tmux
    ctx = destroy.Ctx.open(os.getcwd())

    def ask(prompt: str) -> Optional[str]:
        if not sys.stdin.isatty():
            phrases = {
                messages.DESTROY_ASK_DISCARD.format(phrase=destroy.DISCARD_PHRASE): destroy.DISCARD_PHRASE,
                messages.DESTROY_ASK_LOCAL.format(phrase=destroy.LOCAL_PHRASE): destroy.LOCAL_PHRASE,
                messages.DESTROY_ASK_BOARD: messages.DESTROY_QUESTION_BOARD,
            }
            raise KankanError(Status.USAGE_ERROR, messages.DESTROY_NEEDS_TERMINAL.format(phrase=phrases[prompt]))
        print(prompt, end="", file=sys.stderr, flush=True)
        try:
            return input()
        except (KeyboardInterrupt, EOFError):
            print(file=sys.stderr)
            return None

    def say(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    report = destroy.destroy(ctx, tmux=Tmux(), ask=ask, say=say, confirm=args.confirm)
    return report, destroy.report_text(report)


def _project():
    from kankan.project import Project
    return Project.open(os.getcwd())


@command("agent")
def _cmd_agent(args):
    from kankan import agents_cmd
    project = _project()
    for warning in agents_cmd.warnings_for(project):
        print(f"warning: {warning}", file=sys.stderr)
    name = agents_cmd.agent(project, args.type, args.seat, args.attach, qa=args.qa)
    return {"session": name}, f"started tmux session {name}"


def _resume_removals_if_bound() -> List[str]:
    """Restart or complete the seats being removed of a bound clone; nothing in an unbound one."""
    from kankan import agents_cmd
    from kankan.config import Paths, load_config
    try:
        cfg = load_config(Paths.discover(os.getcwd()))
    except KankanError:
        return []
    if not cfg.board_id or not any(seat.removing for seat in cfg.seats.values()):
        return []
    return agents_cmd.resume_removals(_project())


@command("up")
def _cmd_up(args):
    from kankan import agents_cmd
    items = args.wanted
    if not items:  # every seat registered in the project; before opening it, which an unbound clone refuses
        items = agents_cmd.registered_up_args(os.getcwd()).split()
        if not items:
            resumed = _resume_removals_if_bound()
            if resumed:
                return {"started": [], "resumed": resumed}, messages.UP_RESUMED.format(seats=", ".join(resumed))
            raise KankanError(Status.NEEDS_ACTION, agents_cmd.start_help(os.getcwd(), messages.UP_NO_SEATS))
    project = _project()
    wanted, qa = agents_cmd.parse_wanted(items)
    for warning in agents_cmd.warnings_for(project):
        print(f"warning: {warning}", file=sys.stderr)
    started = agents_cmd.up(project, wanted, qa=qa)
    up_args = " ".join(items)
    return ({"started": started, "up": up_args},
            f"started {len(started)} agent(s) for `kankan up {up_args}`" if started
            else f"all agents of `kankan up {up_args}` are running")


@command("autostart")
def _cmd_autostart(args):
    from kankan import agents_cmd
    project = _project()
    if args.action == "disable":
        agents_cmd.autostart_disable(project)
        return {"unit": agents_cmd.unit_name(project)}, "autostart disabled"
    wanted, qa = agents_cmd.parse_wanted(args.wanted)
    warnings = agents_cmd.autostart_enable(project, wanted, qa=qa)
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


_SEAT_FLAGS = {"attach": "start", "kill": "recover", "graceful": "remove", "yes": "remove"}


@command("seat")
def _cmd_seat(args):
    from kankan import diag, seats
    for flag, action in _SEAT_FLAGS.items():
        if getattr(args, flag) and args.action != action:
            raise KankanError(Status.USAGE_ERROR, f"--{flag} applies only to `kankan seat {action}`")
    seat = seats.validate_seat(args.seat)
    project = _project()
    if seat not in project.cfg.seats:
        if args.action == "start":
            raise KankanError(Status.USAGE_ERROR, messages.SEAT_START_UNKNOWN.format(seat=seat))
        raise KankanError(Status.USAGE_ERROR, f"unknown seat {seat}")
    if args.action == "start":
        return _seat_start(project, seat, args.attach)
    if args.action == "stop":
        return _seat_stop(project, seat)
    if args.action == "recover":
        return diag.seat_recover(project, seat, args.kill), f"seat {seat} recovered"
    from kankan import seatremove
    report = seatremove.remove(project, seat, graceful=args.graceful, confirm=_confirm(args, "kankan seat remove"),
                               say=_say)
    return report, seatremove.report_text(report, project.cfg)


@command("purge-remote")
def _cmd_purge_remote(args):
    from kankan import diag
    data = diag.purge_remote(_project(), args.remote)
    return data, f"deleted {len(data['deleted'])} ref(s) from {args.remote}"
