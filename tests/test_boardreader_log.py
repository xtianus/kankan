"""The comment log and ``new_comments`` (board reader technical §7). Comment points are dates; the fake gives
comment IDs of one second out of creation order, so no test passes by comparing IDs."""
import time

import pytest

from kankan import boardreader
from kankan.boardreader import LIVE, BoardReader, max_age
from kankan.errors import KankanError
from kankan.trello import TrelloClient
from tests.fakes.fake_trello import KEY, TOKEN


@pytest.fixture
def env(fake_trello, tmp_path, monkeypatch):
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE", raising=False)
    fake = fake_trello
    fake.shuffled_comment_ids = True
    board = fake.add_board()
    lst = fake.add_list(board["id"], "Q&A")
    card = fake.add_card(lst["id"], "a")
    other = fake.add_card(lst["id"], "b")
    client = TrelloClient(KEY, TOKEN, base_url=fake.url, timeout=2)
    reader = BoardReader(tmp_path / "kankan", client, board["id"])
    client.on_write = reader.on_write
    return fake, client, reader, card, other


def board_comment_reads(fake):
    return sum(1 for m, p in fake.requests if m == "GET" and p.startswith("/1/boards/") and p.endswith("/actions"))


def card_comment_reads(fake):
    return sum(1 for m, p in fake.requests if m == "GET" and p.startswith("/1/cards/") and p.endswith("/actions"))


def ids(comments):
    return [c.id for c in comments]


def test_log_start_sets_cursor_and_horizon(env):
    fake, _, reader, card, _ = env
    old = fake.user_comment(card["id"], "old")
    comments, point = reader.new_comments(card["id"], old["date"], LIVE)
    assert comments == [] and point == old["date"]
    log = reader._load("log").payload
    assert log["cursor"] == old["date"] == log["horizon"]


def test_log_start_empty_board_origin(env):
    _, _, reader, card, _ = env
    comments, point = reader.new_comments(card["id"], "", LIVE)
    assert comments == [] and point == "" and reader._load("log").payload["horizon"] == "origin"


def test_origin_serves_empty_after_without_card_read(env):
    fake, _, reader, card, _ = env
    reader.new_comments(card["id"], "", LIVE)
    for _ in range(3):
        reader.new_comments(card["id"], "", LIVE)
    new = fake.user_comment(card["id"], "first ever")
    comments, point = reader.new_comments(card["id"], "", LIVE)
    assert ids(comments) == [new["id"]] and point == new["date"] and card_comment_reads(fake) == 0


def test_refresh_since_exclusive_and_ordered_by_date(env):
    fake, _, reader, card, other = env
    first = fake.user_comment(card["id"], "first")
    _, point = reader.new_comments(card["id"], first["date"], LIVE)
    made = [fake.user_comment(card["id"], f"m{i}") for i in range(6)]
    fake.user_comment(other["id"], "other card")
    comments, point = reader.new_comments(card["id"], point, LIVE)
    assert ids(comments) == [m["id"] for m in made] and point > made[-1]["date"]


def test_refresh_pages_full_pages(env, monkeypatch):
    fake, _, reader, card, _ = env
    monkeypatch.setattr(boardreader, "LOG_PAGE", 2)
    start = fake.user_comment(card["id"], "start")
    _, point = reader.new_comments(card["id"], start["date"], LIVE)
    made = [fake.user_comment(card["id"], f"m{i}") for i in range(5)]
    comments, _ = reader.new_comments(card["id"], point, LIVE)
    assert ids(comments) == [m["id"] for m in made]


def test_no_new_comment_advances_send_time_only(env):
    fake, _, reader, card, _ = env
    start = fake.user_comment(card["id"], "start")
    reader.new_comments(card["id"], start["date"], LIVE)
    first = reader._load("log")
    time.sleep(0.01)
    reader.new_comments(card["id"], start["date"], LIVE)
    second = reader._load("log")
    assert second.payload["cursor"] == first.payload["cursor"] and second.sent_at > first.sent_at


def test_served_from_cached_log(env):
    fake, _, reader, card, _ = env
    start = fake.user_comment(card["id"], "start")
    reader.new_comments(card["id"], start["date"], max_age(60))
    reader.new_comments(card["id"], start["date"], max_age(60))
    assert board_comment_reads(fake) == 1


def test_serving_point_never_below_after(env):
    """A caller whose previous call used the card read is ahead of the log: it is not moved back and gets no
    comment twice."""
    fake, _, reader, card, _ = env
    start = fake.user_comment(card["id"], "start")
    reader.new_comments(card["id"], start["date"], LIVE)  # the log's cursor is ``start``
    later = fake.user_comment(card["id"], "later")
    comments, point = reader.new_comments(card["id"], later["date"], max_age(60))  # served by the stale log
    assert comments == [] and point == later["date"]


def test_fallback_for_after_older_than_horizon(env):
    fake, _, reader, card, _ = env
    before = fake.user_comment(card["id"], "before the log")
    missed = fake.user_comment(card["id"], "missed")
    reader.new_comments(card["id"], missed["date"], LIVE)  # log starts at ``missed``
    comments, point = reader.new_comments(card["id"], before["date"], LIVE)
    assert ids(comments) == [missed["id"]] and point == missed["date"] and card_comment_reads(fake) == 1


def test_fallback_card_without_new_comment_gets_servable_point(env):
    fake, _, reader, card, other = env
    fake.user_comment(other["id"], "elsewhere")
    comments, point = reader.new_comments(card["id"], "", LIVE)  # empty after, horizon not origin: card read
    assert comments == [] and point == reader._load("log").payload["cursor"]
    reader.new_comments(card["id"], point, LIVE)
    assert card_comment_reads(fake) == 1  # served by the log from then on


def test_fallback_comment_between_log_and_card_read_covered(env, monkeypatch):
    fake, client, reader, card, other = env
    fake.user_comment(other["id"], "elsewhere")
    original = client.read_comments
    late = {}

    def card_read(card_id, since=None):
        late["c"] = fake.user_comment(card["id"], "arrived between the reads")
        return original(card_id, since)
    monkeypatch.setattr(client, "read_comments", card_read)
    comments, point = reader.new_comments(card["id"], "", LIVE)
    assert ids(comments) == [late["c"]["id"]] and point == late["c"]["date"]
    monkeypatch.setattr(client, "read_comments", original)
    again, _ = reader.new_comments(card["id"], point, LIVE)
    assert again == []  # never given twice


def test_pruning_moves_horizon(env, monkeypatch):
    fake, _, reader, card, _ = env
    start = fake.user_comment(card["id"], "start")
    _, point = reader.new_comments(card["id"], start["date"], LIVE)
    old = fake.user_comment(card["id"], "old")
    reader.new_comments(card["id"], point, LIVE)
    later = time.time() + 2 * 86400
    monkeypatch.setattr(boardreader.time, "time", lambda: later)
    reader.new_comments(card["id"], point, LIVE)
    log = reader._load("log").payload
    assert log["entries"] == [] and log["horizon"] == old["date"]


def test_polled_card_never_falls_behind_horizon(env, monkeypatch):
    fake, _, reader, card, other = env
    fake.user_comment(other["id"], "x")
    _, point = reader.new_comments(card["id"], "", LIVE)
    fake.user_comment(other["id"], "y")
    _, point = reader.new_comments(card["id"], point, LIVE)
    later = time.time() + 2 * 86400
    monkeypatch.setattr(boardreader.time, "time", lambda: later)
    reads = card_comment_reads(fake)
    reader.new_comments(card["id"], point, LIVE)
    assert card_comment_reads(fake) == reads


def test_card_unpolled_24h_costs_one_card_read(env, monkeypatch):
    fake, _, reader, card, other = env
    fake.user_comment(other["id"], "x")
    _, stale_point = reader.new_comments(card["id"], "", LIVE)
    fake.user_comment(other["id"], "y")
    reader.new_comments(other["id"], stale_point, LIVE)
    later = time.time() + 2 * 86400
    monkeypatch.setattr(boardreader.time, "time", lambda: later)
    reader.new_comments(other["id"], stale_point, LIVE)  # prunes "y": the horizon passes ``stale_point``
    reads = card_comment_reads(fake)
    reader.new_comments(card["id"], stale_point, LIVE)
    assert card_comment_reads(fake) == reads + 1


def test_edit_and_delete_not_reflected_in_log(env):
    fake, _, reader, card, _ = env
    start = fake.user_comment(card["id"], "start")
    _, point = reader.new_comments(card["id"], start["date"], LIVE)
    made = fake.user_comment(card["id"], "original")
    comments, _ = reader.new_comments(card["id"], point, LIVE)
    fake.user_edit_comment(made["id"], "edited")
    again, _ = reader.new_comments(card["id"], point, LIVE)
    assert [c.text for c in again] == ["original"] and ids(again) == ids(comments)


def test_comment_post_marks_log(env):
    fake, client, reader, card, _ = env
    start = fake.user_comment(card["id"], "start")
    _, point = reader.new_comments(card["id"], start["date"], max_age(60))
    client.add_comment(card["id"], "by kankan", "op-1")
    comments, _ = reader.new_comments(card["id"], point, max_age(60))
    assert len(comments) == 1 and comments[0].text.startswith("by kankan")  # the log was marked, not served stale


def test_m7_unsupported_falls_back_to_card_reads(env):
    fake, _, reader, card, _ = env
    fake.inject("GET", r"^/1/boards/[^/]+/actions$", "404", times=100)
    made = fake.user_comment(card["id"], "x")
    comments, point = reader.new_comments(card["id"], "", LIVE)
    assert ids(comments) == [made["id"]] and point == made["date"] and card_comment_reads(fake) == 1


def test_m7_transient_error_fails_and_retries(env):
    fake, client, reader, card, _ = env
    client.budget = 0.5
    fake.inject("GET", r"^/1/boards/[^/]+/actions$", "500", times=100)
    with pytest.raises(KankanError):
        reader.new_comments(card["id"], "", LIVE)
    fake.faults.clear()
    assert reader.new_comments(card["id"], "", LIVE)[0] == []


def test_m7_unsupported_probed_once_a_day(env):
    fake, _, reader, card, _ = env
    fake.inject("GET", r"^/1/boards/[^/]+/actions$", "404", times=100)
    reader.new_comments(card["id"], "", LIVE)
    reader.new_comments(card["id"], "", LIVE)
    assert board_comment_reads(fake) == 1
