"""Manual live run with real Claude Code and Codex agents on the Trello test board.

    .venv/bin/python -m tests.live.agents_e2e [--clean] [--no-ask] [--no-hold] [--no-attach] [--no-settings]
                                              [--no-qa] [--no-instructions] [--timeout MIN] [--keep] [--base DIR]

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
HEARTBEAT_SECONDS = 120  # how often the watch says which cards are not in Done yet
SHUTDOWN_SECONDS = 60
SESSION_PREFIX = f"ai-{es.PROJECT_NAME}-"
SETTINGS_WAIT_SECONDS = 900
QUIET_SECONDS = 15  # kankan judges a setting card only when its last activity is at least 10 s old


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

    def __init__(self, client, cfg, cards: Dict[str, object], project=None):
        self.client, self.cfg, self.cards, self.project = client, cfg, cards, project
        self.last: Dict[str, str] = {}
        self.ask_prompted = self.hold_prompted = self.hold_parked = self.attach_prompted = False
        self.qa_follow_prompted = self.qa_handoff_prompted = False
        self.strays: Dict[str, str] = {}
        self.next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

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
        self.report_waiting(state, names)
        return state

    def report_waiting(self, state, names) -> None:
        """Warn once per card that leaves the workflow, and say every HEARTBEAT_SECONDS what the run waits for."""
        # the seats, and so their lists, are registered by `kankan up`, after self.cfg was loaded
        current = self.project.reload_config() if self.project is not None else self.cfg
        strays = es.stray_cards(current, self.cards, state, names)
        for key, where in strays.items():
            if self.strays.get(key) != where:
                alert(f"The {key} card is {where}: the run waits for every card in Done, so it will not finish. "
                      f"Move it back to the workflow (or to Done), or stop the run with Ctrl-C")
        self.strays = strays
        if time.monotonic() >= self.next_heartbeat:
            pending = es.not_done(self.cfg, self.cards, state, names)
            if pending:
                say(f"{time.strftime('%H:%M:%S')}  waiting for Done: {', '.join(pending)}")
            self.next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

    def guide(self, state, names) -> None:
        cfg = self.cfg
        ask = state.get("ask")
        if ask is not None and cfg.label_question in ask.label_ids and not self.ask_prompted:
            alert(f"The agent asks a question on {ask.url}\n    Reply with a card comment from your account (not "
                  f"@{cfg.kankan_username}), "
                  "for example: blue")
            self.ask_prompted = True
        attach = state.get("attach")
        if attach is not None and cfg.label_hold in attach.label_ids and not self.attach_prompted:
            alert(f"Attach any small file to {attach.url} from your own account (not @{cfg.kankan_username}): "
                  "\"Add > Attachment > Computer\". Then remove HOLD from the card so an agent takes it")
            self.attach_prompted = True
        self.guide_qa(state.get("qa"))
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

    def guide_qa(self, card) -> None:
        """The Q&A dialogue (questions F§8): a follow-up with an attachment after the first answer, then a request
        to implement after the second; the agent hands the card over to the work seats."""
        cfg = self.cfg
        if card is None or card.list_id != cfg.qa_list or self.qa_handoff_prompted:
            return
        answered = es.qa_turns(self.client, card.id, cfg.kankan_member_id)["qa"]
        if answered >= 1 and not self.qa_follow_prompted:
            alert(f"The Q&A agent answered on {card.url}\n    From your own account (not @{cfg.kankan_username}) "
                  "attach a small text file whose first line is a short phrase (for example `blue sky`), then "
                  "comment: What is the first line of the attached file?")
            self.qa_follow_prompted = True
        elif answered >= 2 and self.qa_follow_prompted:
            alert(f"Comment on {card.url}: Now implement it: append that first line, alone, to qa.txt.\n    If the "
                  "agent asks you to confirm, confirm with a comment. It hands the card over to Backlog, where a work "
                  "seat implements it")
            self.qa_handoff_prompted = True

    def all_done(self, state) -> bool:
        return len(state) == len(self.cards) and all(c.list_id == self.cfg.done for c in state.values())


def choose_settings(client, project, clone) -> tuple:
    """Have the user tick, in the Trello UI, a different model and effort for reviews; return (selection, problems).

    The review values differ from the implementation ones for every agent type, so each seat restarts its agent
    between roles. Done before the agents start, so every task runs with the new values."""
    from kankan import boardconfig, boardsettings, doctor
    cfg, paths = project.cfg, project.paths
    adapters = {t: a for t, a in boardsettings.adapters_for(paths).items() if t in ("claude", "codex")}
    read = client.settings_cards(cfg.settings_list)
    targets = es.settings_targets(read, adapters)
    review = next(c for c in read.cards if c.name == boardconfig.title("review") and not c.closed)
    before = {c.id for c in client.comments(review.id)}
    alert(f"On the `{review.name}` card of the `kankan` list ({review.url}), in each checklist untick the ticked item and "
          "tick:\n" + "\n".join(f"      {t}: {pair.name}" for t, pair in targets.items()))
    deadline = time.monotonic() + SETTINGS_WAIT_SECONDS
    while not es.settings_ready(client.settings_cards(cfg.settings_list), targets):
        if time.monotonic() > deadline:
            say(f"timeout: the review card does not have the ticks above after {SETTINGS_WAIT_SECONDS // 60} min")
            raise TimeoutError
        time.sleep(POLL)
    say(f"ticks seen; waiting {QUIET_SECONDS} s for the card to settle, then `kankan doctor` applies them")
    effective = {}
    for _ in range(4):
        time.sleep(QUIET_SECONDS)
        if not es.settings_ready(client.settings_cards(cfg.settings_list), targets):
            say("the review ticks changed again; waiting for them to match")
            continue
        doctor.run(clone, fix=True, online=True, types=tuple(adapters))
        effective = boardconfig.effective(paths, "review")
        if all(effective.get(t) == pair for t, pair in targets.items()):
            break
    else:
        say(f"timeout: kankan did not apply the review ticks (effective: {boardconfig.display(effective)})")
        raise TimeoutError
    selection = {"implementation": dict(boardconfig.effective(paths, "implementation")), "review": dict(effective)}
    for role, values in selection.items():
        say(f"  {role}: {boardconfig.display(values)}")
    new = [c.text for c in client.comments(review.id) if c.id not in before and c.member_id == cfg.kankan_member_id]
    return selection, es.applied_problems(new, targets)


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


def clean_leftovers(tmux, client, board_id: str, base: Path) -> List[str]:
    """--clean: kill the sessions, board items and run directories that previous runs left behind."""
    actions = []
    for name in run_sessions(tmux):
        tmux.kill_session(name)
        actions.append(f"killed tmux session {name}")
    actions += es.cleanup_board(client, board_id)
    for old in sorted(base.glob("kankan-e2e-ke-*")):
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
            actions.append(f"removed {old}")
    return actions


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tests.live.agents_e2e", description=__doc__.splitlines()[0])
    parser.add_argument("--no-ask", action="store_true", help="skip the card that asks you a question")
    parser.add_argument("--no-hold", action="store_true", help="skip the card you put on HOLD")
    parser.add_argument("--no-attach", action="store_true", help="skip the card you attach a file to")
    parser.add_argument("--no-qa", action="store_true", help="skip the Q&A dialogue (no Q&A seat)")
    parser.add_argument("--no-instructions", action="store_true", help="skip the card with agent review instructions")
    parser.add_argument("--no-settings", action="store_true", help="skip ticking other review models on the board "
                        "(the agents then use the model and effort already ticked)")
    parser.add_argument("--timeout", type=int, default=60, help="minutes before giving up (default 60)")
    parser.add_argument("--keep", action="store_true", help="leave agents, cards, registry and run directory "
                        "(for debugging; clean up later with --clean)")
    parser.add_argument("--clean", action="store_true", help="first kill and remove what previous runs left: "
                        "ai-kankan-e2e-* tmux sessions, ke-/kt- cards and the registry block on the test board, "
                        "kankan-e2e-ke-* run directories")
    parser.add_argument("--clean-only", action="store_true", help="like --clean, then exit without running")
    parser.add_argument("--base", help="directory for the run directory (default: the system temp dir)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Link this checkout's kankan into ~/.local/bin for the run (Codex sees only the login PATH), then run.

    The link is removed at the end, except with --keep while agents are still running on it."""
    args = _parser().parse_args(argv)
    link = Path(os.path.expanduser("~")) / ".local" / "bin" / "kankan"
    try:
        if es.ensure_kankan_link():
            say(f"linked {link} to this checkout's kankan for the run")
    except es.LinkError as err:
        say(str(err))
        return 2
    if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{link.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    state = {"agents_started": False}
    try:
        return _main(args, state)
    finally:
        if args.keep and state["agents_started"]:
            say(f"--keep: {link} stays while the agents run; afterwards: --clean-only (or rm {link})")
        elif es.remove_kankan_link(link):
            say(f"removed {link}")


def _main(args, state) -> int:

    from kankan.init import parse_board_url
    from tests.live.conftest import MARKER

    pre = prerequisites()
    if pre is None:
        return 2
    board_url, key, token, creds = pre
    for var in ("TMUX", "KANKAN_TMUX_SOCKET"):  # address the user's default tmux server
        os.environ.pop(var, None)
    tmux = _tmux()
    client = TrelloClient(key, token)
    board = client.board(parse_board_url(board_url))
    if MARKER not in (board.get("desc") or ""):
        say(f"the board description lacks {MARKER!r}: refusing to write")
        return 3
    base = Path(args.base or tempfile.gettempdir())
    if args.clean or args.clean_only:
        for action in clean_leftovers(tmux, client, board["id"], base) or ["nothing to clean"]:
            say(f"  {action}")
        if args.clean_only:
            return 0
    leftovers = run_sessions(tmux)
    if leftovers:
        say(f"tmux sessions of a previous run are still alive: {', '.join(leftovers)}; rerun with --clean")
        return 2
    blocking = es.blocking_cards(client, board["id"])
    if blocking:
        say("the test board has cards in workflow lists; archive them (ke-/kt- leftovers go away with --clean):")
        for card in blocking:
            say(f"  {card.name}  {card.url}")
        return 2

    run_id = es.new_run_id()
    run_dir = base / f"kankan-e2e-{run_id}"
    run_dir.mkdir(parents=True)
    say(f"run {run_id} in {run_dir}")
    clone = run_dir / es.PROJECT_NAME
    cfg = project = None
    card_ids: List[str] = []
    ok = False
    try:
        clone, origin = es.make_repos(run_dir)
        cfg = es.bind(clone, run_dir, board_url, creds, say)
        from kankan import doctor
        from kankan.integrate import merge_request
        from kankan.project import Project
        project_checks = [c for c in doctor.run(clone, fix=False, online=True) if c.status == doctor.MANUAL]
        if project_checks:
            say(doctor.report_text(project_checks))
            return 2
        project = Project.open(clone)
        qa_type = None if args.no_qa else es.QA_TYPE
        if qa_type:
            # Register the Q&A seat first: `doctor` then asks its `questions` seeds and creates its setting card.
            from kankan import seats
            seats.ensure_created(project, seats.qa_seat_name(qa_type), qa_type)
            qa_checks = [c for c in doctor.run(clone, fix=True, online=True) if c.status == doctor.MANUAL]
            if qa_checks:
                say(doctor.report_text(qa_checks))
                return 2
        selection, pre_problems = (None, []) if args.no_settings else choose_settings(client, project, clone)
        cards = es.create_cards(client, cfg, run_id, es.scenario(ask=not args.no_ask, hold=not args.no_hold,
                                                                 attach=not args.no_attach, qa=qa_type is not None,
                                                                 instructions=not args.no_instructions))
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
        state["agents_started"] = True
        agents_cmd.up(project, {"claude": 1, "codex": 1}, tmux=tmux, qa=qa_type)
        say(f"\nBoard: https://trello.com/b/{cfg.board_short}")
        say(f"Watch: tmux attach -t {SESSION_PREFIX}claude-1    |    tmux attach -t {SESSION_PREFIX}codex-1"
            + (f"    |    tmux attach -t {SESSION_PREFIX}{qa_type}-qa" if qa_type else "")
            + "    (detach with Ctrl-b d)")
        say(f"kankan acts as @{cfg.kankan_username}; answer from your own account. Run directory: {run_dir}\n")

        watch = Watch(client, cfg, cards, project)
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
        sha256, problems = None, list(pre_problems)
        if "attach" in cards:
            upload = es.user_upload(client, cards["attach"].id, cfg.kankan_member_id)
            if upload is None:
                problems.append(f"{cards['attach'].name}: no file uploaded by a board member other than kankan")
                sha256 = ""
            else:
                sha256 = es.attachment_sha256(client, cards["attach"].id, upload, cfg.attachment_max_bytes)
        qa_line = None
        if "qa" in cards:
            upload = es.user_upload(client, cards["qa"].id, cfg.kankan_member_id)
            if upload is None:
                problems.append(f"{cards['qa'].name}: no file uploaded by a board member other than kankan")
                qa_line = ""
            else:
                qa_line = es.first_line(es.attachment_bytes(client, cards["qa"].id, upload, cfg.attachment_max_bytes))
        problems += es.verify_run(project, client, cards, origin, ask_ran=not args.no_ask, hold_ran=not args.no_hold,
                                  user_refs=user_refs, attach_sha256=sha256, selection=selection, qa_line=qa_line)
        from kankan import auditlog
        events = auditlog.read(project.paths)
        if selection is not None:
            for line in es.restart_report(events)[1]:
                say(f"observed: {line}")
        covered = sorted({str(es.reviewer_seat(events, cards[key].id)).split("-")[0]
                          for key in es.INSTRUCTION_CARDS if key in cards})
        if covered:
            say(f"observed: card-instructions reviews by {', '.join(covered)}")
        if "hold" in cards and es.hold_path(events, cards["hold"].id):
            say(f"observed: HOLD noticed first by {es.hold_path(events, cards['hold'].id)}")
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
