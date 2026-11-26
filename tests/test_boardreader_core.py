"""Board reader core (board reader technical §2–§6): freshness, cache files, write marks, newest wins, in-flight
fetches. Driven through the reader's generic unit read with a counting fetch; the typed reads are tested elsewhere."""
import json
import shutil
import time

import pytest

from kankan import boardreader
from kankan.boardreader import LIVE, BoardReader, max_age
from kankan.errors import KankanError, Status
from kankan.trello import Read, WriteEffect

BOARD = "b" * 24
CARD = "c" * 24


class Fetch:
    """A fetch returning a new payload each time, with an optional action run during the request."""

    def __init__(self):
        self.count = 0
        self.during = None

    def __call__(self):
        sent_at = time.time()
        self.count += 1
        if self.during is not None:
            action, self.during = self.during, None
            action()
        return Read({"n": self.count}, sent_at, None)


@pytest.fixture
def reader(tmp_path, monkeypatch):
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE", raising=False)
    return BoardReader(tmp_path / "kankan", None, BOARD)


def test_live_needs_request_after_call(reader):
    fetch = Fetch()
    reader._read("card:" + CARD, max_age(60), fetch)
    got = reader._read("card:" + CARD, LIVE, fetch)
    assert fetch.count == 2 and got.payload == {"n": 2}


def test_max_age_zero_is_live(reader):
    fetch = Fetch()
    reader._read("card:" + CARD, LIVE, fetch)
    reader._read("card:" + CARD, max_age(0), fetch)
    assert fetch.count == 2


def test_max_age_served_from_cache(reader):
    fetch = Fetch()
    first = reader._read("card:" + CARD, max_age(60), fetch)
    again = reader._read("card:" + CARD, max_age(60), fetch)
    assert fetch.count == 1 and again == first


def test_max_age_older_than_allowed_fetches(reader, monkeypatch):
    fetch = Fetch()
    reader._read("card:" + CARD, max_age(60), fetch)
    later = time.time() + 61
    monkeypatch.setattr(boardreader.time, "time", lambda: later)
    reader._read("card:" + CARD, max_age(60), fetch)
    assert fetch.count == 2


def test_cache_shared_between_reader_instances(reader, tmp_path):
    fetch = Fetch()
    reader._read("card:" + CARD, max_age(60), fetch)
    other = BoardReader(tmp_path / "kankan", None, BOARD)
    assert other._read("card:" + CARD, max_age(60), fetch).payload == {"n": 1} and fetch.count == 1


MARKS = [
    (WriteEffect("card", CARD), ["card:" + CARD]),
    (WriteEffect("comment", CARD), ["thread:" + CARD, "log"]),
    (WriteEffect("comment_edit", CARD), ["thread:" + CARD]),
    (WriteEffect("card_set", CARD), ["board", "card:" + CARD]),
    (WriteEffect("card_set", None, "l" * 24), ["board"]),
    (WriteEffect("board"), ["board"]),
]


@pytest.mark.parametrize("effect,keys", MARKS, ids=[f"{e.kind}-{len(k)}" for e, k in MARKS])
def test_mark_makes_older_unit_stale(reader, effect, keys):
    every = ["board", "card:" + CARD, "thread:" + CARD, "log", "members"]
    fetches = {key: Fetch() for key in every}
    for key in every:
        reader._read(key, max_age(60), fetches[key])
    reader.on_write(effect)
    for key in every:
        reader._read(key, max_age(60), fetches[key])
        assert fetches[key].count == (2 if key in keys else 1), key


def test_inflight_fetch_across_write_refetched_for_later_call(reader):
    fetch = Fetch()
    fetch.during = lambda: reader.on_write(WriteEffect("card", CARD))
    got = reader._read("card:" + CARD, max_age(60), fetch)
    assert fetch.count == 2 and got.payload == {"n": 2}


def test_inflight_result_not_stored(reader):
    fetch = Fetch()
    fetch.during = lambda: reader.on_write(WriteEffect("card", CARD))
    reader._read("card:" + CARD, max_age(60), fetch)
    assert reader._load("card:" + CARD).payload == {"n": 2}


def test_newest_wins_slow_older_fetch_not_stored(reader):
    newer = Read({"n": "newer"}, time.time(), None)
    older = Read({"n": "older"}, newer.sent_at - 5, None)
    reader._store("card:" + CARD, newer)
    reader._store("card:" + CARD, older)
    assert reader._load("card:" + CARD).payload == {"n": "newer"}


def test_format_or_board_mismatch_ignored(reader, tmp_path):
    fetch = Fetch()
    reader._read("card:" + CARD, max_age(60), fetch)
    path = tmp_path / "kankan" / "board-cache" / "cards" / f"{CARD}.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps(dict(data, board_id="x" * 24)))
    reader._read("card:" + CARD, max_age(60), fetch)
    path.write_text(json.dumps(dict(json.loads(path.read_text()), format=2)))
    reader._read("card:" + CARD, max_age(60), fetch)
    assert fetch.count == 3


def test_corrupt_file_ignored(reader, tmp_path):
    fetch = Fetch()
    reader._read("card:" + CARD, max_age(60), fetch)
    (tmp_path / "kankan" / "board-cache" / "cards" / f"{CARD}.json").write_text("{not json")
    assert reader._read("card:" + CARD, max_age(60), fetch).payload == {"n": 2}


def test_error_stores_nothing(reader):
    def failing():
        raise KankanError(Status.NETWORK_ERROR, "down")
    with pytest.raises(KankanError):
        reader._read("card:" + CARD, max_age(60), failing)
    assert reader._load("card:" + CARD) is None


def test_unbound_reader_never_stores(tmp_path):
    reader = BoardReader.unbound(None)
    fetch = Fetch()
    reader._read("card:" + CARD, max_age(60), fetch)
    reader._read("card:" + CARD, max_age(60), fetch)
    reader.on_write(WriteEffect("board"))
    assert fetch.count == 2 and [p.name for p in tmp_path.iterdir()] == ["home"]  # isolated_home only


def test_cache_files_private(reader, tmp_path):
    reader._read("card:" + CARD, max_age(60), Fetch())
    cache = tmp_path / "kankan" / "board-cache"
    assert cache.stat().st_mode & 0o777 == 0o700
    assert (cache / "cards" / f"{CARD}.json").stat().st_mode & 0o777 == 0o600


def test_cache_deleted_marks_and_locks_kept(reader, tmp_path):
    fetch = Fetch()
    reader._read("card:" + CARD, max_age(60), fetch)

    def write_then_delete_cache():
        reader.on_write(WriteEffect("card", CARD))
        shutil.rmtree(tmp_path / "kankan" / "board-cache")
    fetch.during = write_then_delete_cache
    reader._read("card:" + CARD, LIVE, fetch)
    assert (tmp_path / "kankan" / "board-marks.json").exists()
    assert reader._load("card:" + CARD).payload == {"n": 3}  # the result fetched across the write was not stored


def test_project_board_installed_and_hook_set(kproject):
    from kankan.project import Project
    project = Project.open(kproject.paths.repo)
    assert isinstance(project.board, BoardReader) and project.board.board_id == project.cfg.board_id
    assert project.trello.on_write == project.board.on_write
    assert project.board.state_dir == project.paths.kankan_dir


def test_read_never_returns_result_rejected_by_mark(reader):
    """Every fetch overtaken by a write: the read fails rather than return pre-write data (Codex review)."""
    fetch = Fetch()

    def always_written():
        result = Fetch.__call__(fetch)
        reader.on_write(WriteEffect("card", CARD))
        return result
    with pytest.raises(KankanError):
        reader._read("card:" + CARD, LIVE, always_written)


def test_test_switch_makes_polling_reads_live(reader, monkeypatch, fake_trello):
    from kankan.trello import TrelloClient
    from tests.fakes.fake_trello import KEY, TOKEN
    board = fake_trello.add_board()
    live = BoardReader(reader.state_dir, TrelloClient(KEY, TOKEN, base_url=fake_trello.url, timeout=2), board["id"])
    monkeypatch.setenv("KANKAN_TEST_NO_CACHE", "1")
    live.snapshot(max_age(60))
    live.snapshot(max_age(60))
    assert sum(1 for m, p in fake_trello.requests if p == f"/1/boards/{board['id']}") == 2
