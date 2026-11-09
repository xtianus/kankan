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
