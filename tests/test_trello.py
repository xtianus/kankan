import io
from urllib.parse import urlparse

import pytest

from kankan import clock
from kankan import trello as trello_mod
from kankan.errors import KankanError, Status
from kankan.trello import DownloadTooLarge, TrelloClient
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
    assert comments[0].username == "boardowner"


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


def test_create_card_with_description(fake, board):
    _, backlog, _ = board
    c = client(fake)
    card = c.create_card(backlog["id"], "t", desc="do X")
    assert card.desc == "do X"
    assert c.card(card.id).desc == "do X"


# ---------------------------------------------------------------------- attachments
CONTENT = bytes(range(256)) * 600  # 153 600 bytes, several socket reads


def _api_path(attachment) -> str:
    url = attachment["url"] if isinstance(attachment, dict) else attachment.url
    return urlparse(url).path


def _download(fake, attachment, max_bytes=10 ** 7, budget=15.0, **kwargs):
    out = io.BytesIO()
    written = client(fake, **kwargs).download(_api_path(attachment), out, max_bytes, clock.monotonic() + budget)
    return written, out.getvalue()


def test_attachments_fields_and_order(fake, board, virtual_time):
    card = board[2]
    link = fake.add_attachment(card["id"], "https://example.com/spec", name="Spec")
    upload = fake.upload(card["id"], "notes.txt", b"hello")
    link["pos"] = upload["pos"] + 1000
    found = client(fake).attachments(card["id"])
    assert [a.id for a in found][-2:] == [upload["id"], link["id"]]
    got_upload, got_link = found[-2], found[-1]
    assert got_upload.is_upload and got_upload.bytes == 5 and got_upload.file_name == "notes.txt"
    assert got_upload.member_id == fake.owner["id"] and got_upload.mime_type is None
    assert got_upload.url.endswith(f"/attachments/{upload['id']}/download/notes.txt")
    assert got_link.bytes is None and not got_link.is_upload and got_link.name == "Spec"
    assert got_link.url == "https://example.com/spec"
    assert fake.requests[-1] == ("GET", f"/1/cards/{card['id']}/attachments")


def test_download_streams_content(fake, board, virtual_time):
    att = fake.upload(board[2]["id"], "data.bin", CONTENT, mime_type="application/octet-stream")
    assert _download(fake, att) == (len(CONTENT), CONTENT)
    assert fake.downloads[att["id"]] == 1


def test_download_too_large_by_content_length(fake, board, virtual_time):
    att = fake.upload(board[2]["id"], "data.bin", CONTENT)
    with pytest.raises(DownloadTooLarge):
        _download(fake, att, max_bytes=len(CONTENT) - 1)


def test_download_too_large_while_streaming(fake, board, virtual_time):
    att = fake.upload(board[2]["id"], "data.bin", CONTENT)
    fake.download_omit_length(att["id"])
    with pytest.raises(DownloadTooLarge):
        _download(fake, att, max_bytes=100_000)
    assert _download(fake, att, max_bytes=len(CONTENT)) == (len(CONTENT), CONTENT)


def test_download_retries_500_then_succeeds(fake, board, virtual_time):
    att = fake.upload(board[2]["id"], "data.bin", CONTENT)
    fake.inject("GET", "/download/", "500", times=2)
    assert _download(fake, att) == (len(CONTENT), CONTENT)


def test_download_truncated_is_retried_then_error(fake, board, virtual_time):
    att = fake.upload(board[2]["id"], "data.bin", CONTENT)
    fake.inject("GET", "/download/", "truncate", times=1)
    assert _download(fake, att) == (len(CONTENT), CONTENT)
    fake.inject("GET", "/download/", "truncate", times=1000)
    with pytest.raises(KankanError) as exc:
        _download(fake, att)
    assert exc.value.status == Status.NETWORK_ERROR
    assert fake.downloads[att["id"]] == 1


def test_download_404_is_error(fake, board, virtual_time):
    att = fake.upload(board[2]["id"], "data.bin", CONTENT)
    fake.remove_attachment(board[2]["id"], att["id"])
    with pytest.raises(KankanError) as exc:
        _download(fake, att)
    assert exc.value.message == "HTTP 404"
    assert KEY not in repr(exc.value.data) and TOKEN not in str(exc.value)


def test_download_same_origin_redirect_keeps_credential(fake, board, virtual_time):
    first = fake.upload(board[2]["id"], "a.bin", b"first")
    second = fake.upload(board[2]["id"], "b.bin", CONTENT)
    fake.redirect_download(first["id"], _api_path(second))  # relative Location, same origin
    assert _download(fake, first) == (len(CONTENT), CONTENT)
    downloads = [entry for entry in fake.auth_log if "/download/" in entry[1]]
    assert downloads == [("GET", _api_path(first), True), ("GET", _api_path(second), True)]


def test_download_cross_origin_redirect_drops_credential(fake, board, external_host, virtual_time):
    att = fake.upload(board[2]["id"], "a.bin", b"first")
    fake.redirect_download(att["id"], external_host.url + "/files/a.bin")
    assert _download(fake, att) == (len(external_host.content), external_host.content)
    path, headers = external_host.requests[0]
    assert path == "/files/a.bin"
    assert "authorization" not in {name.lower() for name in headers}


def test_download_too_many_redirects(fake, board, virtual_time):
    att = fake.upload(board[2]["id"], "a.bin", b"first")
    fake.redirect_download(att["id"], _api_path(att))
    with pytest.raises(KankanError) as exc:
        _download(fake, att)
    assert exc.value.message == "too many redirects"


def test_download_refused_redirect_scheme(fake, board, virtual_time):
    att = fake.upload(board[2]["id"], "a.bin", b"first")
    fake.redirect_download(att["id"], "file:///etc/passwd")
    with pytest.raises(KankanError):
        _download(fake, att)


def test_download_path_must_be_absolute(fake, virtual_time):
    with pytest.raises(KankanError):
        client(fake).download("@example.com/x", io.BytesIO(), 100, clock.monotonic() + 5)


def test_redirect_auth_policy():
    auth = trello_mod._redirect_auth
    assert auth("https://api.trello.com", "http://x.example/f") is None
    assert auth("https://api.trello.com", "https://x.example/f") is False
    assert auth("https://api.trello.com", "https://api.trello.com/1/x") is True
    assert auth("https://api.trello.com", "https://API.trello.com:443/1/x") is True
    assert auth("https://api.trello.com", "https://api.trello.com:8443/1/x") is False
    assert auth("http://127.0.0.1:1", "http://localhost:2/f") is False
    assert auth("https://api.trello.com", "file:///etc/passwd") is None
    assert auth("https://api.trello.com", "ftp://api.trello.com/x") is None


def test_json_call_does_not_follow_redirect(fake, external_host, virtual_time):
    fake.redirect_route("GET", "/1/members/me$", external_host.url)
    with pytest.raises(KankanError):
        client(fake).me()
    assert external_host.requests == []


def test_attach_link_and_upload(fake, board, virtual_time):
    card = board[2]
    trello = client(fake)
    link = trello.attach_link(card["id"], "https://example.com/doc", "Doc")
    assert link.url == "https://example.com/doc" and link.name == "Doc" and not link.is_upload
    assert link.member_id == fake.me["id"]
    upload = trello.upload_attachment(card["id"], 'my "notes".txt', b"line\r\n--x\r\n", "text/plain")
    assert upload.is_upload and upload.bytes == 11 and upload.mime_type == "text/plain"
    assert upload.file_name == 'my "notes".txt'
    assert _download(fake, upload) == (11, b"line\r\n--x\r\n")
    assert [a.id for a in trello.attachments(card["id"])][-2:] == [link.id, upload.id]


@pytest.mark.parametrize("location", ["http://[bad", "/1/café/x", "/1/a b"])
def test_download_malformed_redirect_is_error(fake, board, virtual_time, location):
    att = fake.upload(board[2]["id"], "a.bin", b"first")
    fake.redirect_download(att["id"], location)
    with pytest.raises(KankanError) as exc:
        _download(fake, att)
    assert exc.value.status == Status.TRELLO_ERROR
