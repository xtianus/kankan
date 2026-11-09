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
                 notify_id=fake.me["id"], notify_username=fake.me["username"], test_command="",
                 trello_base_url=fake.url, trello_timeout=0.5, settle_seconds=0,
                 integration_branch="main")
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    paths = Paths.discover(repo)
    save_config(paths, cfg)
    project = Project.open(repo)
    project.board = board
    return project
