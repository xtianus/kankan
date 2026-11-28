"""The work cycle on the board reader (board reader technical §8, plan Task 10): convergence decides from a
snapshot that may be old and reads a card live before any step; comment forwarding follows the comment log."""
import types

import pytest

from kankan import faults, forward, seats
from kankan.boardreader import max_age
from kankan.cards import CardMeta, converge, target_list_for, transition
from kankan.seatstate import load_state, update_state


@pytest.fixture
def setup(kproject, fake_trello, monkeypatch):
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE", raising=False)
    lease = seats.acquire(kproject, "claude")
    kproject.reload_config()
    card = fake_trello.add_card(kproject.cfg.backlog, "Task A")
    return kproject, fake_trello, lease, kproject.board.card(card["id"], max_age(0))


def claim_meta(project, lease, card):
    return CardMeta(phase="impl", card=card.id, short=card.short, owner=lease.seat, session=lease.session,
                    implementer=lease.seat, implementer_type=lease.type, branch=f"26kankan-{card.short}",
                    target_list=target_list_for(project.cfg, "impl", lease.seat, None))


def card_gets(fake, card_id):
    return sum(1 for m, p in fake.requests if m == "GET" and p == f"/1/cards/{card_id}")


def test_user_move_after_snapshot_not_reverted(setup, monkeypatch):
    project, fake, lease, card = setup
    monkeypatch.setenv("KANKAN_FAULT", "after_ref_tx")
    with pytest.raises(faults.SimulatedCrash):
        transition(project, lease, card, None, claim_meta(project, lease, card), None)
    monkeypatch.delenv("KANKAN_FAULT")
    snap = project.board.snapshot(max_age(600))  # shows the card still in Backlog: the move lags
    fake.user_move(card.id, project.cfg.done)  # the user moves it after the snapshot
    assert converge(project, lease, snap) == [f"human_moved:{card.id}"]
    assert fake.card_list(card.id) == project.cfg.done


def test_idle_board_no_live_card_read_per_tick(setup):
    project, fake, lease, card = setup
    transition(project, lease, card, None, claim_meta(project, lease, card), None)
    converge(project, lease)
    snap = project.board.snapshot(max_age(600))
    before = card_gets(fake, card.id)
    for _ in range(3):
        assert converge(project, lease, snap) == []
    assert card_gets(fake, card.id) == before


def test_kankan_move_never_seen_as_former_list(setup):
    project, fake, lease, card = setup
    project.board.snapshot(max_age(600))
    transition(project, lease, card, None, claim_meta(project, lease, card), None)
    seen = project.board.snapshot(max_age(600)).cards[card.id].list_id
    assert seen == project.cfg.seats[lease.seat].in_progress


def launcher_for(project, lease, card_id):
    events = []
    return types.SimpleNamespace(project=project, lease=lease, cfg=project.cfg, task={"card": card_id, "id": "t1"},
                                 log=lambda event, **f: events.append(event), events=events)


def test_forward_cursor_moves_without_comment(setup):
    project, fake, lease, card = setup
    other = fake.add_card(project.cfg.backlog, "Other")
    first = fake.user_comment(card.id, "before the ask")
    update_state(project.paths, lease.seat, lambda st: st.comment_cursor.__setitem__(card.id, first["date"]))
    fake.user_comment(other["id"], "elsewhere")
    forward.poll(launcher_for(project, lease, card.id))
    cursor = load_state(project.paths, lease.seat).comment_cursor[card.id]
    assert cursor > first["date"] and load_state(project.paths, lease.seat).pending_forward is None


def test_waiting_human_quiet_card_no_own_request(setup):
    project, fake, lease, card = setup
    first = fake.user_comment(card.id, "before the ask")
    update_state(project.paths, lease.seat, lambda st: st.comment_cursor.__setitem__(card.id, first["date"]))
    launcher = launcher_for(project, lease, card.id)
    launcher.cfg.poll_seconds = 0
    forward.poll(launcher)
    own = sum(1 for m, p in fake.requests if p == f"/1/cards/{card.id}/actions")
    for _ in range(3):
        forward.poll(launcher)
    assert sum(1 for m, p in fake.requests if p == f"/1/cards/{card.id}/actions") == own


def test_forward_answer_delivered_once_by_date(setup):
    project, fake, lease, card = setup
    fake.shuffled_comment_ids = True
    first = fake.user_comment(card.id, "before the ask")
    update_state(project.paths, lease.seat, lambda st: st.comment_cursor.__setitem__(card.id, first["date"]))
    answer = fake.user_comment(card.id, "the answer")
    launcher = launcher_for(project, lease, card.id)
    forward.poll(launcher)
    assert load_state(project.paths, lease.seat).pending_forward["text"] == "the answer"
    update_state(project.paths, lease.seat, lambda st: setattr(st, "pending_forward", None))
    forward.poll(launcher)
    assert load_state(project.paths, lease.seat).pending_forward is None
    assert load_state(project.paths, lease.seat).comment_cursor[card.id] >= answer["date"]


def test_cursor_stored_as_action_id_converted(setup):
    project, fake, lease, card = setup
    first = fake.user_comment(card.id, "asked")
    update_state(project.paths, lease.seat, lambda st: st.comment_cursor.__setitem__(card.id, first["id"]))
    answer = fake.user_comment(card.id, "the answer")
    forward.poll(launcher_for(project, lease, card.id))
    assert load_state(project.paths, lease.seat).pending_forward["text"] == "the answer"
    assert load_state(project.paths, lease.seat).comment_cursor[card.id] >= answer["date"]
