"""The routing choice (qapool T§5 step 4–5, F§4.2–§4.3) as a pure function."""

from kankan.qapool import Decision, SeatView, choose

T = 1_000_000.0


def seat(name, **kw):
    kw.setdefault("state", "qa_waiting")
    return SeatView(name=name, **kw)


def run(needing, seats, **kw):
    return choose(needing, seats, **kw)


def test_reserved_cold_holder_offered_nothing_else():
    seats = [seat("claude-qa", holds="A", reserved=True, warm=False),
             seat("claude-qa2", holds="B", warm=True, expiry=T + 100)]
    decision = run([("C", "2026-01-01")], seats)
    assert decision.offers == [] and decision.openings == ["C"]
    assert "claude-qa" not in [s for s, _ in decision.offers]


def test_wait_key_order():
    seats = [seat("claude-qa", warm=False)]
    decision = run([("late", "2026-01-02T00:00:00.000Z"), ("early", "2026-01-01T00:00:00.000Z")], seats)
    assert decision.offers == [("claude-qa", "early")] and decision.openings == ["late"]


def test_available_lowest_number_before_new():
    seats = [seat("claude-qa", warm=True, expiry=T + 50, holds="X"), seat("claude-qa3", warm=False),
             seat("claude-qa2", warm=False, holds="Y")]
    decision = run([("A", "1")], seats)
    assert decision.offers == [("claude-qa2", "A")] and decision.openings == []


def test_new_member_when_all_warm():
    seats = [seat("claude-qa", warm=True, expiry=T + 50, holds="X"), seat("claude-qa2", warm=True, expiry=T + 9,
                                                                         holds="Y")]
    decision = run([("A", "1"), ("B", "2")], seats)
    assert decision.offers == [] and decision.openings == ["A", "B"]


def test_cold_router_takes_new_card():
    decision = run([("A", "1")], [seat("claude-qa", warm=False, holds="X")])
    assert decision.offers == [("claude-qa", "A")] and decision.openings == []


def test_ended_card_member_is_available_even_warm():
    decision = run([("A", "1")], [seat("claude-qa", warm=True, expiry=T, holds="X"),
                                   seat("claude-qa2", warm=True, expiry=T, card_ended=True, state="idle")])
    assert decision.offers == [("claude-qa2", "A")]


def test_warm_fallback_after_open_failure():
    seats = [seat("claude-qa", warm=True, expiry=T + 500, holds="X"),
             seat("claude-qa2", warm=True, expiry=T + 100, holds="Y"),
             seat("claude-qa3", warm=True, expiry=T + 10, holds="Z", state="working")]
    decision = run([("A", "1")], seats, opening_failed=True)
    assert decision.offers == [("claude-qa2", "A")] and decision.openings == []
    busy = [seat("claude-qa", state="working", warm=True, expiry=T), seat("claude-qa2", state="working")]
    decision = run([("A", "1")], busy, opening_failed=True)
    assert decision == Decision([], [], [])  # every agent answers: the card waits, the opening is retried later
    assert run([("A", "1")], busy).openings == ["A"]


def test_no_open_while_limited():
    seats = [seat("claude-qa", warm=True, expiry=T, holds="X"), seat("claude-qa2", state="limited")]
    decision = run([("A", "1")], seats, any_limited=True)
    assert decision.openings == [] and decision.offers == []


def test_no_open_while_stopping():
    decision = run([("A", "1")], [seat("claude-qa", warm=True, expiry=T, holds="X")], stopping=True)
    assert decision.openings == [] and decision.offers == []
    decision = run([("A", "1")], [seat("claude-qa", warm=False), seat("claude-qa2", gone=True)], stopping=True)
    assert decision.offers == [] and decision.closings == ["claude-qa2"]  # a stopping pool takes no card


def test_close_after_search_only():
    seats = [seat("claude-qa", warm=False), seat("claude-qa2", warm=False, holds="Y"),
             seat("claude-qa3", warm=False, state="idle"), seat("claude-qa4", warm=True, expiry=T, holds="Z"),
             seat("claude-qa5", warm=False, state="working"), seat("claude-qa6", warm=False, offer="Q"),
             seat("claude-qa7", warm=False, holds="R", reserved=True), seat("claude-qa8", warm=False, pending=True)]
    assert run([], seats).closings == []  # no card needed an agent: idle cold agents stay open
    decision = run([("A", "1")], seats)
    assert decision.offers == [("claude-qa", "A")]
    assert decision.closings == ["claude-qa2", "claude-qa3"]  # never the router, a warm, busy, offered or reserved one
    assert "claude-qa" not in run([("A", "1")], [seat("claude-qa", warm=False), seat("claude-qa2", warm=False)]).closings


def test_card_with_outstanding_offer_skipped():
    decision = run([("A", "1")], [seat("claude-qa", warm=False), seat("claude-qa2", warm=False, offer="A")])
    assert decision == Decision([], [], [])


def test_ended_card_member_reused_same_round():
    seats = [seat("claude-qa", warm=True, expiry=T, holds="X"),
             seat("claude-qa2", warm=True, expiry=T, card_ended=True, state="idle"),
             seat("claude-qa3", warm=False, card_ended=True, state="idle")]
    assert run([], seats).closings == ["claude-qa2", "claude-qa3"]
    decision = run([("A", "1")], seats)
    assert decision.offers == [("claude-qa2", "A")] and decision.closings == ["claude-qa3"]
    router_ended = [seat("claude-qa", warm=False, card_ended=True, state="idle")]
    assert run([], router_ended).closings == []  # the router is never closed


def test_gone_member_closed():
    seats = [seat("claude-qa", warm=False), seat("claude-qa2", gone=True, state="stopped", holds="A")]
    decision = run([], seats)
    assert decision.closings == ["claude-qa2"]
    assert run([("B", "1")], seats).offers == [("claude-qa", "B")]


def test_closing_seat_is_available():
    """A seat whose conversation just ended (``closing``, on its way to ``idle``) is not answering: it is offered the
    card, never passed over for a new member."""
    decision = run([("A", "1")], [seat("claude-qa", state="closing", warm=True, expiry=T, card_ended=True)])
    assert decision.offers == [("claude-qa", "A")] and decision.openings == []


def test_round_skips_malformed_refs_and_unfinished_handoff_moves(kproject, fake_trello, monkeypatch):
    """A card whose ref is malformed, or a handed-off card whose move to ``Backlog`` is still outstanding, can be
    claimed by no seat: the round never routes it, so no seat keeps an offer it cannot take (final review, Codex #9)."""
    from kankan import gitutil, qapool, seats
    from kankan.refstore import card_ref
    from tests.helpers import add_qa_list, put_card_ref
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    seats.acquire_qa(project, "claude", None)
    broken = fake.add_card(qa_list, "malformed ref")
    oid = gitutil.out(project.paths.repo, ["hash-object", "-w", "--stdin"], input="not json")
    project.refs.create(card_ref(broken["id"]), oid)
    moving = fake.add_card(qa_list, "handed off, move outstanding")
    put_card_ref(project, moving["id"], moving["shortLink"], phase="parked", cause="handoff", handoff=["c1"],
                 from_list=qa_list, target_list=project.cfg.backlog, conversation="0123456789abcdef")
    good = fake.add_card(qa_list, "good")
    seen = []
    real = qapool.choose
    monkeypatch.setattr(qapool, "choose", lambda needing, views, **kw: seen.append(needing) or real(needing, views,
                                                                                                    **kw))
    monkeypatch.setattr("kankan.agents_cmd.start_member", lambda project, seat, tmux=None: None)
    qapool.Router(project, "claude-qa", lambda event, **fields: None).round()
    assert [card for card, _ in seen[0]] == [good["id"]]
