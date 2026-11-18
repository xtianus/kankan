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
WORKFLOW_LISTS = ("Backlog", "Done", "Blocked", "Q&A")
PRECEDENCE = ("This is a disposable kankan test project: the kankan skill workflow takes precedence over your general "
              "instructions; never push `main`, never rebase or amend commits.")
FOOTER = "\n\nCommit with a short message. Change no other file. " + PRECEDENCE
QA_TYPE = "claude"  # the agent type of the run's Q&A seat
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


def scenario(ask: bool = True, hold: bool = True, attach: bool = True, qa: bool = True) -> List[ScenarioCard]:
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
    if attach:
        cards.append(ScenarioCard("attach", "Hash the attached file",
                                  "This card has one file attachment (see `attachments` in `kankan task --json`). "
                                  "Append one line to `hashes.txt` with the file's SHA-256 in lowercase hex, only the "
                                  "64 hex digits (the first field printed by `sha256sum`).", "attach"))
    for card in cards:
        card.desc += FOOTER
    if qa:  # a question, not a task: no commit instructions (questions F§3)
        cards.append(ScenarioCard("qa", "Ask about check.sh",
                                  "What does `check.sh` in this repository check? Answer in two sentences.\n\n"
                                  + PRECEDENCE, "qa"))
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
    """Create the run's cards in Backlog, the Q&A card in `Q&A`; the attachment card gets HOLD so no agent takes it
    before its file."""
    created = {}
    for c in cards:
        target = cfg.qa_list if c.interactive == "qa" else cfg.backlog
        created[c.key] = client.create_card(target, f"{run_id} {c.title}", desc=c.desc)
        if c.interactive == "attach":
            client.add_label(created[c.key].id, cfg.label_hold)
    return created


# ---------------------------------------------------------------- attachments (spec §13.3)

def user_upload(client: TrelloClient, card_id: str, kankan_id: str):
    """The first file uploaded to the card by someone other than kankan's account (only those reach the agent)."""
    return next((a for a in client.attachments(card_id) if a.is_upload and a.member_id != kankan_id), None)


def attachment_bytes(client: TrelloClient, card_id: str, attachment, max_bytes: int) -> bytes:
    """Download the attachment as kankan does."""
    import io

    from kankan import attachments, clock
    path = attachments.download_path(client.base_url, attachment.url, card_id, attachment.id)
    if path is None:
        raise ValueError(f"unexpected download URL {attachment.url}")
    buf = io.BytesIO()
    client.download(path, buf, max_bytes, clock.monotonic() + 60)
    return buf.getvalue()


def attachment_sha256(client: TrelloClient, card_id: str, attachment, max_bytes: int) -> str:
    """Download the attachment as kankan does and return its SHA-256 (lowercase hex)."""
    import hashlib
    return hashlib.sha256(attachment_bytes(client, card_id, attachment, max_bytes)).hexdigest()


def first_line(content: bytes) -> str:
    """The first non-empty line of a text file, stripped ("" when there is none)."""
    text = content.decode("utf-8", "replace")
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


# ---------------------------------------------------------------- Q&A dialogue (questions F§8)

_QA_MARKER = re.compile(r"`kankan:(q[ah])-([0-9a-f]+)-([0-9a-f]+)-[0-9]+`\s*$")


def qa_turns(client: TrelloClient, card_id: str, kankan_id: str) -> Dict[str, int]:
    """How many turns kankan answered (``qa``) and handed off (``qh``) on the card, a multi-part answer counting
    once."""
    seen: Dict[str, set] = {"qa": set(), "qh": set()}
    for comment in client.comments(card_id):
        found = _QA_MARKER.search(comment.text) if comment.member_id == kankan_id else None
        if found:
            seen[found.group(1)].add(found.group(2, 3))
    return {kind: len(turns) for kind, turns in seen.items()}


def qa_problems(events: List[dict], card: Card, turns: Dict[str, int]) -> List[str]:
    """The Q&A card got an answer and an answer to the follow-up, then one handoff; only the Q&A seat held its
    conversation, and work seats implemented and reviewed it."""
    problems = []
    if turns.get("qa", 0) < 2:
        problems.append(f"{card.name}: {turns.get('qa', 0)} answers instead of at least 2 (question and follow-up)")
    if turns.get("qh", 0) != 1:
        problems.append(f"{card.name}: {turns.get('qh', 0)} handoff summaries instead of 1")
    assigned = [(e.get("seat"), e.get("kind")) for e in events
                if e.get("event") == "task_assigned" and e.get("card") == card.id]
    for kind in ("qa", "impl", "review"):
        if not any(k == kind for _, k in assigned):
            problems.append(f"{card.name}: never assigned as {kind}")
    for seat, kind in assigned:
        if (kind == "qa") != str(seat).endswith("-qa"):
            problems.append(f"{card.name}: {kind} task assigned to {seat}")
    return problems


def attachment_dir_problems(project, cards: Dict[str, Card]) -> List[str]:
    """The card's attachment directory is deleted when its ref is (card verified in Done)."""
    from kankan import attachments
    return [f"{card.name}: attachment directory {attachments.card_dir(project.paths, card.short)} left behind"
            for key, card in cards.items() if attachments.card_dir(project.paths, card.short).exists()]


# ---------------------------------------------------------------- model and effort settings (boardsettings §3)

ROLES = ("implementation", "review")


def ticked(read, role: str) -> Dict[str, List[str]]:
    """Ticked item names per agent checklist of the open setting card named ``role``."""
    card = next((c for c in read.cards if c.name == role and not c.closed), None)
    if card is None:
        return {}
    return {cl.name: [i.name for i in cl.items if i.state == "complete"] for cl in card.checklists}


def _same(a: str, b: str) -> bool:
    from kankan.boardconfig import item_name
    return item_name(a) == item_name(b)


def settings_targets(read, adapters: dict) -> Dict[str, object]:
    """Per agent type, the review combination to tick: the least powerful one (the adapter's last) that differs from
    both the implementation and the review values ticked now, so the change is applied and each seat must restart
    its agent between an implementation and a review."""
    from kankan.adapters import combinations
    impl, review = ticked(read, "implementation"), ticked(read, "review")
    targets = {}
    for agent_type, adapter in adapters.items():
        taken = impl.get(agent_type, []) + review.get(agent_type, [])
        choices = [p for p in combinations(adapter) if not any(_same(p.name, t) for t in taken)]
        if not choices:
            raise ValueError(f"{agent_type}: no review combination differs from the ticked ones")
        targets[agent_type] = choices[-1]
    return targets


def settings_ready(read, targets: Dict[str, object]) -> bool:
    """The review card has exactly the target ticked in every agent checklist."""
    now = ticked(read, "review")
    return all(len(now.get(t, [])) == 1 and _same(now[t][0], pair.name) for t, pair in targets.items())


def selection_problems(events: List[dict], cards: Dict[str, Card], expected: Dict[str, Dict[str, object]],
                       adapters: dict) -> List[str]:
    """Every task on the run's cards ran with its role's model and effort, on an agent started with them."""
    from kankan.adapters import ModelEffort, start_command
    ids = {c.id: c.name for c in cards.values()}
    started: Dict[str, list] = {}
    problems = []
    for e in events:
        seat = str(e.get("seat"))
        if e.get("event") == "agent_started":
            started[seat] = e.get("command")
        if e.get("event") != "task_assigned" or e.get("card") not in ids or e.get("role") not in expected:
            continue
        agent_type = seat.split("-")[0]
        want = expected[e["role"]].get(agent_type)
        if want is None:
            continue
        name = ids[e["card"]]
        if (e.get("model"), e.get("effort")) != (want.model, want.effort):
            problems.append(f"{name}: {e['role']} by {seat} with {e.get('model')}/{e.get('effort')} instead of "
                            f"{want.model}/{want.effort}")
        elif seat in started and agent_type in adapters and \
                started[seat] != start_command(adapters[agent_type], ModelEffort(want.model, want.effort)):
            problems.append(f"{name}: {seat}'s agent was started with {started[seat]} for a "
                            f"{want.model}/{want.effort} {e['role']}")
    return problems


def claim_comment_problems(client: TrelloClient, events: List[dict], cards: Dict[str, Card], kankan_id: str) -> List[str]:
    """Each implementation and review claim comment names the model and effort the task was assigned with."""
    from kankan import messages
    ids = {c.id: c for c in cards.values()}
    wanted: Dict[str, set] = {}
    for e in events:
        if e.get("event") == "task_assigned" and e.get("card") in ids and e.get("kind") in ("impl", "review") \
                and e.get("model"):
            role = "review" if e["kind"] == "review" else "implementation"
            wanted.setdefault(e["card"], set()).add(messages.CLAIM_SELECTION.format(
                role="reviewing" if role == "review" else "implementing", type=str(e.get("seat")).split("-")[0],
                model=e["model"], effort=e["effort"]).strip())
    problems = []
    for card_id, texts in wanted.items():
        posted = [c.text for c in client.comments(card_id) if c.member_id == kankan_id]
        for text in sorted(texts):
            if not any(text in p for p in posted):
                problems.append(f"{ids[card_id].name}: no claim comment saying `{text}`")
    return problems


def applied_problems(new_comments: List[str], targets: Dict[str, object]) -> List[str]:
    """kankan confirmed the review change on the setting card ("applied: … → …", boardsettings §2)."""
    applied = [t for t in new_comments if t.startswith("applied:")]
    if not applied:
        return ["review setting card: no `applied:` comment for the new ticks"]
    tail = applied[-1].split("→", 1)[-1]
    return [f"review setting card: `{applied[-1]}` does not name {t} {p.model}/{p.effort}"
            for t, p in targets.items() if f"{t} {p.model}/{p.effort}" not in tail]


def restart_report(events: List[dict]) -> Tuple[List[str], List[str]]:
    """(problems, observations) about agent restarts: a seat whose tasks used different models or efforts must have
    restarted its agent; how each old agent ended is the release observation for the agents' exit behaviour."""
    pairs: Dict[str, set] = {}
    restarts: Dict[str, List[str]] = {}
    for e in events:
        seat = str(e.get("seat"))
        if e.get("event") == "task_assigned" and e.get("model"):
            pairs.setdefault(seat, set()).add((e["model"], e.get("effort")))
        elif e.get("event") == "agent_restart":
            restarts.setdefault(seat, []).append(str(e.get("ended")))
    problems = [f"{seat}: ran tasks with {len(used)} model/effort pairs but never restarted its agent"
                for seat, used in sorted(pairs.items()) if len(used) > 1 and not restarts.get(seat)]
    observations = [f"{seat}: old agent ended by {', '.join(ended)}" for seat, ended in sorted(restarts.items())]
    problems += [f"{seat}: old agent needed {how} (the exit command did not end it)"
                 for seat, ended in sorted(restarts.items()) for how in ended if how in ("sigterm", "sigkill")]
    return problems, observations


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
    "attach": ("hashes.txt", lambda text, ctx: bool(ctx.get("sha256")) and text == f"{ctx['sha256']}\n",
               "one line with the attached file's SHA-256"),
    "qa": ("qa.txt", lambda text, ctx: bool(ctx.get("qa_line")) and text == f"{ctx['qa_line']}\n",
           "one line: the first line of the file attached to the Q&A card"),
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


def _hold_parking(events: List[dict], card_id: str):
    """(index of the first impl assignment, index of the first parking after it), each None when absent."""
    impls = [i for i, e in enumerate(events)
             if e.get("event") == "task_assigned" and e.get("card") == card_id and e.get("kind") == "impl"]
    if not impls:
        return None, None
    parked = next((i for i in range(impls[0] + 1, len(events)) if events[i].get("event") == "transition"
                   and events[i].get("card") == card_id and events[i].get("phase") == "parked"), None)
    return impls[0], parked


def hold_problems(events: List[dict], card_id: str) -> List[str]:
    """impl → parked (the agent yielded) → impl again on the same branch.

    Either side may notice HOLD first: the launcher (suspension request queued for the agent) or the agent itself,
    when `kankan task` reports `hold: true` (spec §9.4, skill); so the order of `suspend_requested` and the parking
    is not checked (see ``hold_path``)."""
    first_i, parked = _hold_parking(events, card_id)
    if first_i is None:
        return ["hold: the card was never assigned for implementation"]
    first = events[first_i]
    if parked is None:
        return [f"hold: the card was not parked after {first.get('seat')} started it (was HOLD set while it worked?)"]
    impls = [i for i, e in enumerate(events) if i > parked and e.get("event") == "task_assigned"
             and e.get("card") == card_id and e.get("kind") == "impl"]
    if not impls:
        return ["hold: the card was not implemented again after HOLD was removed"]
    resumed = events[impls[0]]
    if resumed.get("branch") != first.get("branch"):
        return [f"hold: resumed on branch {resumed.get('branch')} instead of {first.get('branch')}"]
    return []


def hold_path(events: List[dict], card_id: str) -> Optional[str]:
    """Who noticed HOLD first on the slow card, for the run's observations."""
    first_i, parked = _hold_parking(events, card_id)
    if parked is None:
        return None
    seat = events[first_i].get("seat")
    asked = any(e.get("event") == "suspend_requested" and e.get("seat") == seat for e in events[first_i:parked])
    return ("the launcher: suspension request, then the agent yielded" if asked else
            "the agent: `kankan task` reported HOLD and it yielded before the launcher's request")


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
               user_refs: Optional[Dict[str, str]] = None, attach_sha256: Optional[str] = None,
               selection: Optional[Dict[str, Dict[str, object]]] = None, qa_line: Optional[str] = None) -> List[str]:
    """Every check of the live run; an empty list means pass.

    ``attach_sha256``: the SHA-256 of the user's upload on the attachment card (None: the card did not run).
    ``qa_line``: the first line of the user's upload on the Q&A card (None: the Q&A dialogue did not run).
    ``selection``: the model and effort expected per role and agent type (None: settings not checked)."""
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
        if key not in cards or (key == "ask" and not ask_ran) or (key == "hold" and not hold_ran) or \
                (key == "attach" and attach_sha256 is None) or (key == "qa" and qa_line is None):
            continue
        text = _show(repo, f"refs/heads/{branch}", path)
        if text is None or not check(text, {"answers": answers, "sha256": attach_sha256, "qa_line": qa_line}):
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
    if attach_sha256 is not None and "attach" in cards:
        problems += attachment_dir_problems(project, {"attach": cards["attach"]})
    if qa_line is not None and "qa" in cards:
        problems += attachment_dir_problems(project, {"qa": cards["qa"]})
        problems += qa_problems(events, cards["qa"], qa_turns(client, cards["qa"].id, cfg.kankan_member_id))
    left = sorted(p.name for p in (project.paths.kankan_dir / "qa").glob("*"))
    if left:  # deleted when the integrated card's ref is (questions T§3)
        problems.append(f"Q&A conversation directories left: {', '.join(left)}")
    if selection is not None:
        from kankan import boardsettings
        problems += selection_problems(events, cards, selection, boardsettings.adapters_for(project.paths))
        problems += claim_comment_problems(client, events, cards, cfg.kankan_member_id)
        problems += restart_report(events)[0]
    problems += [f"reconcile: {issue}" for issue in diag.reconcile(project)]
    return problems
