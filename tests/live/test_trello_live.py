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
