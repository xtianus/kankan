"""The fake Trello's comment actions behave as observed on the live service (board reader Task 0, 2026-11-26):
ordered and filtered by date (milliseconds), action IDs of one second not ordered by creation."""
import pytest

from kankan.errors import KankanError
from kankan.trello import TrelloClient
from tests.fakes.fake_trello import KEY, TOKEN


@pytest.fixture
def setup(fake_trello):
    fake_trello.shuffled_comment_ids = True
    board = fake_trello.add_board()
    lst = fake_trello.add_list(board["id"], "Q&A")
    card = fake_trello.add_card(lst["id"], "c1")
    other = fake_trello.add_card(lst["id"], "c2")
    client = TrelloClient(KEY, TOKEN, base_url=fake_trello.url, timeout=2)
    return fake_trello, client, board, card, other


def _board_actions(client, board_id, **params):
    return client._ok("GET", f"/1/boards/{board_id}/actions",
                      {"filter": "commentCard", **{k: str(v) for k, v in params.items()}})


def _comments(fake, card_id, n):
    return [fake.user_comment(card_id, f"m{i}") for i in range(n)]


def test_same_second_ids_not_ordered(setup):
    fake, _, _, card, _ = setup
    made = _comments(fake, card["id"], 12)
    ids = [c["id"] for c in made]
    assert ids != sorted(ids)  # a test comparing IDs cannot pass by chance on this fake
    dates = [c["date"] for c in made]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)  # milliseconds, strictly increasing


def test_actions_newest_first_by_date_and_board_wide(setup):
    fake, client, board, card, other = setup
    a, b = _comments(fake, card["id"], 2)
    c = fake.user_comment(other["id"], "on the other card")
    got = _board_actions(client, board["id"])
    assert [x["id"] for x in got] == [c["id"], b["id"], a["id"]]
    assert got[0]["data"]["card"]["id"] == other["id"]


def test_actions_since_exclusive_by_action_id(setup):
    fake, client, board, card, _ = setup
    made = _comments(fake, card["id"], 5)
    got = [x["id"] for x in _board_actions(client, board["id"], since=made[1]["id"])]
    assert got == [made[4]["id"], made[3]["id"], made[2]["id"]]


def test_actions_since_date_exclusive(setup):
    fake, client, board, card, _ = setup
    made = _comments(fake, card["id"], 4)
    got = [x["id"] for x in _board_actions(client, board["id"], since=made[1]["date"])]
    assert got == [made[3]["id"], made[2]["id"]]


def test_actions_before_pages(setup):
    fake, client, board, card, _ = setup
    made = _comments(fake, card["id"], 5)
    first = _board_actions(client, board["id"], limit=2)
    assert [x["id"] for x in first] == [made[4]["id"], made[3]["id"]]
    rest = _board_actions(client, board["id"], limit=2, before=first[-1]["date"])
    assert [x["id"] for x in rest] == [made[2]["id"], made[1]["id"]]


def test_actions_limit_over_1000_is_400(setup):
    _, client, board, _, _ = setup
    with pytest.raises(KankanError) as err:
        _board_actions(client, board["id"], limit=1001)
    assert err.value.data["status"] == 400


def test_actions_since_deleted_id_falls_back_to_its_second(setup):
    fake, client, board, card, _ = setup
    made = _comments(fake, card["id"], 3)
    fake.user_delete_comment(made[1]["id"])
    got = {x["id"] for x in _board_actions(client, board["id"], since=made[1]["id"])}
    assert made[2]["id"] in got and made[1]["id"] not in got  # nothing later lost; earlier ones of that second may return


def test_card_actions_by_date(setup):
    fake, client, _, card, _ = setup
    made = _comments(fake, card["id"], 4)
    got = client._ok("GET", f"/1/cards/{card['id']}/actions", {"filter": "commentCard", "since": made[0]["date"]})
    assert [x["id"] for x in got] == [made[3]["id"], made[2]["id"], made[1]["id"]]
    assert fake.card_comments(card["id"]) == ["m0", "m1", "m2", "m3"]  # creation order, not ID order


def test_edit_changes_text_keeps_date_no_new_action(setup):
    fake, client, board, card, _ = setup
    (made,) = _comments(fake, card["id"], 1)
    fake.user_edit_comment(made["id"], "edited")
    got = _board_actions(client, board["id"])
    assert len(got) == 1 and got[0]["data"]["text"] == "edited" and got[0]["date"] == made["date"]


def test_delete_removes_action(setup):
    fake, client, board, card, _ = setup
    made = _comments(fake, card["id"], 2)
    fake.user_delete_comment(made[0]["id"])
    assert [x["id"] for x in _board_actions(client, board["id"])] == [made[1]["id"]]


def test_board_read_attachment_fields_with_pos(setup):
    fake, client, board, card, _ = setup
    fake.add_attachment(card["id"], "https://example.com/a", "a")
    fields = "id,name,url,idMember,pos,date"
    data = client._ok("GET", f"/1/boards/{board['id']}", {"cards": "open", "card_attachments": "true",
                                                         "card_attachment_fields": fields})
    (att,) = next(c for c in data["cards"] if c["id"] == card["id"])["attachments"]
    assert set(att) == {"id", "name", "url", "idMember", "pos", "date"}


def test_card_read_attachment_pos(setup):
    fake, client, _, card, _ = setup
    fake.add_attachment(card["id"], "https://example.com/a", "a")
    data = client._ok("GET", f"/1/cards/{card['id']}", {"attachments": "true", "attachment_fields": "id,pos"})
    assert set(data["attachments"][0]) == {"id", "pos"}
