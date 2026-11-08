"""Live Trello tests (marker ``trello_live``) on a dedicated test board (spec §18.2).

Configuration, outside the repository: ``KANKAN_TEST_BOARD_URL`` + ``KANKAN_TEST_CREDENTIALS``
(a .properties file with ``kankan.trello.key``/``kankan.trello.token``), or
``~/.config/kankan/test.properties`` with ``kankan.test.board.url`` and the same keys.
"""

import os
import secrets
import time
from pathlib import Path

import pytest

from kankan.init import parse_board_url
from kankan.properties import PropertiesFile
from kankan.trello import TrelloClient

MARKER = "kankan-test-board"


def live_config():
    """Return (board_url, key, token) or None. Reads the real HOME (not the isolated one)."""
    url = os.environ.get("KANKAN_TEST_BOARD_URL")
    creds = os.environ.get("KANKAN_TEST_CREDENTIALS")
    real_home = os.environ.get("KANKAN_REAL_HOME") or os.path.expanduser("~")
    default = Path(real_home) / ".config" / "kankan" / "test.properties"
    source = Path(creds) if creds else default
    if not source.exists():
        return None
    props = PropertiesFile.load(source)
    url = url or props.get("kankan.test.board.url")
    key, token = props.get("kankan.trello.key"), props.get("kankan.trello.token")
    if not (url and key and token):
        return None
    return url, key, token


_CONFIG = live_config()


@pytest.fixture
def run_prefix():
    return f"kt-{int(time.time())}-{secrets.token_hex(3)}"


@pytest.fixture
def live_board(run_prefix):
    if _CONFIG is None:
        pytest.skip("no test board configured (KANKAN_TEST_BOARD_URL/KANKAN_TEST_CREDENTIALS or "
                    "~/.config/kankan/test.properties)")
    url, key, token = _CONFIG
    client = TrelloClient(key, token)
    board = client.board(parse_board_url(url))
    if MARKER not in (board.get("desc") or ""):
        pytest.exit(f"the test board description lacks the marker {MARKER!r}: refusing to write", returncode=3)
    created = {"cards": [], "labels": [], "lists": []}
    yield client, board, created
    for card_id in created["cards"]:
        try:
            client.delete_card(card_id)
        except Exception:
            pass
    for label_id in created["labels"]:
        try:
            client.delete_label(label_id)
        except Exception:
            pass
    for list_id in created["lists"]:
        try:
            client.archive_list(list_id)
        except Exception:
            pass
