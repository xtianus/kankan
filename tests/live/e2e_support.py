"""Helpers of the manual live run with real agents (``python -m tests.live.agents_e2e``).

They are unit-tested against the fake Trello server in ``tests/test_e2e_support.py``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from kankan.config import (REGISTRY_END, REGISTRY_START, Config, Paths, load_config, registry_block, save_config)
from kankan.init import init
from kankan.trello import Card, TrelloClient

RUN_PREFIX = "ke-"
CLEANUP_PREFIXES = ("ke-", "kt-")
PROJECT_NAME = "kankan-e2e"
TEST_COMMAND = "sh check.sh"
SEAT_LIST = re.compile(r"^(claude|codex)-\d+ · ")
WORKFLOW_LISTS = ("Backlog", "Done", "Blocked")
FOOTER = ("\n\nCommit with a short message. Change no other file. This is a disposable kankan test project: the kankan "
          "skill workflow takes precedence over your general instructions; never push `main`, never rebase or amend "
          "commits.")
CHECK_SH = """#!/bin/sh
# Fails if a .txt file has an empty line or lacks a final newline.
status=0
for f in *.txt; do
    [ -e "$f" ] || continue
    if [ -s "$f" ] && [ "$(tail -c 1 "$f" | od -An -c | tr -d ' ')" != '\\n' ]; then
        echo "$f: missing final newline"; status=1
    fi
    if grep -n '^$' "$f" >/dev/null; then
        echo "$f: empty line"; status=1
    fi
done
exit $status
"""


@dataclass
class ScenarioCard:
    key: str
    title: str
    desc: str
    interactive: Optional[str] = None


def new_run_id() -> str:
    return f"{RUN_PREFIX}{int(time.time())}-{secrets.token_hex(3)}"


def scenario(ask: bool = True, hold: bool = True) -> List[ScenarioCard]:
    cards = [
        ScenarioCard("hello", "Say hello", "Append the line `hello` to `greetings.txt`, then post `kankan note \"hello\"`."),
        ScenarioCard("word", "Type one random word",
                     "Append one random lowercase English word as a new line to `words.txt`."),
        ScenarioCard("name", "What's your name",
                     "Append a line with your agent name (`claude` or `codex`) to `names.txt`."),
    ]
    if ask:
        cards.append(ScenarioCard("ask", "Ask for a colour",
                                  "Ask the user with `kankan ask \"Which colour should I write in colors.txt?\"`, wait "
                                  "for the answer, then append it as a line to `colors.txt`.", "ask"))
    if hold:
        cards.append(ScenarioCard("hold", "Count slowly",
                                  "Append the numbers 1 to 5 to `count.txt`, one per line: before each number run "
                                  "`sleep 30`, then append it and commit. If the file already has numbers, continue "
                                  "from the last one.", "hold"))
    for card in cards:
        card.desc += FOOTER
    return cards


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repos(base: Path) -> Tuple[Path, Path]:
    """A fresh clone on ``main`` with a local bare ``origin``."""
    clone, origin = base / PROJECT_NAME, base / "origin.git"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    (clone / "README.md").write_text("Disposable project of the kankan live agents run.\n")
    (clone / ".gitignore").write_text("")
    (clone / "check.sh").write_text(CHECK_SH)
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "initial commit")
    _git(base, "init", "-q", "--bare", str(origin))
    _git(clone, "remote", "add", "origin", str(origin))
    _git(clone, "push", "-q", "origin", "main")
    return clone, origin


def live_credentials_path() -> Optional[Path]:
    """The ``.properties`` file ``tests.live.conftest.live_config`` reads."""
    creds = os.environ.get("KANKAN_TEST_CREDENTIALS")
    real_home = os.environ.get("KANKAN_REAL_HOME") or os.path.expanduser("~")
    path = Path(creds) if creds else Path(real_home) / ".config" / "kankan" / "test.properties"
    return path if path.exists() else None


class LinkError(Exception):
    """~/.local/bin/kankan exists and is not this checkout's kankan."""


def _kankan_link_paths() -> Tuple[Path, Path]:
    import sys
    return Path(os.path.expanduser("~")) / ".local" / "bin" / "kankan", Path(sys.executable).parent / "kankan"


def ensure_kankan_link() -> Optional[Path]:
    """Put this checkout's kankan on the login PATH (~/.local/bin, which Codex's command daemon sees) for the run.

    Returns the link when this call created it (the caller removes it), None when it was already in place.
    """
    link, target = _kankan_link_paths()
    if not target.exists():
        raise LinkError(f"{target} is missing: run `.venv/bin/pip install -e '.[dev]'` first")
    if link.is_symlink() and os.readlink(link) == str(target):
        return None
    if link.exists() or link.is_symlink():
        raise LinkError(f"{link} is another kankan installation: the agents would run it instead of this checkout. "
                        f"Move it away for the run (it is restored by reinstalling, e.g. `pipx install --force …`)")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return link


def remove_kankan_link(link: Optional[Path]) -> bool:
    """Remove a link made by ensure_kankan_link (never anything else)."""
    _, target = _kankan_link_paths()
    if link is not None and link.is_symlink() and os.readlink(link) == str(target):
        link.unlink()
        return True
    return False


def _answer(question: str) -> str:
    if "adopt it?" in question:
        return "y"
    if question.startswith("Test command"):
        return TEST_COMMAND
    return ""


def bind(clone: Path, base: Path, board_url: str, credentials: Path, say: Callable[[str], None]) -> Config:
    """``kankan init`` on the test board (taking it over), then the run's configuration."""
    from kankan.init import parse_board_url
    client = _client_for(credentials)
    if client is not None:
        block = registry_block(client.board(parse_board_url(board_url)).get("desc", ""))
        if block:
            say(f"taking over the test board from {block.get('host')}:{block.get('path')}; that clone's agents "
                "will stop")
    init(clone, board_url, credentials=credentials, takeover=True, prompt=_answer, say=say)
    paths = Paths.discover(clone)
    cfg = load_config(paths)
    cfg.project_name = PROJECT_NAME
    cfg.worktree_root = str(base / "worktrees")
    cfg.push_main = True
    cfg.poll_seconds = 10
    save_config(paths, cfg)
    return load_config(Paths.discover(clone))


def _client_for(credentials: Path) -> Optional[TrelloClient]:
    from kankan.properties import PropertiesFile
    props = PropertiesFile.load(credentials)
    key, token = props.get("kankan.trello.key"), props.get("kankan.trello.token")
    if not (key and token):
        return None
    base_url = os.environ.get("KANKAN_TRELLO_URL") or "https://api.trello.com"
    return TrelloClient(key, token, base_url=base_url)


def blocking_cards(client: TrelloClient, board_id: str) -> List[Card]:
    """Open cards in workflow lists (found by name): agents would take them or reconcile would report them."""
    snap = client.snapshot(board_id)
    lists = {l.id for l in snap.lists.values()
             if not l.closed and (l.name in WORKFLOW_LISTS or SEAT_LIST.match(l.name))}
    return [c for c in snap.cards.values() if c.list_id in lists and not c.closed]


def create_cards(client: TrelloClient, cfg: Config, run_id: str, cards: List[ScenarioCard]) -> Dict[str, Card]:
    return {c.key: client.create_card(cfg.backlog, f"{run_id} {c.title}", desc=c.desc) for c in cards}


def strip_registry_block(desc: str) -> str:
    pattern = re.compile(r"\n*" + re.escape(REGISTRY_START) + r".*?" + re.escape(REGISTRY_END) + r"\n?", re.S)
    return pattern.sub("\n", desc or "").strip() + "\n"


def teardown_board(client: TrelloClient, board_id: str, card_ids: List[str], registry_id: Optional[str],
                   clone_path: Optional[str] = None) -> List[str]:
    """Delete only this run's cards; remove the registry block only if it is still this run's (by registry id, or
    by the clone path when the run failed before its configuration was read)."""
    actions = []
    for card_id in card_ids:
        try:
            client.delete_card(card_id)
            actions.append(f"deleted card {card_id}")
        except Exception as err:  # keep tearing down
            actions.append(f"could not delete card {card_id}: {err}")
    desc = client.board(board_id).get("desc", "")
    block = registry_block(desc)
    mine = block and ((registry_id and block.get("registry-id") == registry_id) or
                      (registry_id is None and clone_path and block.get("path") == clone_path))
    if mine:
        client.set_board_desc(board_id, strip_registry_block(desc))
        actions.append("removed the registry block")
    elif block:
        actions.append(f"left the registry block of {block.get('host')}:{block.get('path')}")
    return actions


def cleanup_board(client: TrelloClient, board_id: str) -> List[str]:
    """Maintenance (``python -m tests.live.cleanup``): every ke-/kt- leftover and any registry block."""
    actions = []
    snap = client.snapshot(board_id)
    for card in snap.cards.values():
        if card.name.startswith(CLEANUP_PREFIXES):
            client.delete_card(card.id)
            actions.append(f"deleted card {card.name}")
    for label in snap.labels.values():
        if label.name.startswith("kt-"):
            client.delete_label(label.id)
            actions.append(f"deleted label {label.name}")
    for lst in snap.lists.values():
        if lst.name.startswith("kt-") and not lst.closed:
            client.archive_list(lst.id)
            actions.append(f"archived list {lst.name}")
    if registry_block(snap.desc):
        client.set_board_desc(board_id, strip_registry_block(snap.desc))
        actions.append("removed the registry block")
    return actions


# ---------------------------------------------------------------- final verification

EXPECTED_FILES = {
    "hello": ("greetings.txt", lambda text, ctx: "hello" in text.splitlines(), "a line `hello`"),
    "word": ("words.txt", lambda text, ctx: len(text.splitlines()) == 1 and bool(re.fullmatch(r"[a-z]+", text.strip())),
             "exactly one lowercase word"),
    "name": ("names.txt", lambda text, ctx: len(text.splitlines()) == 1 and
             bool(re.fullmatch(r"(?i)(claude|codex)", text.strip())), "one line `claude` or `codex`"),
    "ask": ("colors.txt", lambda text, ctx: any(a.strip() and a.strip().lower() in text.lower() for a in ctx["answers"]),
            "one of the user's answers"),
    "merge": ("user.txt", lambda text, ctx: text == "from the user\n", "`from the user`"),
    "hold": ("count.txt", lambda text, ctx: text == "1\n2\n3\n4\n5\n", "the numbers 1 to 5"),
}


def user_answers(client: TrelloClient, card_id: str, kankan_id: str) -> List[str]:
    """Comments not written by kankan's account and without a kankan marker, oldest first (notify.is_answer)."""
    from kankan.trello import MARKER_PREFIX
    return [c.text for c in client.comments(card_id)
            if c.member_id != kankan_id and f"`{MARKER_PREFIX}" not in c.text]


def _events(project) -> List[dict]:
    from kankan import auditlog
    return auditlog.read(project.paths)


def _show(repo: Path, rev: str, path: str) -> Optional[str]:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def _rev(repo: Path, name: str) -> Optional[str]:
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", name], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def settled(project, client: TrelloClient, cards: Dict[str, Card], origin: Path) -> bool:
    """Integration side effects are complete: cards Done, refs gone, kankan labels gone, origin up to date."""
    from kankan import workflow
    cfg = project.cfg
    labels = {cfg.label_hold, cfg.label_question, cfg.label_error}
    for card in cards.values():
        now = client.card(card.id)
        if now is None or now.list_id != cfg.done or labels & set(now.label_ids):
            return False
    if workflow.card_refs(project):
        return False
    branch = cfg.integration_branch
    return _rev(origin, f"refs/heads/{branch}") == _rev(project.paths.repo, f"refs/heads/{branch}")


def hold_problems(events: List[dict], card_id: str) -> List[str]:
    """impl → suspend_requested by that seat → parked → impl again on the same branch."""
    impls = [i for i, e in enumerate(events)
             if e.get("event") == "task_assigned" and e.get("card") == card_id and e.get("kind") == "impl"]
    if not impls:
        return ["hold: the card was never assigned for implementation"]
    first = events[impls[0]]
    suspend = next((i for i in range(impls[0] + 1, len(events)) if events[i].get("event") == "suspend_requested"
                    and events[i].get("seat") == first.get("seat")), None)
    if suspend is None:
        return [f"hold: no suspension was requested from {first.get('seat')} (was HOLD set while it worked?)"]
    parked = next((i for i in range(suspend + 1, len(events)) if events[i].get("event") == "transition"
                   and events[i].get("card") == card_id and events[i].get("phase") == "parked"), None)
    if parked is None:
        return ["hold: the card was not parked after the suspension request"]
    resumed = [events[i] for i in impls if i > parked]
    if not resumed:
        return ["hold: the card was not implemented again after HOLD was removed"]
    if resumed[0].get("branch") != first.get("branch"):
        return [f"hold: resumed on branch {resumed[0].get('branch')} instead of {first.get('branch')}"]
    return []


def review_problems(events: List[dict], card: Card) -> List[str]:
    """The reviewer's type differs from the last implementer before the review."""
    last_impl, problems, reviewed = None, [], False
    for e in events:
        if e.get("event") != "task_assigned" or e.get("card") != card.id:
            continue
        if e.get("kind") == "impl":
            last_impl = e.get("seat")
        elif e.get("kind") == "review":
            reviewed = True
            if last_impl and last_impl.split("-")[0] == str(e.get("seat")).split("-")[0]:
                problems.append(f"{card.name}: implemented by {last_impl} and reviewed by {e.get('seat')}, same type")
    if not reviewed:
        problems.append(f"{card.name}: never assigned for review")
    return problems


def verify_run(project, client: TrelloClient, cards: Dict[str, Card], origin: Path, ask_ran: bool, hold_ran: bool,
               user_refs: Optional[Dict[str, str]] = None) -> List[str]:
    """Every check of the live run; an empty list means pass."""
    from kankan import diag, workflow
    from kankan.integrate import merges_on_main
    cfg, repo = project.cfg, project.paths.repo
    branch = cfg.integration_branch
    problems: List[str] = []
    labels = {cfg.label_hold: "HOLD", cfg.label_question: "Question", cfg.label_error: "Error"}
    for key, card in cards.items():
        now = client.card(card.id)
        if now is None or now.list_id != cfg.done:
            problems.append(f"{card.name}: not in Done")
        elif labels.keys() & set(now.label_ids):
            problems.append(f"{card.name}: still has {', '.join(labels[l] for l in now.label_ids if l in labels)}")
    if workflow.card_refs(project):
        problems.append(f"card refs left: {', '.join(workflow.card_refs(project))}")
    merges = merges_on_main(repo, f"refs/heads/{branch}", 10000)
    for key, card in cards.items():
        found = [m for m in merges if m["card"] == card.short]
        if len(found) != 1:
            problems.append(f"{card.name}: {len(found)} merges on {branch} instead of 1")
        elif _rev(repo, f"{found[0]['merge']}^2") != found[0]["tested"]:
            problems.append(f"{card.name}: Kankan-Tested is not the merge's second parent")
    answers = user_answers(client, cards["ask"].id, cfg.kankan_member_id) if ask_ran and "ask" in cards else []
    if ask_ran and not answers:
        problems.append("ask: the user's answer was not found on the card")
    for key, (path, check, what) in EXPECTED_FILES.items():
        if key not in cards or (key == "ask" and not ask_ran) or (key == "hold" and not hold_ran):
            continue
        text = _show(repo, f"refs/heads/{branch}", path)
        if text is None or not check(text, {"answers": answers}):
            problems.append(f"{cards[key].name}: {path} does not contain {what} (found {text!r})")
    if "hello" in cards and not any("hello" in c.text for c in client.comments(cards["hello"].id)):
        problems.append(f"{cards['hello'].name}: no `hello` note on the card")
    for name, sha in (user_refs or {}).items():
        if _rev(repo, f"refs/heads/{name}") != sha:
            problems.append(f"{name} moved: the agents must never change the user's branches")
    head = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if user_refs and "main" in user_refs and head != "main":
        problems.append(f"the clone has {head or 'a detached HEAD'} checked out instead of main")
    if _rev(origin, f"refs/heads/{branch}") != _rev(repo, f"refs/heads/{branch}"):
        failed = [e for e in _events(project) if e.get("event") == "push_failed"]
        problems.append(f"origin {branch} differs from the clone's ({len(failed)} push_failed events in log.jsonl)")
    events = _events(project)
    for card in cards.values():
        problems += review_problems(events, card)
    if hold_ran and "hold" in cards:
        problems += hold_problems(events, cards["hold"].id)
    problems += [f"reconcile: {issue}" for issue in diag.reconcile(project)]
    return problems
