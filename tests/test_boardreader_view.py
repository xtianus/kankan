"""The board view and the card, list and settings reads of the board reader (board reader technical §2–§4)."""
import time

import pytest

from kankan import boardreader
from kankan.boardreader import LIVE, BoardReader, max_age
from kankan.trello import TrelloClient
from tests.fakes.fake_trello import KEY, TOKEN


@pytest.fixture
def env(fake_trello, tmp_path, monkeypatch):
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE", raising=False)
    fake = fake_trello
    board = fake.add_board()
    backlog = fake.add_list(board["id"], "Backlog")
    done = fake.add_list(board["id"], "Done")
    cards = [fake.add_card(backlog["id"], f"c{i}") for i in range(6)]
    client = TrelloClient(KEY, TOKEN, base_url=fake.url, timeout=2)
    reader = BoardReader(tmp_path / "kankan", client, board["id"])
    client.on_write = reader.on_write
    return fake, client, reader, board, backlog, done, cards


def board_reads(fake):
    return sum(1 for method, path in fake.requests if method == "GET" and path.startswith("/1/boards/")
               and path.count("/") == 3)


def card_reads(fake):
    return sum(1 for method, path in fake.requests if method == "GET" and path.startswith("/1/cards/")
               and path.count("/") == 3)


def test_view_served_from_cache(env):
    fake, _, reader, *_ = env
    reader.snapshot(max_age(60))
    reader.snapshot(max_age(60))
    assert board_reads(fake) == 1


def test_view_merges_fresher_card_unit(env):
    fake, _, reader, _, _, done, cards = env
    reader.snapshot(max_age(60))
    fake.user_move(cards[0]["id"], done["id"])
    reader.card(cards[0]["id"], LIVE)
    assert reader.snapshot(max_age(60)).cards[cards[0]["id"]].list_id == done["id"] and board_reads(fake) == 1


def test_view_card_mark_forces_patch(env):
    fake, client, reader, _, _, done, cards = env
    reader.snapshot(max_age(60))
    client.move_card(cards[0]["id"], done["id"])
    before = card_reads(fake)
    snap = reader.snapshot(max_age(60))
    assert snap.cards[cards[0]["id"]].list_id == done["id"]
    assert board_reads(fake) == 1 and card_reads(fake) == before + 1


def test_view_patches_at_most_three_then_board_read(env):
    fake, client, reader, _, _, done, cards = env
    reader.snapshot(max_age(60))
    for card in cards[:4]:
        client.move_card(card["id"], done["id"])
    snap = reader.snapshot(max_age(60))
    assert all(snap.cards[c["id"]].list_id == done["id"] for c in cards[:4]) and board_reads(fake) == 2


def test_view_response_date_oldest_part(env):
    fake, _, reader, _, _, _, cards = env
    first = reader.snapshot(max_age(60))
    time.sleep(1.1)
    reader.card(cards[0]["id"], LIVE)
    assert reader.snapshot(max_age(60)).response_date == first.response_date


@pytest.mark.parametrize("how", ["archive", "delete"])
def test_view_fresher_closed_or_deleted_card_removed(env, how):
    fake, _, reader, _, _, _, cards = env
    reader.snapshot(max_age(60))
    (fake.user_archive if how == "archive" else fake.user_delete)(cards[0]["id"])
    reader.card(cards[0]["id"], LIVE)
    assert cards[0]["id"] not in reader.snapshot(max_age(60)).cards


def test_view_card_moved_to_other_board_removed(env):
    fake, _, reader, _, _, _, cards = env
    reader.snapshot(max_age(60))
    other = fake.add_board("other")
    other_list = fake.add_list(other["id"], "X")
    fake.state["cards"][cards[0]["id"]].update(idBoard=other["id"], idList=other_list["id"])
    reader.card(cards[0]["id"], LIVE)
    assert cards[0]["id"] not in reader.snapshot(max_age(60)).cards


def test_view_never_adds_unlisted_card(env):
    fake, _, reader, _, backlog, _, _ = env
    reader.snapshot(max_age(60))
    new = fake.add_card(backlog["id"], "new")
    reader.card(new["id"], LIVE)
    assert new["id"] not in reader.snapshot(max_age(60)).cards


def test_card_none_when_deleted_closed_when_archived(env):
    fake, _, reader, _, _, _, cards = env
    fake.user_archive(cards[0]["id"])
    fake.user_delete(cards[1]["id"])
    assert reader.card(cards[0]["id"], LIVE).closed is True
    assert reader.card(cards[1]["id"], LIVE) is None


def test_card_answered_from_fresh_board_unit(env):
    fake, _, reader, _, _, _, cards = env
    reader.snapshot(max_age(60))
    before = card_reads(fake)
    assert reader.card(cards[0]["id"], max_age(60)).name == "c0" and card_reads(fake) == before


def test_card_by_short_link_reads_live(env):
    fake, _, reader, _, _, _, cards = env
    assert reader.card(cards[0]["shortLink"], max_age(60)).id == cards[0]["id"]


def test_list_info_from_view_and_deleted_list_none(env):
    fake, _, reader, board, backlog, done, _ = env
    assert reader.list_info(backlog["id"], max_age(60)).name == "Backlog"
    assert reader.list_info("f" * 24, max_age(60)) is None
    assert board_reads(fake) == 1


def test_settings_cards_from_view_with_checklists(env):
    fake, client, reader, board, _, _, _ = env
    settings = fake.add_list(board["id"], "Settings")
    card = fake.add_card(settings["id"], "Implementation Agents")
    checklist = client.create_checklist(card["id"], "claude")
    read = reader.settings_cards(settings["id"], max_age(60))
    assert read.list_available and [c.id for c in read.cards] == [card["id"]]
    assert [x.id for x in read.cards[0].checklists] == [checklist.id] and read.response_date is not None
    fake.state["lists"][settings["id"]]["closed"] = True
    assert reader.settings_cards(settings["id"], LIVE).list_available is False


def test_attachments_ordered_by_pos_from_board_and_card_read(env):
    fake, _, reader, _, _, _, cards = env
    first = fake.add_attachment(cards[0]["id"], "https://example.com/a", "a")
    second = fake.add_attachment(cards[0]["id"], "https://example.com/b", "b")
    second["pos"] = first["pos"] / 2
    view = reader.snapshot(LIVE).cards[cards[0]["id"]]
    assert [a.id for a in view.attachment_items] == [second["id"], first["id"]]
    assert [a.id for a in reader.card(cards[0]["id"], LIVE).attachment_items] == [second["id"], first["id"]]


def test_board_read_prunes_old_absent_card_files(env, tmp_path, monkeypatch):
    fake, _, reader, _, _, _, cards = env
    reader.card(cards[0]["id"], LIVE)
    reader.thread(cards[0]["id"], LIVE) if hasattr(reader, "thread") else None
    fake.user_delete(cards[0]["id"])
    path = tmp_path / "kankan" / "board-cache" / "cards" / f"{cards[0]['id']}.json"
    assert path.exists()
    later = time.time() + 2 * 86400
    real = time.time
    monkeypatch.setattr(boardreader.time, "time", lambda: later)
    reader.snapshot(LIVE)
    assert not path.exists()
    monkeypatch.setattr(boardreader.time, "time", real)


def test_card_cached_open_contradicted_by_newer_board_read(env):
    """An open card unit is not served once a newer board read no longer lists the card (Codex review)."""
    fake, _, reader, _, _, _, cards = env
    reader.card(cards[0]["id"], LIVE)
    fake.user_archive(cards[0]["id"])
    time.sleep(0.01)
    reader.snapshot(LIVE)
    assert reader.card(cards[0]["id"], max_age(60)).closed is True


def test_view_stale_negative_card_unit_is_patched_not_removed(env):
    """A card unit showing the card archived, invalidated by a later card mark, is re-read, not trusted."""
    fake, client, reader, _, _, _, cards = env
    reader.snapshot(LIVE)
    fake.user_archive(cards[0]["id"])
    reader.card(cards[0]["id"], LIVE)  # closed card unit, newer than the board unit
    fake.state["cards"][cards[0]["id"]]["closed"] = False  # restored by the user
    client.set_card_desc(cards[0]["id"], "changed")  # kankan's write marks the card
    assert reader.snapshot(max_age(60)).cards[cards[0]["id"]].desc == "changed"


def test_view_response_date_unknown_when_a_part_has_none(env, monkeypatch):
    fake, client, reader, _, _, _, cards = env
    fake.response_date = None
    reader.snapshot(LIVE)
    fake.response_date = "auto"
    reader.card(cards[0]["id"], LIVE)
    assert reader.snapshot(max_age(60)).response_date is None
