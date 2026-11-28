import pytest

pytestmark = pytest.mark.trello_live


def test_live_snapshot_move_label_comment(live_board, run_prefix):
    client, board, created = live_board
    me = client.me()
    assert me["id"]
    first = client.create_list(board["id"], f"{run_prefix} A")
    second = client.create_list(board["id"], f"{run_prefix} B")
    created["lists"] += [first.id, second.id]
    card = client.create_card(first.id, f"{run_prefix} card")
    created["cards"].append(card.id)
    label = client.create_label(board["id"], f"{run_prefix} label", "green")
    created["labels"].append(label.id)
    snap = client.snapshot(board["id"])
    assert card.id in snap.cards and first.id in snap.lists and label.id in snap.labels
    assert client.list_info(first.id).name == f"{run_prefix} A"
    moved = client.move_card(card.id, second.id)
    assert moved.list_id == second.id
    client.add_label(card.id, label.id)
    client.add_label(card.id, label.id)
    assert label.id in client.card(card.short).label_ids
    client.remove_label(card.id, label.id)
    comment = client.add_comment(card.id, "live test", f"{run_prefix}-op")
    again = client.add_comment(card.id, "live test", f"{run_prefix}-op")
    assert comment.text.endswith(f"`kankan:{run_prefix}-op`")
    comments = client.comments(card.id)
    assert any(c.id == comment.id for c in comments) and again.id
    desc = client.board(board["id"])["desc"]
    client.set_board_desc(board["id"], desc)
    assert client.member(me["username"])["id"] == me["id"]
    assert client.card_creator(card.id)[0] == me["id"]  # the token's owner created it
    assert me["id"] in {member_id for member_id, _, _ in client.board_members(board["id"])}
    client.delete_label(label.id)
    created["labels"].remove(label.id)
    client.archive_list(second.id)
    assert client.list_info(second.id).closed


def test_live_attachments(live_board, run_prefix):
    """Upload, link, listing and download on real Trello; pins the download URL shape `download_path` accepts."""
    import io

    from kankan import attachments, clock
    from kankan.trello import DownloadTooLarge
    client, board, created = live_board
    lst = client.create_list(board["id"], f"{run_prefix} attachments")
    created["lists"].append(lst.id)
    card = client.create_card(lst.id, f"{run_prefix} attachments")
    created["cards"].append(card.id)
    content = b"hello attachments\n"
    client.upload_attachment(card.id, "kankan probe.txt", content, "text/plain")
    client.attach_link(card.id, "https://example.com/spec.html", "spec link")
    upload, link = client.attachments(card.id)
    assert upload.is_upload and upload.bytes == len(content) == 18 and upload.member_id == client.me()["id"]
    assert not link.is_upload and link.url == "https://example.com/spec.html"
    path = attachments.download_path(client.base_url, upload.url, card.id, upload.id)
    assert path is not None, upload.url
    buf = io.BytesIO()
    assert client.download(path, buf, 1024, clock.monotonic() + 30) == len(content)
    assert buf.getvalue() == content
    with pytest.raises(DownloadTooLarge):
        client.download(path, io.BytesIO(), 4, clock.monotonic() + 30)


def test_live_settings_checklist_and_comment_deletion(live_board, run_prefix):
    client, board, created = live_board
    lst = client.create_list(board['id'], f'{run_prefix} settings')
    created['lists'].append(lst.id)
    card = client.create_card(lst.id, f'{run_prefix} implementation')
    created['cards'].append(card.id)
    checklist = client.create_checklist(card.id, 'codex')
    item = client.create_check_item(checklist.id, 'model · high', True, 1024)
    read = client.settings_cards(lst.id)
    assert read.list_available and read.response_date is not None
    assert read.cards[0].checklists[0].items[0].state == 'complete'
    snap = client.snapshot(board['id'])
    assert any(c.id == checklist.id for c in snap.cards[card.id].checklists)
    client.move_check_item(card.id, item.id, 512)
    comment = client.add_comment(card.id, 'settings feedback', f'{run_prefix}-settings')
    client.delete_comment(card.id, comment.id)
    assert all(c.id != comment.id for c in client.comments(card.id))
    client.delete_check_item(checklist.id, item.id)
    client.archive_list(lst.id)
    assert not client.settings_cards(lst.id).list_available
    client.unarchive_list(lst.id)
    assert client.settings_cards(lst.id).list_available


def test_live_qa_capabilities(live_board, run_prefix):
    """The comment length limit, `pos=bottom` moves and the C1 attachment fields (with `date`) on real Trello."""
    from kankan import trello as trello_mod
    from kankan.errors import KankanError
    client, board, created = live_board
    lst = client.create_list(board["id"], f"{run_prefix} qa")
    other = client.create_list(board["id"], f"{run_prefix} qa target")
    created["lists"] += [lst.id, other.id]
    card = client.create_card(lst.id, f"{run_prefix} qa card")
    created["cards"].append(card.id)
    resident = client.create_card(other.id, f"{run_prefix} resident")
    created["cards"].append(resident.id)
    # M1: 16 384 characters are accepted, one more is rejected
    op = f"{run_prefix}-long"
    fits = "x" * (trello_mod.COMMENT_MAX - len(trello_mod.comment_text("", op)))
    posted = client.add_comment(card.id, fits, op)
    assert len(posted.text) == trello_mod.COMMENT_MAX
    with pytest.raises(KankanError):
        client.add_comment(card.id, fits + "x", f"{op}2")
    # C3: pos=bottom puts the card after the cards already in the list
    moved = client.move_card(card.id, other.id, "bottom")
    assert moved.list_id == other.id and moved.pos > client.card(resident.id).pos
    # C1 and F2: the attachment fields, with the creation date
    client.upload_attachment(card.id, "kankan_qa.txt", b"qa\n", "text/plain")
    client.attach_link(card.id, "https://example.com/qa", "qa link")
    read = client.card(card.short)
    assert len(read.attachment_items) == 2 and len(read.attachments) == 2
    for item in read.attachment_items + client.attachments(card.id):
        assert item.id and item.url and item.member_id and item.date.startswith("20")
    upload = next(a for a in read.attachment_items if a.is_upload)
    assert upload.file_name == "kankan_qa.txt" and upload.bytes == 3


def test_card_desc_update_round_trip(live_board, run_prefix):
    """C9 (card instructions T§8): a description written with set_card_desc reads back unchanged."""
    client, board, created = live_board
    lst = client.create_list(board["id"], f"{run_prefix} desc")
    created["lists"].append(lst.id)
    card = client.create_card(lst.id, f"{run_prefix} desc card", "old")
    created["cards"].append(card.id)
    text = "spec\n\n```\n## agent: claude review\nInstructions for claude review go here.\n```\n"
    assert client.set_card_desc(card.id, text).desc == text
    assert client.card(card.id).desc == text


def _actions(client, board_id, **params):
    """M7: the board's comment actions, newest first (board reader technical §9)."""
    query = {"filter": "commentCard", "limit": "1000", **{k: str(v) for k, v in params.items()}}
    return client._ok("GET", f"/1/boards/{board_id}/actions", query)


def test_live_board_comment_actions(live_board, run_prefix):
    """Board reader Task 0: M7 pages and `since` exclusive; `since`/`before` naming a deleted comment; the same
    action IDs, ordered by creation, in the board-wide and the per-card read; edits and deletions not reported."""
    client, board, created = live_board
    lst = client.create_list(board["id"], f"{run_prefix} m7")
    created["lists"].append(lst.id)
    card = client.create_card(lst.id, f"{run_prefix} m7")
    created["cards"].append(card.id)
    ids = [client.add_comment(card.id, f"m7 {i}", f"{run_prefix}-m7-{i}").id for i in range(4)]
    # Action IDs of one second are NOT ordered by creation (only their first 8 hex digits, the second, are):
    # M7 orders by and filters `since`/`before` on each action's date (milliseconds), which follows creation.
    listed = _actions(client, board["id"], since=ids[0])
    dates = {a["id"]: a["date"] for a in listed}
    assert [a["id"] for a in listed][:3] == [ids[3], ids[2], ids[1]]
    assert dates[ids[1]] < dates[ids[2]] < dates[ids[3]]
    newest = _actions(client, board["id"], limit=1)
    assert [a["id"] for a in newest] == [ids[-1]]
    after_first = [a["id"] for a in _actions(client, board["id"], since=ids[0])]
    assert after_first[:3] == [ids[3], ids[2], ids[1]] and ids[0] not in after_first  # newest first, exclusive
    page = [a["id"] for a in _actions(client, board["id"], since=ids[0], limit=2)]
    assert page == [ids[3], ids[2]]
    rest = [a["id"] for a in _actions(client, board["id"], since=ids[0], before=page[-1], limit=2)]
    assert rest == [ids[1]]
    per_card = [c.id for c in client.comments(card.id)]
    assert sorted(per_card) == sorted(ids)
    assert all(a["data"]["card"]["id"] == card.id for a in _actions(client, board["id"], since=ids[0]))
    client.delete_comment(card.id, ids[1])
    after_delete = [a["id"] for a in _actions(client, board["id"], since=ids[1])]
    # A deleted cursor falls back to the second in its ID: nothing later is lost, but earlier comments of that
    # second come back too, so a cursor keeps its date and the reader filters on it.
    assert ids[1] not in after_delete and {ids[2], ids[3]} <= set(after_delete)
    before_deleted = [a["id"] for a in _actions(client, board["id"], before=ids[1], since=ids[0])]
    assert ids[2] not in before_deleted and ids[3] not in before_deleted
    client._ok("PUT", f"/1/actions/{ids[2]}/text", body={"value": "m7 edited"})
    again = {a["id"]: a for a in _actions(client, board["id"], since=ids[0])}
    assert set(again) == {ids[2], ids[3]} and again[ids[2]]["data"]["text"] == "m7 edited"  # no new action
    assert again[ids[2]]["date"] == dates[ids[2]]  # an edit keeps the date
    by_date = [a["id"] for a in _actions(client, board["id"], since=dates[ids[2]])]
    assert by_date == [ids[3]]  # since=<ISO date> is accepted and exclusive
    per_card = [a["id"] for a in client._ok("GET", f"/1/cards/{card.id}/actions",
                                            {"filter": "commentCard", "since": dates[ids[2]]})]
    assert per_card == [ids[3]]


def test_live_attachment_pos_in_board_and_card_reads(live_board, run_prefix):
    """Board reader Task 0: `pos`, `date` and `idMember` of attachments in the nested board read and the card read."""
    client, board, created = live_board
    lst = client.create_list(board["id"], f"{run_prefix} pos")
    created["lists"].append(lst.id)
    card = client.create_card(lst.id, f"{run_prefix} pos")
    created["cards"].append(card.id)
    client.attach_link(card.id, "https://example.com/a", "a")
    client.attach_link(card.id, "https://example.com/b", "b")
    fields = "id,name,fileName,url,bytes,mimeType,idMember,isUpload,pos,date"
    data = client._ok("GET", f"/1/boards/{board['id']}", {"cards": "open", "card_fields": "name",
                                                         "card_attachments": "true", "card_attachment_fields": fields,
                                                         "checklists": "all"})
    nested = next(c for c in data["cards"] if c["id"] == card.id)["attachments"]
    assert len(nested) == 2 and all("pos" in a and "date" in a and "idMember" in a for a in nested)
    single = client._ok("GET", f"/1/cards/{card.id}", {"attachments": "true", "attachment_fields": fields})
    assert [a["id"] for a in sorted(single["attachments"], key=lambda a: a["pos"])] == \
        [a["id"] for a in sorted(nested, key=lambda a: a["pos"])]
    assert "checklists" in data


def test_live_board_reader_comments_and_attachment_order(live_board, run_prefix, tmp_path, monkeypatch):
    """Board reader Task 12: the comment log and the attachment order through `BoardReader` on real Trello."""
    from kankan.boardreader import LIVE, BoardReader, max_age
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE", raising=False)
    client, board, created = live_board
    reader = BoardReader(tmp_path / "kankan", client, board["id"])
    client.on_write = reader.on_write
    lst = client.create_list(board["id"], f"{run_prefix} reader")
    created["lists"].append(lst.id)
    card = client.create_card(lst.id, f"{run_prefix} reader")
    created["cards"].append(card.id)
    first = client.add_comment(card.id, "r0", f"{run_prefix}-r0")
    _, point = reader.new_comments(card.id, first.date, LIVE)
    made = [client.add_comment(card.id, f"r{i}", f"{run_prefix}-r{i}") for i in range(1, 4)]
    comments, point = reader.new_comments(card.id, point, max_age(60))  # kankan's comments marked the log
    assert [c.id for c in comments] == [m.id for m in made] and point == made[-1].date
    assert reader.new_comments(card.id, point, LIVE)[0] == []
    a = client.attach_link(card.id, "https://example.com/a", "a")
    b = client.attach_link(card.id, "https://example.com/b", "b")
    order = [x.id for x in reader.card(card.id, LIVE).attachment_items]
    assert order == [a.id, b.id] == [x.id for x in reader.snapshot(LIVE).cards[card.id].attachment_items]
