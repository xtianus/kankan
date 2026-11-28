"""Threads, members, the token's member, card creators and the always-live reads (board reader technical §2)."""
import pytest

from kankan.boardreader import LIVE, BoardReader, max_age
from kankan.errors import KankanError
from kankan.trello import TrelloClient
from tests.fakes.fake_trello import KEY, TOKEN


@pytest.fixture
def env(fake_trello, tmp_path, monkeypatch):
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE", raising=False)
    fake = fake_trello
    board = fake.add_board()
    backlog = fake.add_list(board["id"], "Backlog")
    card = fake.add_card(backlog["id"], "c")
    client = TrelloClient(KEY, TOKEN, base_url=fake.url, timeout=2)
    reader = BoardReader(tmp_path / "kankan", client, board["id"])
    client.on_write = reader.on_write
    return fake, client, reader, board, card


def gets(fake, needle):
    return sum(1 for method, path in fake.requests if method == "GET" and needle in path)


def test_thread_cached_and_ordered_by_date(env):
    fake, _, reader, _, card = env
    fake.shuffled_comment_ids = True
    made = [fake.user_comment(card["id"], f"m{i}") for i in range(5)]
    assert [c.id for c in reader.thread(card["id"], max_age(60))] == [m["id"] for m in made]
    reader.thread(card["id"], max_age(60))
    assert gets(fake, "/actions") == 1


@pytest.mark.parametrize("write", ["post", "delete"])
def test_thread_marked_by_comment_post_and_delete(env, write):
    fake, client, reader, _, card = env
    first = fake.user_comment(card["id"], "first")
    reader.thread(card["id"], max_age(60))
    if write == "post":
        client.add_comment(card["id"], "kankan", "op-1")
    else:
        client.delete_comment(card["id"], first["id"])
    texts = [c.text for c in reader.thread(card["id"], max_age(60))]
    assert ("first" in texts) == (write == "post") and any("kankan" in t for t in texts) == (write == "post")


def test_members_and_admins_cached(env):
    fake, _, reader, _, _ = env
    assert {m[0] for m in reader.members(max_age(60))} == {fake.owner["id"], fake.me["id"]}
    assert [a[0] for a in reader.admins(max_age(60))] == [fake.owner["id"]]
    assert gets(fake, "/memberships") == 1


def test_me_cached_per_token_and_live_replaces(env):
    fake, _, reader, _, _ = env
    assert reader.me(max_age(3600))["id"] == fake.me["id"]
    reader.me(max_age(3600))
    assert gets(fake, "/members/me") == 1
    reader.me(LIVE)
    assert gets(fake, "/members/me") == 2


def test_me_live_after_revoked_credentials(env, tmp_path):
    fake, _, reader, board, _ = env
    reader.me(max_age(3600))
    other = TrelloClient(KEY, "revoked" + "0" * 57, base_url=fake.url, timeout=2)
    revoked = BoardReader(tmp_path / "kankan", other, board["id"])
    with pytest.raises(KankanError):
        revoked.me(LIVE)
    with pytest.raises(KankanError):
        revoked.me(max_age(3600))  # another token never gets the kept member


def test_card_creator_cached_forever(env):
    fake, _, reader, _, card = env
    assert reader.card_creator(card["id"]) == reader.card_creator(card["id"])
    assert gets(fake, "/actions") == 1


def test_always_live_reads_never_stored(env, tmp_path):
    fake, _, reader, board, card = env
    for _ in range(2):
        reader.closed_cards()
        reader.board_payload()
        reader.card_place(card["id"])
        reader.board_info(board["shortLink"])
        reader.member("boardowner")
    assert gets(fake, "/cards/closed") == 2 and gets(fake, f"/cards/{card['id']}") == 2
    assert not (tmp_path / "kankan" / "board-cache" / "board.json").exists()
