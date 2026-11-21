import pytest

from kankan import messages
from kankan.config import REGISTRY_END, REGISTRY_START, registry_state, remove_registry_block, write_registry_block
from kankan.errors import KankanError, Status
from kankan.trello import TrelloClient
from tests.fakes.fake_trello import KEY, TOKEN


@pytest.fixture
def board(fake_trello):
    board = fake_trello.add_board("b")
    return board, fake_trello.add_list(board["id"], "Backlog")


def client(fake):
    return TrelloClient(KEY, TOKEN, base_url=fake.url, timeout=0.5)


def test_closed_cards_one_request_when_fewer_than_limit(fake_trello, board):
    board, lst = board
    for i in range(5):
        fake_trello.add_card(lst["id"], f"a{i}")["closed"] = True
    for i in range(3):
        fake_trello.add_card(lst["id"], f"o{i}")
    fake_trello.requests.clear()
    cards = client(fake_trello).closed_cards(board["id"], limit=6)
    assert len({c.id for c in cards}) == len(cards) == 5
    assert all(c.closed and c.list_id == lst["id"] for c in cards)
    assert sum(1 for m, p in fake_trello.requests if p.endswith("/cards/closed")) == 1


@pytest.mark.parametrize("count, kwargs", [(3, {"limit": 3}), (1000, {})])
def test_closed_cards_full_page_refuses(fake_trello, board, count, kwargs):
    board, lst = board
    for i in range(count):
        fake_trello.add_card(lst["id"], f"a{i}")["closed"] = True
    fake_trello.requests.clear()
    with pytest.raises(KankanError) as err:
        client(fake_trello).closed_cards(board["id"], **kwargs)
    assert err.value.status == Status.NEEDS_ACTION
    assert err.value.message == messages.TOO_MANY_ARCHIVED_CARDS.format(limit=kwargs.get("limit", 1000))
    assert "1000" in messages.TOO_MANY_ARCHIVED_CARDS.format(limit=1000)
    assert sum(1 for m, p in fake_trello.requests if p.endswith("/cards/closed")) == 1


def test_closed_cards_error_raises(fake_trello, board):
    board, _ = board
    fake_trello.inject("GET", "/cards/closed", "500", times=10)
    with pytest.raises(KankanError) as err:
        client(fake_trello).closed_cards(board["id"])
    assert err.value.status == Status.NETWORK_ERROR
    fake_trello.faults.clear()
    fake_trello.inject("GET", "/cards/closed", "404")
    with pytest.raises(KankanError) as err:
        client(fake_trello).closed_cards(board["id"])
    assert err.value.status == Status.TRELLO_ERROR


@pytest.mark.parametrize("payload", [
    {},
    [{"id": "a", "closed": True, "idLabels": []}],
    [{"id": "a", "idList": None, "idLabels": []}],
    [{"id": "a", "idList": "l"}],
    [{"id": "", "idList": "l", "idLabels": []}],
    [{"idList": "l", "idLabels": []}],
])
def test_closed_cards_malformed_page(fake_trello, board, payload):
    board, _ = board
    fake_trello.route = lambda method, path, p: (200, payload)
    with pytest.raises(KankanError) as err:
        client(fake_trello).closed_cards(board["id"])
    assert err.value.status == Status.TRELLO_ERROR


def test_registry_state():
    block = write_registry_block("", {"registry-id": "abc"})
    assert registry_state("") == ("absent", None)
    assert registry_state("text") == ("absent", None)
    assert registry_state(block) == ("valid", "abc")
    assert registry_state("x\n" + REGISTRY_START + "\nregistry-id: abc\n") == ("malformed", None)
    assert registry_state(block + "\n" + block) == ("malformed", None)
    assert registry_state(write_registry_block("", {"other": "1"})) == ("malformed", None)


@pytest.mark.parametrize("desc", ["", "intro", "intro\n\nouter"])
def test_remove_registry_block_roundtrip(desc):
    assert remove_registry_block(write_registry_block(desc, {"registry-id": "x"})) == desc


def test_remove_registry_block_trailing_newline_is_ambiguous():
    # write_registry_block maps "intro" and "intro\n" to the same text; the removal restores the former
    assert write_registry_block("intro\n", {"registry-id": "x"}) == write_registry_block("intro", {"registry-id": "x"})
    assert remove_registry_block(write_registry_block("intro\n", {"registry-id": "x"})) == "intro"


def test_remove_registry_block_middle_and_absent():
    block = REGISTRY_START + "\nregistry-id: x\n" + REGISTRY_END
    assert remove_registry_block("before\n" + block + "\nafter") == "before\n\nafter"
    assert remove_registry_block("before\n\n" + block + "\n\nafter\n") == "before\n\n\n\nafter\n"
    assert remove_registry_block("plain\n") == "plain\n"


def test_delete_card_404_is_done(fake_trello, board):
    _, lst = board
    card = fake_trello.add_card(lst["id"], "c")
    c = client(fake_trello)
    c.delete_card(card["id"])
    c.delete_card(card["id"])
    c.delete_card("5f0000000000000000000999")


def test_delete_card_other_errors_raise(fake_trello, board):
    _, lst = board
    card = fake_trello.add_card(lst["id"], "c")
    fake_trello.inject("DELETE", "/cards/", "403")
    with pytest.raises(KankanError) as err:
        client(fake_trello).delete_card(card["id"])
    assert err.value.status == Status.CONFIG_ERROR


def test_delete_card_error_omits_response_text(fake_trello, board):
    fake_trello.route = lambda method, path, p: (409, "board SecretBoardName https://trello.com/b/zzz/SecretBoardName")
    with pytest.raises(KankanError) as err:
        client(fake_trello).delete_card("5f0000000000000000000001")
    assert err.value.status == Status.TRELLO_ERROR
    assert "SecretBoardName" not in err.value.message and "trello.com/b" not in err.value.message
    assert "409" in err.value.message
