"""Shared test helpers: a project bound to a FakeTrello board."""

import secrets
import socket
import subprocess
import sys

from kankan import seats
from kankan.config import Config, Paths, save_config, write_registry_block
from kankan.project import Project
from tests.conftest import ROOT
from tests.fakes.fake_trello import KEY, TOKEN


def bind_project(repo, fake, monkeypatch=None, board=None, **cfg_overrides) -> Project:
    """Create board lists/labels on ``fake`` (unless ``board`` given) and write config.json for ``repo``."""
    if monkeypatch is not None:
        monkeypatch.setattr(seats, "pane_sid", lambda: None)
        monkeypatch.setenv("KANKAN_TRELLO_KEY", KEY)
        monkeypatch.setenv("KANKAN_TRELLO_TOKEN", TOKEN)
    registry = secrets.token_hex(8)
    if board is None:
        board = fake.add_board("test")
        lists = {name: fake.add_list(board["id"], name)["id"] for name in ("Backlog", "Done", "Blocked")}
        labels = {name: fake.add_label(board["id"], name)["id"] for name in ("HOLD", "Question", "Error")}
        board["kankan"] = {"lists": lists, "labels": labels}
    lists, labels = board["kankan"]["lists"], board["kankan"]["labels"]
    fake.state["boards"][board["id"]]["desc"] = write_registry_block(
        "test board", {"registry-id": registry, "host": socket.gethostname(), "path": str(repo)})
    cfg = Config(board_id=board["id"], board_short=board["shortLink"], registry_id=registry,
                 backlog=lists["Backlog"], done=lists["Done"], blocked=lists["Blocked"],
                 label_hold=labels["HOLD"], label_question=labels["Question"], label_error=labels["Error"],
                 kankan_member_id=fake.me["id"], kankan_username=fake.me["username"], test_command="",
                 trello_base_url=fake.url, trello_timeout=0.5, settle_seconds=0,
                 integration_branch="main")
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    paths = Paths.discover(repo)
    save_config(paths, cfg)
    project = Project.open(repo)
    project.board = board
    return project


def add_qa_list(project, fake, name="Q&A") -> str:
    """Create the ``Q&A`` list on the fake board and register it as ``cfg.qa_list``."""
    from kankan.config import load_config
    ident = fake.add_list(project.cfg.board_id, name)["id"]
    cfg = load_config(project.paths)
    cfg.qa_list = ident
    save_config(project.paths, cfg)
    project.reload_config()
    return ident


def add_questions_seed(remove=False) -> None:
    """Give (or, with ``remove``, take away) the ``questions`` seed of both test adapters in the user's overrides."""
    import json
    from pathlib import Path
    target = Path.home() / ".config/kankan/agents.json"
    data = json.loads(target.read_text())
    for agent_type, model in (("claude", "sonnet"), ("codex", "gpt-6.1-sol")):
        seeds = data[agent_type]["seed"]
        if remove:
            seeds.pop("questions", None)
        else:
            seeds["questions"] = {"model": model, "effort": "high"}
    target.write_text(json.dumps(data))


def put_card_ref(project, card_id: str, short: str, **meta):
    """Create a card ref with the given blob fields (``phase`` required) and return its ``RefValue``."""
    from kankan.refstore import card_ref
    oid = project.refs.write_blob({"schema": 1, "card": card_id, "short": short, "created": "2026-11-17T00:00:00Z",
                                   **meta})
    project.refs.create(card_ref(card_id), oid)
    return project.refs.read(card_ref(card_id))


# A seat holder that behaves like a launcher towards stop requests: it clears the request once at start (as
# Launcher.start does), then exits when it sees one; with "ignore" it never exits by itself.
HOLDER = """
import os, sys, time
sys.path.insert(0, {root!r})
os.environ["KANKAN_SUPERVISOR_PID"] = str(os.getpid())
from kankan import seats
from kankan.project import Project
from kankan.seatstate import load_state, update_state
project = Project.open({repo!r})
lease = seats.acquire(project, {type!r}, seat={seat!r})
update_state(project.paths, {seat!r}, lambda s: setattr(s, "stop_requested", False))
print("held", flush=True)
while True:
    time.sleep(0.05)
    if {mode!r} == "obey" and load_state(project.paths, {seat!r}).stop_requested:
        seats.release(project, lease)
        sys.exit(0)
"""


class NoTmux:
    def sessions_with_option(self, option):
        return {}

    def kill_session(self, name):
        pass


def hold(project, seat, mode="obey"):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER.format(root=str(ROOT), repo=str(project.paths.repo),
                                                                type=seats.seat_type(seat), seat=seat, mode=mode)],
                            start_new_session=True, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def remove_seat(project, seat):
    """`kankan seat remove <seat> --yes` (seat F§3.2): an immediate removal, confirmed."""
    from kankan import seatremove
    return seatremove.remove(project, seat, graceful=False, confirm=lambda text: True, say=lambda text: None)
