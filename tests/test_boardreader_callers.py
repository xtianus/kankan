"""Functional acceptance tests of the board reader through its callers (board reader functional §8)."""
import pytest

from kankan import boardsettings, destroy
from kankan.boardreader import LIVE, max_age


@pytest.fixture
def cached(kproject, fake_trello, monkeypatch):
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE", raising=False)
    fake_trello.shuffled_comment_ids = True
    return kproject, fake_trello


def card_comment_reads(fake):
    return sum(1 for m, p in fake.requests if m == "GET" and p.startswith("/1/cards/") and p.endswith("/actions"))


def board_comment_reads(fake):
    return sum(1 for m, p in fake.requests if m == "GET" and p.startswith("/1/boards/") and p.endswith("/actions"))


def test_qa_quiet_cards_no_own_request(cached):
    """F test 11: ten quiet cards cost no request of their own per poll once the log covers them."""
    project, fake = cached
    cards = [fake.add_card(project.cfg.backlog, f"q{i}")["id"] for i in range(10)]
    fake.user_comment(cards[0], "old")
    points = {c: project.board.new_comments(c, "", LIVE)[1] for c in cards}
    before = card_comment_reads(fake)
    for _ in range(3):
        for c in cards:
            comments, points[c] = project.board.new_comments(c, points[c], max_age(0))
            assert comments == []
    assert card_comment_reads(fake) == before


def test_one_board_comment_read_serves_two_cards(cached):
    """F test 2: an answer to an ``ask`` and a Q&A follow-up on another card are found by one board-wide read."""
    project, fake = cached
    asked, qa_card = (fake.add_card(project.cfg.backlog, n)["id"] for n in ("asked", "qa"))
    fake.user_comment(asked, "the question")
    point_a = project.board.new_comments(asked, "", LIVE)[1]
    point_q = project.board.new_comments(qa_card, "", LIVE)[1]
    answer = fake.user_comment(asked, "the answer")
    follow = fake.user_comment(qa_card, "a follow-up")
    reads = board_comment_reads(fake)
    got_a, _ = project.board.new_comments(asked, point_a, LIVE)  # the asking launcher's poll refreshes the log
    assert [c.id for c in got_a] == [answer["id"]]
    got_q, _ = project.board.new_comments(qa_card, point_q, max_age(60))  # the Q&A poll uses that refresh
    assert [c.id for c in got_q] == [follow["id"]] and board_comment_reads(fake) == reads + 1


def test_settings_cached_read_uses_cached_response_date(cached):
    project, fake = cached
    settings = fake.add_list(project.cfg.board_id, "Settings")
    first = project.board.settings_cards(settings["id"], LIVE)
    again = project.board.settings_cards(settings["id"], max_age(60))
    assert again.response_date == first.response_date is not None


def test_destroy_removes_cache_keeps_locks(cached):
    """F test 8: destroy's state sweep covers the cache and the write marks, and keeps the lock files."""
    project, fake = cached
    card = fake.add_card(project.cfg.backlog, "c")
    project.board.snapshot(max_age(60))
    project.trello.move_card(card["id"], project.cfg.done)
    project.board.snapshot(max_age(60))
    listed = {p.relative_to(project.paths.kankan_dir).as_posix() for p in destroy._state(project.paths.kankan_dir)}
    assert "board-cache/board.json" in listed and "board-marks.json" in listed
    assert not any(name.endswith(".lock") and name.startswith("board-") for name in listed)


def test_input_members_live_for_commands(cached):
    """A member removed from the board stops counting as an input member at once for commands (Codex review)."""
    from kankan import notify
    project, fake = cached
    assert fake.owner["id"] in notify.input_members(project, max_age(600))
    fake.leave_board(project.cfg.board_id, fake.owner["id"])
    assert fake.owner["id"] in notify.input_members(project, max_age(600))  # a poller may use its interval
    assert fake.owner["id"] not in notify.input_members(project)  # commands read live


def test_attachment_listing_of_a_deleted_card_raises(cached):
    from kankan import attachments
    from kankan.errors import KankanError
    from kankan.trello import decode_card
    project, fake = cached
    card = fake.add_card(project.cfg.backlog, "c")
    read = project.board.card(card["id"], LIVE)
    fake.user_delete(card["id"])
    with pytest.raises(KankanError):
        attachments.for_task(project, read, still_held=lambda: attachments.Hold.HELD)


def test_quiet_poll_never_rewinds_a_newer_cursor(cached, monkeypatch):
    import types
    from kankan import forward, seats
    from kankan.seatstate import load_state, update_state
    project, fake = cached
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c")
    first = fake.user_comment(card["id"], "q")
    update_state(project.paths, lease.seat, lambda st: st.comment_cursor.__setitem__(card["id"], first["date"]))
    fake.user_comment(fake.add_card(project.cfg.backlog, "other")["id"], "elsewhere")  # the point moves on
    newer = "2999-01-01T00:00:00.000Z"
    real = project.board.new_comments

    def concurrent_ask(card_id, after, fresh):
        result = real(card_id, after, fresh)
        update_state(project.paths, lease.seat, lambda st: st.comment_cursor.__setitem__(card["id"], newer))
        return result
    monkeypatch.setattr(project.board, "new_comments", concurrent_ask)
    launcher = types.SimpleNamespace(project=project, lease=lease, cfg=project.cfg,
                                     task={"card": card["id"], "id": "t"}, log=lambda *a, **k: None)
    forward.poll(launcher)
    assert load_state(project.paths, lease.seat).comment_cursor[card["id"]] == newer
