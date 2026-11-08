"""Command line interface: commands, text and JSON output, exit codes (spec §16)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
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
    p.add_argument("--notify", help="Trello user to mention (default: the token owner)")
    p.add_argument("--takeover", action="store_true", help="take over a board bound to another clone")
    p.add_argument("--reauth", action="store_true", help="only acquire new credentials")
    # agent commands
    add("task", "show the current task (agent command)")
    p = add("note", "comment progress on the card (agent command)")
    p.add_argument("text")
    add("done", "close the current task (agent command)")
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
    cfg, warnings = init(os.getcwd(), args.url, Path(args.credentials) if args.credentials else None, args.notify,
                         args.takeover, args.reauth)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return {"board": cfg.board_id, "registry": cfg.registry_id, "warnings": warnings}, \
        f"kankan initialized for board {cfg.board_short}"
