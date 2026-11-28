"""Offers (qapool T§4): the router gives a card to a seat through ``qa_offer`` in its state; the seat takes it
between turns, ending its own conversation (the card stays in ``Q&A``)."""

import pytest

from kankan import auditlog, faults, messages, qa, qapool, seats
from kankan.adapters import Classification, ModelEffort, load_adapter
from kankan.qa_launcher import QaLauncher
from kankan.refstore import card_ref
from kankan.seatstate import load_state
from tests.helpers import add_qa_list

PAIR = ModelEffort("sonnet", "high")
READY = Classification("ready")


class FakeTmux:
    def __init__(self):
        self.pasted = []

    def paste(self, pane, text, submit_key=None):
        self.pasted.append(text)


@pytest.fixture
def pool(kproject, fake_trello, monkeypatch):
    from kankan import clock
    monkeypatch.setattr(clock, "sleep", lambda seconds: None)
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "claude", None)

    class Env:
        pass
    env = Env()
    env.project, env.fake, env.qa_list, env.lease = project, fake, qa_list, lease
    env.a = fake.add_card(qa_list, "first", desc="first question?")
    env.b = fake.add_card(qa_list, "second", desc="second question?")
    env.launcher = launcher(env)
    return env


def launcher(env):
    result = QaLauncher(env.project, load_adapter(env.project.paths, "claude", read_only=True), FakeTmux(), "claude",
                        "claude-qa")
    result.lease, result.running_pair, result.pane = env.lease, PAIR, "%1"
    result.agent = object()
    result.state = "idle"
    return result


def hold(env, card):
    """The launcher holds ``card``'s conversation, after an answer (``qa_waiting``)."""
    task = qa.claim(env.project, env.lease, env.project.board.card(card["id"], qa.LIVE), PAIR)
    env.launcher.task = task
    env.launcher.state = "qa_waiting"
    return task


def flush(launcher):
    """The pane writes of an assignment, one per step (the new-conversation command, then the prompt)."""
    while launcher.pending_writes:
        launcher.flush_assignment(READY)


def owner(env, card):
    value = env.project.refs.read(card_ref(card["id"]))
    return value.meta.get("owner") if value and value.meta.get("phase") == "qa" else None


def test_offer_taken_ends_old_conversation_card_stays_in_qa(pool):
    hold(pool, pool.a)
    assert qapool.write_offer(pool.project, "claude-qa", pool.b["id"])
    assert load_state(pool.project.paths, "claude-qa").qa_offer["card"] == pool.b["id"]
    assert pool.launcher.take_offer(READY)
    flush(pool.launcher)
    assert owner(pool, pool.a) is None and pool.fake.card_list(pool.a["id"]) == pool.qa_list
    assert owner(pool, pool.b) == "claude-qa" and pool.launcher.task["card"] == pool.b["id"]
    assert pool.launcher.tmux.pasted[0] == pool.launcher.adapter.new_conversation
    assert "turn `" in pool.launcher.tmux.pasted[-1] and pool.launcher.state == "working"
    assert load_state(pool.project.paths, "claude-qa").qa_offer is None
    assert qapool.outstanding(pool.project) == {}


def test_offer_of_held_card_dropped(pool):
    task = hold(pool, pool.a)
    qapool.write_offer(pool.project, "claude-qa", pool.a["id"])
    assert pool.launcher.take_offer(READY)
    assert pool.launcher.task["id"] == task["id"] and pool.launcher.tmux.pasted == []
    assert load_state(pool.project.paths, "claude-qa").qa_offer is None
    assert owner(pool, pool.a) == "claude-qa"


def test_offer_card_left_qa_dropped(pool):
    hold(pool, pool.a)
    qapool.write_offer(pool.project, "claude-qa", pool.b["id"])
    pool.fake.state["cards"][pool.b["id"]]["idList"] = pool.project.cfg.done
    assert pool.launcher.take_offer(READY)
    assert owner(pool, pool.a) == "claude-qa" and owner(pool, pool.b) is None  # nothing ended for nothing
    assert load_state(pool.project.paths, "claude-qa").qa_offer is None
    assert any(e["event"] == "qa_offer_dropped" for e in auditlog.read(pool.project.paths))


def test_one_outstanding_offer_per_card(pool):
    assert qapool.write_offer(pool.project, "claude-qa", pool.b["id"])
    assert not qapool.write_offer(pool.project, "claude-qa2", pool.b["id"])
    assert not qapool.write_offer(pool.project, "claude-qa", pool.a["id"])  # one offer per seat too
    assert qapool.outstanding(pool.project) == {pool.b["id"]: "claude-qa"}


def test_offer_waits_for_pending_answer_settled(pool, monkeypatch):
    hold(pool, pool.a)
    qapool.write_offer(pool.project, "claude-qa", pool.b["id"])
    monkeypatch.setattr(pool.launcher, "settle_pending", lambda: "busy")
    assert not pool.launcher.take_offer(READY)
    assert owner(pool, pool.a) == "claude-qa" and load_state(pool.project.paths, "claude-qa").qa_offer
    pool.launcher.state = "working"  # an answering seat never takes an offer
    monkeypatch.setattr(pool.launcher, "settle_pending", lambda: None)
    assert not pool.launcher.take_offer(READY)


def test_crash_after_offer_written(pool, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "after_offer_written")
    with pytest.raises(faults.SimulatedCrash):
        qapool.write_offer(pool.project, "claude-qa", pool.b["id"])
    monkeypatch.delenv("KANKAN_FAULT")
    assert qapool.outstanding(pool.project) == {pool.b["id"]: "claude-qa"}  # never routed twice
    assert pool.launcher.take_offer(READY) and owner(pool, pool.b) == "claude-qa"


def test_crash_after_old_conversation_ended(pool, monkeypatch):
    hold(pool, pool.a)
    qapool.write_offer(pool.project, "claude-qa", pool.b["id"])
    monkeypatch.setenv("KANKAN_FAULT", "after_old_conversation_ended")
    with pytest.raises(faults.SimulatedCrash):
        pool.launcher.take_offer(READY)
    monkeypatch.delenv("KANKAN_FAULT")
    assert owner(pool, pool.a) is None
    restarted = launcher(pool)  # the launcher's restart: no conversation, the offer is still there
    assert restarted.take_offer(READY) and owner(pool, pool.b) == "claude-qa"
    flush(restarted)
    assert restarted.tmux.pasted[0] == restarted.adapter.new_conversation
