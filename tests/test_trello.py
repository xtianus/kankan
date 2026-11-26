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


def test_settings_transport_round_trip(fake_trello):
    from kankan.trello import TrelloClient
    from tests.fakes.fake_trello import KEY, TOKEN
    fake = fake_trello
    board = fake.add_board()
    lst = fake.add_list(board['id'], 'settings')
    card = fake.add_card(lst['id'], 'implementation')
    client = TrelloClient(KEY, TOKEN, base_url=fake.url, timeout=.2)
    fake.inject('POST', r'/1/cards/.*/checklists$', 'late_success')
    check = client.create_checklist(card['id'], 'claude')
    fake.inject('POST', r'/1/checklists/.*/checkItems$', 'late_success')
    item = client.create_check_item(check.id, 'sonnet · high', True, 1024)
    snap = client.snapshot(board['id'])
    assert snap.response_date is not None
    assert snap.cards[card['id']].checklists[0].items[0].state == 'complete'
    read = client.settings_cards(lst['id'])
    assert read.list_available and read.response_date is not None
    assert read.cards[0].checklists[0].items[0].name == 'sonnet · high'
    assert len(fake.state['checklists']) == 1
    client.move_check_item(card['id'], item.id, 512)
    assert client.settings_cards(lst['id']).cards[0].checklists[0].items[0].pos == 512
    client.delete_check_item(check.id, item.id)
    client.delete_check_item(check.id, item.id)
    comment = client.add_comment(card['id'], 'feedback', 'transport-test')
    client.delete_comment(card['id'], comment.id)
    client.delete_comment(card['id'], comment.id)
    assert client.comments(card['id']) == []
    client.archive_list(lst['id'])
    assert not client.settings_cards(lst['id']).list_available
    client.unarchive_list(lst['id'])
    assert client.settings_cards(lst['id']).list_available
    assert not client.settings_cards('missing').list_available


def test_settings_missing_or_bad_server_date(fake_trello):
    from kankan.trello import TrelloClient
    from tests.fakes.fake_trello import KEY, TOKEN
    board = fake_trello.add_board()
    lst = fake_trello.add_list(board['id'], 'settings')
    client = TrelloClient(KEY, TOKEN, base_url=fake_trello.url)
    fake_trello.response_date = 'bad date'
    assert client.settings_cards(lst['id']).response_date is None
    fake_trello.response_date = None
    assert client.snapshot(board['id']).response_date is None


# ---------------------------------------------------------------------- Q&A capabilities


ATTACHMENT_FIELD_NAMES = "id,name,fileName,url,bytes,mimeType,idMember,isUpload,date"


def test_card_read_returns_attachment_fields(fake, board, virtual_time):
    card = board[2]
    upload = fake.upload(card["id"], "notes.txt", b"hello", "text/plain")
    trello = client(fake)
    read = trello.card(card["id"])
    assert read.attachments == ["https://trello.com/c/Zz12Zz12/9-dep", upload["url"]]
    link, got = read.attachment_items
    assert got.id == upload["id"] and got.name == "notes.txt" and got.file_name == "notes.txt"
    assert got.url == upload["url"] and got.bytes == 5 and got.mime_type == "text/plain"
    assert got.member_id == fake.owner["id"] and got.is_upload is True and got.date == upload["date"] != ""
    assert not link.is_upload and link.url == "https://trello.com/c/Zz12Zz12/9-dep"
    assert trello.snapshot(board[0]["id"]).cards[card["id"]].attachment_items == read.attachment_items  # board reader T§3


def test_attachments_include_date(fake, board, virtual_time):
    card = board[2]
    upload = fake.upload(card["id"], "notes.txt", b"hello")
    found = client(fake).attachments(card["id"])
    assert all(a.date for a in found)
    assert found[-1].date == upload["date"]
    assert trello_mod.ATTACHMENT_FIELDS.endswith(",date")
    assert trello_mod._attachment({"id": "x"}).date == ""


def test_move_card_bottom(fake, board, virtual_time):
    b, backlog, card = board
    done = fake.add_list(b["id"], "Done")
    first = fake.add_card(done["id"], "a")
    second = fake.add_card(done["id"], "b")
    before = fake.request_count
    moved = client(fake).move_card(card["id"], done["id"], "bottom")
    assert moved.list_id == done["id"]
    assert fake.request_count == before + 2  # the move and the verification read
    assert fake.requests[-1][0] == "GET" and fake.requests[-1][1] == f"/1/cards/{card['id']}"
    cards = sorted((c for c in fake.state["cards"].values() if c["idList"] == done["id"]), key=lambda c: c["pos"])
    assert [c["id"] for c in cards] == [first["id"], second["id"], card["id"]]


def test_move_card_default_top_unchanged(fake, board, virtual_time):
    b, backlog, card = board
    done = fake.add_list(b["id"], "Done")
    first = fake.add_card(done["id"], "a")
    client(fake).move_card(card["id"], done["id"])
    cards = sorted((c for c in fake.state["cards"].values() if c["idList"] == done["id"]), key=lambda c: c["pos"])
    assert [c["id"] for c in cards] == [card["id"], first["id"]]


def test_comment_text_matches_posted_text(fake, board, virtual_time):
    card = board[2]
    text = "1/2 hello"
    posted = client(fake).add_comment(card["id"], text, "op-1")
    assert posted.text == trello_mod.comment_text(text, "op-1") == f"{text}\n\n{trello_mod.marker('op-1')}"
    assert fake.card_comments(card["id"])[-1] == trello_mod.comment_text(text, "op-1")


def test_fake_rejects_long_comment(fake, board, virtual_time):
    card = board[2]
    trello = client(fake)
    assert trello_mod.COMMENT_MAX == 16384
    op = "op-long"
    fits = "x" * (trello_mod.COMMENT_MAX - len(trello_mod.comment_text("", op)))
    trello.add_comment(card["id"], fits, op)
    assert len(fake.card_comments(card["id"])[-1]) == 16384
    with pytest.raises(KankanError) as exc:
        trello.add_comment(card["id"], fits + "x", "op-long2")
    assert exc.value.status == Status.TRELLO_ERROR


# ---------------------------------------------------------------- board reader support (board reader Task 2)

def test_read_board_returns_raw_payload_and_send_time(fake, board):
    import time
    b, _, card = board
    c = client(fake)
    before = time.time()
    read = c.read_board(b["id"])
    after = time.time()
    assert before <= read.sent_at <= after and read.response_date is not None
    assert any(raw["id"] == card["id"] for raw in read.payload["cards"])


def test_send_time_is_taken_before_the_request(fake, board):
    import time
    b, _, _ = board
    fake.response_delay = 0.3
    fake.inject("GET", r"/1/boards/", "delay")
    started = time.time()
    read = client(fake, timeout=2).read_board(b["id"])
    assert read.sent_at - started < 0.2 and time.time() - read.sent_at >= 0.3


def test_decoders_same_result_for_raw_and_live(fake, board):
    b, _, card = board
    c = client(fake)
    read = c.read_board(b["id"])
    assert trello_mod.decode_snapshot(read.payload, read.response_date, b["id"]) == c.snapshot(b["id"])
    raw_card = c.read_card(card["id"])
    assert trello_mod.decode_card(raw_card.payload) == c.card(card["id"])


def test_board_nested_card_decodes_like_card_read(fake, board):
    b, _, card = board
    fake.add_attachment(card["id"], "https://example.com/second", "second")
    checklist = fake.add_checklist(card["id"], "Settings") if hasattr(fake, "add_checklist") else None
    c = client(fake)
    nested = c.snapshot(b["id"]).cards[card["id"]]
    single = c.card(card["id"])
    assert nested.attachment_items == single.attachment_items and len(single.attachment_items) == 2
    assert [a.url for a in nested.attachment_items] == nested.attachments
    if checklist is not None:
        assert [x.id for x in nested.checklists] == [x.id for x in single.checklists]


def test_attachment_items_ordered_by_pos(fake, board):
    b, _, card = board
    second = fake.add_attachment(card["id"], "https://example.com/second", "second")
    second["pos"] = 1.0  # moved first in the card
    c = client(fake)
    assert c.card(card["id"]).attachment_items[0].id == second["id"]
    assert c.snapshot(b["id"]).cards[card["id"]].attachment_items[0].id == second["id"]


def test_read_card_none_when_deleted(fake, board):
    _, _, card = board
    fake.user_delete(card["id"])
    assert client(fake).read_card(card["id"]).payload is None


def test_board_comment_actions_pages_and_card_ids(fake, board):
    b, _, card = board
    fake.shuffled_comment_ids = True
    made = [fake.user_comment(card["id"], f"m{i}") for i in range(3)]
    c = client(fake)
    page = c.read_board_comments(b["id"], since=made[0]["date"], limit=1)
    assert [raw["id"] for raw in page.payload] == [made[2]["id"]]
    comments = [trello_mod.decode_comment(raw) for raw in c.read_board_comments(b["id"]).payload]
    assert [x.id for x in comments] == [m["id"] for m in reversed(made)]
    assert {x.card_id for x in comments} == {card["id"]}


def test_newest_comment_action_empty_board(fake, board):
    b, _, _ = board
    assert client(fake).read_board_comments(b["id"], limit=1).payload == []


def test_comments_ordered_and_paged_by_date(fake, board):
    _, _, card = board
    fake.shuffled_comment_ids = True
    made = [fake.user_comment(card["id"], f"m{i}") for i in range(120)]  # three pages of 50
    got = client(fake).comments(card["id"])
    assert [x.id for x in got] == [m["id"] for m in made]
    since = client(fake).comments(card["id"], since=made[59]["date"])
    assert [x.id for x in since] == [m["id"] for m in made[60:]]
    raw = client(fake).read_comments(card["id"]).payload
    assert [r["id"] for r in raw] == [m["id"] for m in made]


WRITE_EFFECTS = [
    ("move_card", lambda c, ids: c.move_card(ids["card"], ids["list"]), ("card", "card")),
    ("add_label", lambda c, ids: c.add_label(ids["card"], ids["label"]), ("card", "card")),
    ("remove_label", lambda c, ids: c.remove_label(ids["card"], ids["label"]), ("card", "card")),
    ("set_card_desc", lambda c, ids: c.set_card_desc(ids["card"], "d"), ("card", "card")),
    ("attach_link", lambda c, ids: c.attach_link(ids["card"], "https://example.com/x", "x"), ("card", "card")),
    ("add_comment", lambda c, ids: c.add_comment(ids["card"], "t", "op-1"), ("comment", "card")),
    ("create_card", lambda c, ids: c.create_card(ids["list"], "n"), ("card_set", "list")),
    ("delete_card", lambda c, ids: c.delete_card(ids["card"]), ("card_set", "card")),
    ("create_list", lambda c, ids: c.create_list(ids["board"], "L"), ("board", None)),
    ("archive_list", lambda c, ids: c.archive_list(ids["list"]), ("board", None)),
    ("set_board_desc", lambda c, ids: c.set_board_desc(ids["board"], "x"), ("board", None)),
    ("create_label", lambda c, ids: c.create_label(ids["board"], "L", "red"), ("board", None)),
]


@pytest.mark.parametrize("name,write,expected", WRITE_EFFECTS, ids=[w[0] for w in WRITE_EFFECTS])
def test_on_write_called_for_each_write_kind(fake, board, name, write, expected):
    b, backlog, card = board
    label = next(iter(l for l in fake.state["labels"].values() if l["idBoard"] == b["id"]))
    if name == "remove_label":
        fake.state["cards"][card["id"]]["idLabels"].append(label["id"])
    c = client(fake)
    effects = []
    c.on_write = effects.append
    write(c, {"card": card["id"], "list": backlog["id"], "board": b["id"], "label": label["id"]})
    kind, target = expected
    assert effects and effects[-1].kind == kind
    if target == "card":
        assert effects[-1].card_id == card["id"]


def test_on_write_check_items_mark_their_card(fake, board):
    _, _, card = board
    c = client(fake)
    checklist = c.create_checklist(card["id"], "Settings")
    effects = []
    c.on_write = effects.append
    item = c.create_check_item(checklist.id, "x", False, 1, card_id=card["id"])
    c.delete_check_item(checklist.id, item.id, card_id=card["id"])
    assert [(e.kind, e.card_id) for e in effects] == [("card", card["id"])] * 2


def test_on_write_check_item_without_card_marks_board(fake, board):
    _, _, card = board
    c = client(fake)
    checklist = c.create_checklist(card["id"], "Settings")
    effects = []
    c.on_write = effects.append
    c.create_check_item(checklist.id, "x", False, 1)
    assert effects[-1].kind == "board"


def test_on_write_called_after_timeout(fake, board):
    _, backlog, card = board
    c = client(fake, timeout=0.25)
    effects = []
    c.on_write = effects.append
    fake.inject("PUT", r"/1/cards/", "timeout", times=10)
    with pytest.raises(KankanError):
        c.move_card(card["id"], backlog["id"])
    assert effects and effects[-1].card_id == card["id"]
