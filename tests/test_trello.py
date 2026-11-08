import pytest

from kankan import clock
from kankan.errors import KankanError, Status
from kankan.trello import TrelloClient
from tests.fakes.fake_trello import KEY, TOKEN, FakeTrello


@pytest.fixture
def virtual_time(monkeypatch):
    """Sleeps advance a virtual offset instead of blocking; monotonic includes it."""
    import time
    offset = [0.0]
    monkeypatch.setattr(clock, "sleep", lambda s: offset.__setitem__(0, offset[0] + s))
    monkeypatch.setattr(clock, "monotonic", lambda: time.monotonic() + offset[0])
    return offset


@pytest.fixture
def fake():
    with FakeTrello() as server:
        yield server


@pytest.fixture
def board(fake):
    board = fake.add_board("b", "free text")
    backlog = fake.add_list(board["id"], "Backlog")
    card = fake.add_card(backlog["id"], "First")
    fake.add_attachment(card["id"], "https://trello.com/c/Zz12Zz12/9-dep")
    fake.add_label(board["id"], "HOLD")
    return board, backlog, card


def client(fake, **kwargs):
    kwargs.setdefault("timeout", 0.25)
    return TrelloClient(KEY, TOKEN, base_url=fake.url, **kwargs)


def test_snapshot_single_request(fake, board, virtual_time):
    b, backlog, card = board
    before = fake.request_count
    snap = client(fake).snapshot(b["id"])
    assert fake.request_count == before + 1
    assert snap.desc == "free text"
    assert snap.lists[backlog["id"]].name == "Backlog"
    assert snap.cards[card["id"]].attachments == ["https://trello.com/c/Zz12Zz12/9-dep"]
    assert snap.cards[card["id"]].short == card["shortLink"]
    assert [l.name for l in snap.labels.values()] == ["HOLD"]


def test_retry_on_5xx_and_429(fake, board, virtual_time):
    b = board[0]
    fake.inject("GET", "/1/boards/", "500", times=2)
    assert client(fake).snapshot(b["id"]).board_id == b["id"]
    fake.inject("GET", "/1/boards/", "429", times=10)
    with pytest.raises(KankanError) as exc:
        client(fake).snapshot(b["id"])
    assert exc.value.status == Status.NETWORK_ERROR


def test_comment_not_duplicated_after_late_success(fake, board, virtual_time):
    card = board[2]
    fake.inject("POST", "/actions/comments", "late_success")
    comment = client(fake).add_comment(card["id"], "hello", "op123")
    texts = fake.card_comments(card["id"])
    assert len(texts) == 1 and texts[0].endswith("`kankan:op123`")
    assert comment.text == texts[0]


def test_create_list_late_success_no_duplicate(fake, board, virtual_time):
    b = board[0]
    fake.inject("POST", "^/1/lists$", "late_success")
    created = client(fake).create_list(b["id"], "claude-1 · In Progress")
    found = fake.list_by_name(b["id"], "claude-1 · In Progress")
    assert len(found) == 1 and found[0]["id"] == created.id


def test_create_label_late_success_no_duplicate(fake, board, virtual_time):
    b = board[0]
    fake.inject("POST", "^/1/labels$", "late_success")
    client(fake).create_label(b["id"], "Question", "blue")
    assert len(fake.label_by_name(b["id"], "Question")) == 1


def test_snapshot_more_than_50_labels(fake, board, virtual_time):
    b = board[0]
    for i in range(59):
        fake.add_label(b["id"], f"l{i}")
    assert len(client(fake).snapshot(b["id"]).labels) == 60


def test_retry_budget_bounded(fake, board, virtual_time):
    import time
    fake.inject("GET", "/1/boards/", "timeout", times=10)
    start = time.monotonic()
    with pytest.raises(KankanError) as exc:
        client(fake).snapshot(board[0]["id"])
    assert exc.value.status == Status.NETWORK_ERROR
    assert time.monotonic() + virtual_time[0] - start < 20


def test_move_card_verifies(fake, board, virtual_time):
    b, backlog, card = board
    done = fake.add_list(b["id"], "Done")
    trello = client(fake)
    moved = trello.move_card(card["id"], done["id"])
    assert moved.list_id == done["id"]
    fake.inject("GET", "/1/cards/", "stale_read")
    with pytest.raises(KankanError) as exc:
        trello.move_card(card["id"], backlog["id"])
    assert exc.value.status == Status.TRELLO_ERROR


def test_card_missing_is_none(fake, board, virtual_time):
    assert client(fake).card("a" * 24) is None
    assert client(fake).card(board[2]["shortLink"]).id == board[2]["id"]


def test_labels_add_remove_idempotent(fake, board, virtual_time):
    b, _, card = board
    trello = client(fake)
    label = trello.create_label(b["id"], "Error", "red")
    trello.add_label(card["id"], label.id)
    trello.add_label(card["id"], label.id)
    assert trello.card(card["id"]).label_ids == [label.id]
    trello.remove_label(card["id"], label.id)
    trello.remove_label(card["id"], label.id)
    assert trello.card(card["id"]).label_ids == []
    trello.delete_label(label.id)
    trello.delete_label(label.id)


def test_comments_paginated_oldest_first(fake, board, virtual_time):
    card = board[2]
    first = fake.user_comment(card["id"], "c0")
    for i in range(1, 120):
        fake.user_comment(card["id"], f"c{i}")
    comments = client(fake).comments(card["id"], since=first["id"])
    assert [c.text for c in comments] == [f"c{i}" for i in range(1, 120)]
    assert comments[0].username == "kankanbot"


def test_lists_and_desc(fake, board, virtual_time):
    b = board[0]
    trello = client(fake)
    lst = trello.create_list(b["id"], "X")
    assert trello.list_info(lst.id).closed is False
    trello.archive_list(lst.id)
    assert trello.list_info(lst.id).closed is True
    assert trello.list_info("b" * 24) is None
    trello.set_board_desc(b["id"], "new")
    assert trello.board(b["shortLink"])["desc"] == "new"
    assert trello.me()["username"] == "kankanbot"


def test_bad_token_config_error(fake, board, virtual_time):
    with pytest.raises(KankanError) as exc:
        TrelloClient(KEY, "x" * 64, base_url=fake.url).me()
    assert exc.value.status == Status.CONFIG_ERROR


def test_credentials_never_in_errors(fake, board, virtual_time):
    fake.inject("GET", "/1/boards/", "500", times=10)
    with pytest.raises(KankanError) as exc:
        client(fake).snapshot(board[0]["id"])
    for err in (exc.value,):
        text = str(err) + repr(err.data)
        assert KEY not in text and TOKEN not in text
    with pytest.raises(KankanError) as exc:
        TrelloClient(KEY, TOKEN, base_url="http://127.0.0.1:1", timeout=0.2).me()
    assert KEY not in str(exc.value) and TOKEN not in str(exc.value)


def test_rate_bucket_waits(monkeypatch, virtual_time):
    from kankan.trello import _RateBucket
    bucket = _RateBucket(limit=3, window=10.0)
    for _ in range(4):
        bucket.take()
    assert virtual_time[0] >= 9.0
