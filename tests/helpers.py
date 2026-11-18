"""Shared test helpers: a project bound to a FakeTrello board."""

import secrets
import socket

from kankan.config import Config, Paths, save_config, write_registry_block
from kankan.project import Project
from tests.fakes.fake_trello import KEY, TOKEN


def bind_project(repo, fake, monkeypatch=None, board=None, **cfg_overrides) -> Project:
    """Create board lists/labels on ``fake`` (unless ``board`` given) and write config.json for ``repo``."""
    if monkeypatch is not None:
        from kankan import seats
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
