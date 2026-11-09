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
