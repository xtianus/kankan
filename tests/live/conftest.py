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


def _live_properties():
    """The live test .properties file, loaded, or None. Reads the real HOME (not the isolated one)."""
    creds = os.environ.get("KANKAN_TEST_CREDENTIALS")
    real_home = os.environ.get("KANKAN_REAL_HOME") or os.path.expanduser("~")
    default = Path(real_home) / ".config" / "kankan" / "test.properties"
    source = Path(creds) if creds else default
    return PropertiesFile.load(source) if source.exists() else None


def _credentials_of(props):
    if props is None:
        return None
    key, token = props.get("kankan.trello.key"), props.get("kankan.trello.token")
    return (key, token) if key and token else None


def live_credentials():
    """Return (key, token) or None; needs no test board URL."""
    return _credentials_of(_live_properties())


def live_config():
    """Return (board_url, key, token) or None. Reads the real HOME (not the isolated one), once."""
    props = _live_properties()
    credentials = _credentials_of(props)
    url = os.environ.get("KANKAN_TEST_BOARD_URL") or (props.get("kankan.test.board.url") if props else None)
    if not (url and credentials):
        return None
    return (url, *credentials)


_CONFIG = live_config()


@pytest.fixture
def run_prefix():
    return f"kt-{int(time.time())}-{secrets.token_hex(3)}"


@pytest.fixture
def live_client(isolated_home, monkeypatch):
    """A client for the live account, without selecting the shared test board: for tests that create (and
    delete) their own disposable board. Skips without credentials. The credentials go only to
    ``KANKAN_TRELLO_KEY``/``KANKAN_TRELLO_TOKEN`` (for the code under test), never into the fixture value, which
    pytest prints when a test fails."""
    credentials = live_credentials()
    if credentials is None:
        pytest.skip("no live Trello credentials (KANKAN_TEST_CREDENTIALS or ~/.config/kankan/test.properties)")
    key, token = credentials
    monkeypatch.setenv("KANKAN_TRELLO_KEY", key)
    monkeypatch.setenv("KANKAN_TRELLO_TOKEN", token)
    return TrelloClient(key, token)


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
