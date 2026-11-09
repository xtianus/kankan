"""Manual live run with real Claude Code and Codex agents on the Trello test board.

    .venv/bin/python -m tests.live.agents_e2e [--no-ask] [--no-hold] [--timeout MIN] [--keep] [--notify USER] [--base DIR]

Never collected by pytest. See docs/developer-manual.md §3.5.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from kankan.trello import TrelloClient
from tests.live import e2e_support as es

POLL = 10
SETTLE_SECONDS = 300
SHUTDOWN_SECONDS = 60
SESSION_PREFIX = f"ai-{es.PROJECT_NAME}-"


def say(text: str = "") -> None:
    print(text, flush=True)


def alert(text: str) -> None:
    print(f"\a\n>>> {text}\n", flush=True)


def prerequisites() -> Optional[tuple]:
    """Return (board_url, key, token, credentials path) or print what is missing and return None."""
    from kankan import doctor
    from tests.live.conftest import live_config
    config = live_config()
    creds = es.live_credentials_path()
    if config is None or creds is None:
        say("no test board configured: create ~/.config/kankan/test.properties (developer manual §3.3)")
        return None
    checks = doctor.run(None, fix=True, types=("claude", "codex"))
    say(doctor.report_text(checks))
    if any(c.status == doctor.MANUAL for c in checks):
        say("fix the manual steps above, then run again")
        return None
    return (*config, creds)


def _tmux():
    from kankan.tmux import Tmux
    return Tmux()


def run_sessions(tmux) -> List[str]:
    return [s for s in tmux.sessions() if s.startswith(SESSION_PREFIX)]


class Watch:
    """Print card changes and guide the user through the interactive steps."""

    def __init__(self, client, cfg, cards: Dict[str, object]):
        self.client, self.cfg, self.cards = client, cfg, cards
        self.last: Dict[str, str] = {}
        self.ask_prompted = self.hold_prompted = self.hold_parked = False

    def poll(self) -> Dict[str, object]:
        snap = self.client.snapshot(self.cfg.board_id)
        names = {l.id: l.name for l in snap.lists.values()}
        label_names = {l.id: l.name for l in snap.labels.values()}
        state = {}
        for key, card in self.cards.items():
            now = snap.cards.get(card.id)
            if now is None:
                continue
            state[key] = now
            labels = [label_names.get(l, "?") for l in now.label_ids]
            line = f"{names.get(now.list_id, '?')}  [{', '.join(labels)}]" if labels else names.get(now.list_id, "?")
            if self.last.get(key) != line:
                say(f"{time.strftime('%H:%M:%S')}  {key:<6} {line}")
                self.last[key] = line
        self.guide(state, names)
        return state

    def guide(self, state, names) -> None:
        cfg = self.cfg
        ask = state.get("ask")
        if ask is not None and cfg.label_question in ask.label_ids and not self.ask_prompted:
            alert(f"The agent asks a question on {ask.url}\n    Reply with a card comment as @{cfg.notify_username}, "
                  "for example: blue")
            self.ask_prompted = True
        hold = state.get("hold")
        if hold is None:
            return
        in_progress = names.get(hold.list_id, "").endswith("· In Progress")
        if in_progress and not self.hold_prompted:
            alert(f"The slow card is in progress: add the HOLD label to {hold.url} now")
            self.hold_prompted = True
        if self.hold_prompted and not self.hold_parked and hold.list_id == cfg.backlog and \
                cfg.label_hold in hold.label_ids:
            alert(f"Work suspended and the card is back in Backlog. Remove HOLD from {hold.url} to let it resume")
            self.hold_parked = True

    def all_done(self, state) -> bool:
        return len(state) == len(self.cards) and all(c.list_id == self.cfg.done for c in state.values())


def save_diagnostics(project, tmux, run_dir: Path) -> Path:
    from kankan import diag
    out = run_dir / "diagnostics"
    out.mkdir(exist_ok=True)
    for name in run_sessions(tmux):
        try:
            pane = tmux.pane_of_session(name)
            text = tmux.run(["capture-pane", "-p", "-S", "-500", "-t", pane]).stdout
            (out / f"{name}.txt").write_text(text)
        except Exception as err:  # noqa: BLE001 - diagnostics are best effort
            (out / f"{name}.error").write_text(str(err))
    log = project.paths.kankan_dir / "log.jsonl"
    if log.exists():
        shutil.copy(log, out / "log.jsonl")
    try:
        (out / "status.txt").write_text(diag.status_text(diag.status(project)))
    except Exception as err:  # noqa: BLE001
        (out / "status.error").write_text(str(err))
    return out


def stop_agents(project, tmux) -> bool:
    """Kill the run's sessions; True if every seat is dead within SHUTDOWN_SECONDS."""
    from kankan import seats
    for name in run_sessions(tmux):
        tmux.kill_session(name)
    deadline = time.monotonic() + SHUTDOWN_SECONDS
    while time.monotonic() < deadline:
        project.reload_config()
        if all(seats.liveness(project, s) != "alive" for s in project.cfg.seats):
            return True
        time.sleep(1)
    return False


def teardown(args, client, board_id, tmux, project, cfg, card_ids, clone, run_dir, ok) -> None:
    """Best effort, immune to a second Ctrl-C: diagnostics, agents, this run's cards and registry block."""
    if args.keep:
        say(f"--keep: agents, cards, registry and {run_dir} left as they are")
        return
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    stopped = True
    try:
        if project is not None:
            if not ok:
                try:
                    say(f"diagnostics: {save_diagnostics(project, tmux, run_dir)}")
                except Exception as err:  # noqa: BLE001
                    say(f"diagnostics failed: {err}")
            try:
                stopped = stop_agents(project, tmux)
            except Exception as err:  # noqa: BLE001
                stopped = False
                say(f"stopping the agents failed: {err}")
            if not stopped:
                say("some agent processes are still alive: the run directory is kept")
        try:
            for action in es.teardown_board(client, board_id, card_ids, cfg.registry_id if cfg else None,
                                            clone_path=str(clone)):
                say(f"  {action}")
        except Exception as err:  # noqa: BLE001
            say(f"board cleanup failed ({err}): run `python -m tests.live.cleanup`")
        if ok and stopped:
            shutil.rmtree(run_dir, ignore_errors=True)
        else:
            say(f"run directory kept: {run_dir}")
    finally:
        signal.signal(signal.SIGINT, previous)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.live.agents_e2e", description=__doc__.splitlines()[0])
    parser.add_argument("--no-ask", action="store_true", help="skip the card that asks you a question")
    parser.add_argument("--no-hold", action="store_true", help="skip the card you put on HOLD")
    parser.add_argument("--timeout", type=int, default=60, help="minutes before giving up (default 60)")
    parser.add_argument("--keep", action="store_true", help="leave agents, cards, registry and run directory")
    parser.add_argument("--notify", help="Trello user who answers (default: the token owner)")
    parser.add_argument("--base", help="directory for the run directory (default: the system temp dir)")
    args = parser.parse_args(argv)

    from kankan.init import parse_board_url
    from tests.live.conftest import MARKER

    pre = prerequisites()
    if pre is None:
        return 2
    board_url, key, token, creds = pre
    for var in ("TMUX", "KANKAN_TMUX_SOCKET"):  # address the user's default tmux server
        os.environ.pop(var, None)
    tmux = _tmux()
    leftovers = run_sessions(tmux)
    if leftovers:
        say(f"tmux sessions of a previous run are still alive: {', '.join(leftovers)}; kill them first")
        return 2
    client = TrelloClient(key, token)
    board = client.board(parse_board_url(board_url))
    if MARKER not in (board.get("desc") or ""):
        say(f"the board description lacks {MARKER!r}: refusing to write")
        return 3
    blocking = es.blocking_cards(client, board["id"])
    if blocking:
        say("the test board has cards in workflow lists; archive them (or run `python -m tests.live.cleanup` for "
            "ke-/kt- leftovers):")
        for card in blocking:
            say(f"  {card.name}  {card.url}")
        return 2

    run_id = es.new_run_id()
    run_dir = Path(args.base or tempfile.gettempdir()) / f"kankan-e2e-{run_id}"
    run_dir.mkdir(parents=True)
    say(f"run {run_id} in {run_dir}")
    clone = run_dir / es.PROJECT_NAME
    cfg = project = None
    card_ids: List[str] = []
    ok = False
    try:
        clone, origin = es.make_repos(run_dir)
        cfg = es.bind(clone, run_dir, board_url, creds, args.notify, say)
        from kankan import doctor
        from kankan.integrate import merge_request
        from kankan.project import Project
        project_checks = [c for c in doctor.run(clone, fix=False, online=True) if c.status == doctor.MANUAL]
        if project_checks:
            say(doctor.report_text(project_checks))
            return 2
        project = Project.open(clone)
        cards = es.create_cards(client, cfg, run_id, es.scenario(ask=not args.no_ask, hold=not args.no_hold))
        card_ids += [c.id for c in cards.values()]
        wt = run_dir / "user-work-wt"
        subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "-b", "user-work", str(wt), "main"], check=True)
        (wt / "user.txt").write_text("from the user\n")
        subprocess.run(["git", "-C", str(wt), "add", "user.txt"], check=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "user work"], check=True)
        subprocess.run(["git", "-C", str(clone), "worktree", "remove", str(wt)], check=True)
        user_refs = {name: subprocess.run(["git", "-C", str(clone), "rev-parse", name], check=True, capture_output=True,
                                          text=True).stdout.strip() for name in ("main", "user-work")}
        cards["merge"] = merge_request(project, "user-work", top=False, title_prefix=f"{run_id} ")
        card_ids.append(cards["merge"].id)

        from kankan import agents_cmd
        agents_cmd.up(project, {"claude": 1, "codex": 1}, tmux=tmux)
        say(f"\nBoard: https://trello.com/b/{cfg.board_short}")
        say(f"Watch: tmux attach -t {SESSION_PREFIX}claude-1    |    tmux attach -t {SESSION_PREFIX}codex-1"
            "    (detach with Ctrl-b d)")
        say(f"Answers to questions come from @{cfg.notify_username}. Run directory: {run_dir}\n")

        watch = Watch(client, cfg, cards)
        deadline = time.monotonic() + args.timeout * 60
        while time.monotonic() < deadline:
            if watch.all_done(watch.poll()):
                break
            time.sleep(POLL)
        else:
            say("timeout: not every card reached Done")
            raise TimeoutError
        say("every card is in Done; waiting for the integration side effects to settle…")
        settle_deadline = time.monotonic() + SETTLE_SECONDS
        while time.monotonic() < settle_deadline and not es.settled(project, client, cards, origin):
            time.sleep(POLL)
        problems = es.verify_run(project, client, cards, origin, ask_ran=not args.no_ask, hold_ran=not args.no_hold,
                                 user_refs=user_refs)
        if problems:
            say("FAILED:")
            for p in problems:
                say(f"  - {p}")
        else:
            say("all checks passed")
            ok = True
    except (KeyboardInterrupt, TimeoutError):
        say("\ninterrupted")
    finally:
        teardown(args, client, board["id"], tmux, project, cfg, card_ids, clone, run_dir, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
