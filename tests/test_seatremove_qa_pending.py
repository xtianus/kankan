"""qapool T§14 (seat §6 Completion): removing a Q&A seat completes the pending answer or handoff of its conversations
with an administrative lease before ending them."""

import os

import pytest

from kankan import agents_cmd, auditlog, faults, procutil, qa, qa_answer, qapool, seatremove, seats
from kankan.adapters import ModelEffort
from kankan.config import SeatConfig, project_lock, save_config
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref, seat_ref
from kankan.seatstate import load_state
from tests.flow import agent_ctx
from tests.helpers import add_qa_list

PAIR = ModelEffort("sonnet", "high")


def long_text(paragraphs=40):
    return (("word " * 199 + "word") + "\n\n") * paragraphs


def _register_member(project, seat):
    cfg = project.reload_config()
    cfg.seats[seat] = SeatConfig(type="claude", role="qa")
    save_config(project.paths, cfg)
    project.reload_config()


class Conv:
    pass


def _conversation(project, fake, monkeypatch, qa_list, seat="claude-qa", title="question"):
    """A claimed Q&A card on ``seat`` with one delivered question and a started turn; the seat stays occupied."""
    if qapool.is_member(seat):
        _register_member(project, seat)
    lease = seats.acquire_qa(project, "claude", None, seat=seat)
    card = fake.add_card(qa_list, title, desc="how does it work?")
    question = fake.user_comment(card["id"], "and what about X?")["id"]
    task = qa.claim(project, lease, project.trello.card(card["id"]), PAIR)
    token = qa.start_turn(project, lease, task, qa.Inputs([question], []))
    env = Conv()
    env.seat, env.lease, env.card, env.question, env.token = seat, lease, card, question, token
    env.ctx = agent_ctx(project, lease, monkeypatch)
    env.task = env.ctx.task
    return env


def _crash(monkeypatch, fault, call):
    monkeypatch.setenv("KANKAN_FAULT", fault)
    with pytest.raises(faults.SimulatedCrash):
        call()
    monkeypatch.delenv("KANKAN_FAULT")


def _cut_short(project, fake, monkeypatch, qa_list, seat="claude-qa", title="question"):
    """A seat whose agent died after posting part 1/3 of an answer (flock released, seat ref kept)."""
    env = _conversation(project, fake, monkeypatch, qa_list, seat, title)
    _crash(monkeypatch, "qa_after_chunk", lambda: qa_answer.answer(env.ctx, long_text(), env.token))
    assert len(_answers(fake, env.card)) == 1
    seats._release_fd(env.lease.fd)  # the agent died: its seat ref stays
    return env


def _answers(fake, card, prefix="qa"):
    return [text for text in fake.card_comments(card["id"]) if f"`kankan:{prefix}-" in text]


def _posts(fake):
    return sum(1 for m, p in fake.requests if m == "POST" and p.endswith("/actions/comments"))


def _pending(project, env):
    return qa_answer.read_pending(project.paths, env.task["conversation"])


def _removing(project, seat, mode="immediate"):
    cfg = project.reload_config()
    cfg.seats[seat].removing = mode
    save_config(project.paths, cfg)
    project.reload_config()


def _entry(project, card):
    return qapool.load_record(project.paths).get(card["id"])


@pytest.fixture
def qa_list(kproject, fake_trello):
    return add_qa_list(kproject, fake_trello)


# ---------------------------------------------------------------------- completion at removal

def test_remove_qa_seat_completes_cut_short_answer(kproject, fake_trello, monkeypatch, qa_list):
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list)
    posts = _posts(fake)
    seatremove.complete(project, "claude-qa")
    assert [c.split("\n", 1)[0] for c in _answers(fake, env.card)] == ["1/3", "2/3", "3/3"]
    assert _posts(fake) - posts == 2  # nothing repeated
    assert "claude-qa" not in project.reload_config().seats
    assert project.refs.read(card_ref(env.card["id"])) is None and fake.card_list(env.card["id"]) == qa_list
    assert not qa.conversation_dir(project.paths, env.task["conversation"]).exists()
    last = [c for c in project.trello.comments(env.card["id"]) if c.text.startswith("3/3")][0]
    entry = _entry(project, env.card)
    assert entry["answered"]["id"] == last.id
    assert entry["delivered"]["description"] is True and entry["delivered"]["comment"] is not None


def test_remove_qa_seat_pending_handoff_already_transitioned_only_signals(kproject, fake_trello, monkeypatch,
                                                                          qa_list):
    project, fake = kproject, fake_trello
    env = _conversation(project, fake, monkeypatch, qa_list)
    _crash(monkeypatch, "qa_after_handoff_tx", lambda: qa_answer.handoff(env.ctx, "summary", env.token))
    seats._release_fd(env.lease.fd)
    assert project.refs.read(card_ref(env.card["id"])).meta["phase"] == "parked"
    assert _pending(project, env) is not None
    posts = _posts(fake)
    seatremove.complete(project, "claude-qa")
    assert _posts(fake) == posts and len(_answers(fake, env.card, "qh")) == 1
    assert _pending(project, env) is None
    meta = project.refs.read(card_ref(env.card["id"])).meta
    assert meta["phase"] == "parked" and meta["cause"] == "handoff"
    assert "claude-qa" not in project.reload_config().seats
    assert not [e for e in auditlog.read(project.paths) if e["event"] == "signal" and e.get("kind") == "handoff"]


def test_remove_qa_seat_card_left_qa_ends_conversation(kproject, fake_trello, monkeypatch, qa_list):
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list)
    fake.user_move(env.card["id"], project.cfg.done)
    posts = _posts(fake)
    seatremove.complete(project, "claude-qa")
    assert _posts(fake) == posts and len(_answers(fake, env.card)) == 1
    assert "claude-qa" not in project.reload_config().seats
    assert project.refs.read(card_ref(env.card["id"])) is None
    assert not qa.conversation_dir(project.paths, env.task["conversation"]).exists()


def test_remove_qa_seat_trello_unreachable_keeps_seat_registered(kproject, fake_trello, monkeypatch, qa_list):
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list)
    fake.inject("GET", f"^/1/cards/{env.card['id']}$", "500", times=100)
    with pytest.raises(KankanError) as err:
        seatremove.complete(project, "claude-qa")
    assert err.value.status == Status.NEEDS_ACTION
    seat_cfg = project.reload_config().seats["claude-qa"]
    assert seat_cfg.removing == "immediate" and seat_cfg.remove_error
    assert _pending(project, env) is not None and len(_answers(fake, env.card)) == 1
    assert project.refs.read(card_ref(env.card["id"])).meta["phase"] == "qa"
    assert seats.liveness(project, "claude-qa") == "free"  # the administrative lease was released
    fake.faults.clear()
    seatremove.complete(project, "claude-qa")
    assert [c.split("\n", 1)[0] for c in _answers(fake, env.card)] == ["1/3", "2/3", "3/3"]
    assert "claude-qa" not in project.reload_config().seats


def test_remove_qa_seat_revoked_lease_keeps_pending(kproject, fake_trello, monkeypatch, qa_list):
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list)
    real = qa.check_seat

    def revoked(project_, lease):
        value = project_.refs.read(seat_ref(lease.seat))
        if value is not None:  # `kankan seat recover --kill` by the user while the completion runs
            project_.refs.delete(seat_ref(lease.seat), value.oid)
        return real(project_, lease)
    monkeypatch.setattr(qa, "check_seat", revoked)
    with pytest.raises(KankanError) as err:
        seatremove.complete(project, "claude-qa")
    monkeypatch.setattr(qa, "check_seat", real)
    assert err.value.status == Status.NEEDS_ACTION
    assert "claude-qa" in project.reload_config().seats and project.cfg.seats["claude-qa"].remove_error
    assert _pending(project, env) is not None and len(_answers(fake, env.card)) == 1
    assert project.refs.read(card_ref(env.card["id"])).meta["phase"] == "qa"
    seatremove.complete(project, "claude-qa")
    assert [c.split("\n", 1)[0] for c in _answers(fake, env.card)] == ["1/3", "2/3", "3/3"]


def test_crash_after_delivered_recorded_retry_posts_nothing_twice(kproject, fake_trello, monkeypatch, qa_list):
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list)
    _crash(monkeypatch, "after_delivered_recorded", lambda: seatremove.complete(project, "claude-qa"))
    assert _pending(project, env) is not None and len(_answers(fake, env.card)) == 3
    recorded = _entry(project, env.card)
    posts = _posts(fake)
    seatremove.complete(project, "claude-qa")
    assert _posts(fake) == posts and len(_answers(fake, env.card)) == 3
    assert _entry(project, env.card) == recorded
    assert "claude-qa" not in project.reload_config().seats


def test_remove_one_member_leaves_other_members_pending_alone(kproject, fake_trello, monkeypatch, qa_list):
    project, fake = kproject, fake_trello
    two = _cut_short(project, fake, monkeypatch, qa_list, "claude-qa2", "first")
    three = _cut_short(project, fake, monkeypatch, qa_list, "claude-qa3", "second")
    seatremove.complete(project, "claude-qa2")
    assert len(_answers(fake, two.card)) == 3 and "claude-qa2" not in project.reload_config().seats
    assert len(_answers(fake, three.card)) == 1 and len(_pending(project, three)["posted"]) == 1
    meta = project.refs.read(card_ref(three.card["id"])).meta
    assert meta["owner"] == "claude-qa3" and meta["conversation"] == three.task["conversation"]
    assert "claude-qa3" in project.cfg.seats


# ---------------------------------------------------------------------- the administrative lease

def test_admin_lease_records_no_caller_pane(kproject, fake_trello, monkeypatch, qa_list):
    project = kproject
    seats.release(project, seats.acquire_qa(project, "claude", None))
    monkeypatch.setattr(seats, "pane_sid", lambda: os.getsid(0))  # a plain occupation would record this pane
    lease = seats.occupy_admin(project, "claude-qa")
    assert project.refs.read(seat_ref("claude-qa")).oid == lease.seat_oid
    assert seats.read_proc(project, "claude-qa") == {"boot_id": procutil.boot_id()}
    seats._release_fd(lease.fd)  # the completion crashed: the seat ref stays
    killed = []
    monkeypatch.setattr(procutil, "terminate_session", lambda *a, **k: killed.append(("session", a)) or [])
    monkeypatch.setattr(procutil, "terminate_pids", lambda pids, **k: (pids and killed.append(("pids", pids))) or [])
    assert seats.recover_dead(project, "claude-qa", kill_alive=True) == []
    assert killed == []


def test_admin_lease_refused_while_the_seat_ref_exists(kproject, fake_trello, qa_list):
    project = kproject
    lease = seats.acquire_qa(project, "claude", None)
    seats._release_fd(lease.fd)
    with pytest.raises(KankanError) as err:
        seats.occupy_admin(project, "claude-qa")
    assert err.value.status == Status.NEEDS_ACTION
    assert project.refs.read(seat_ref("claude-qa")).oid == lease.seat_oid


def test_admin_completion_writes_no_signal(kproject, fake_trello, monkeypatch, qa_list):
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list)
    with project_lock(project.paths):
        project.refs.delete(seat_ref("claude-qa"), project.refs.read(seat_ref("claude-qa")).oid)
    monkeypatch.setattr(qa_answer, "_commit_check", lambda *a, **k: pytest.fail("no commit check"))
    lease = seats.occupy_admin(project, "claude-qa")
    try:
        assert qa_answer.complete_pending(project, lease, env.task, admin=True) == "answer"
    finally:
        seats.release(project, lease)
    assert load_state(project.paths, "claude-qa").signal is None
    assert not [e for e in auditlog.read(project.paths) if e["event"] == "signal"]
    assert len(_answers(fake, env.card)) == 3 and _pending(project, env) is None
    assert project.refs.read(card_ref(env.card["id"])).meta["covered"]["comment"] == env.question
    turn = qa.read_turn(project.paths, env.task["conversation"])
    assert [c["kind"] for c in turn["completed"]] == ["answer"]


# ---------------------------------------------------------------------- resume_removals

def test_resume_removals_completes_pending(kproject, fake_trello, monkeypatch, qa_list):
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list)
    _removing(project, "claude-qa", "immediate")
    result = agents_cmd.resume_removals(project, tmux=object())
    assert result == {"acted": ["claude-qa"], "failed": {}}
    assert len(_answers(fake, env.card)) == 3 and "claude-qa" not in project.reload_config().seats


def test_resume_removals_completes_stopped_graceful_member_instead_of_starting(kproject, fake_trello, monkeypatch,
                                                                               qa_list):
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list, "claude-qa2")
    with project_lock(project.paths):  # stopped, not dead
        project.refs.delete(seat_ref("claude-qa2"), project.refs.read(seat_ref("claude-qa2")).oid)
    _removing(project, "claude-qa2", "graceful")
    assert seatremove.has_work(project, "claude-qa2")
    starts = []
    monkeypatch.setattr(agents_cmd, "agent", lambda *a, **k: starts.append((a, k)) or "session")
    result = agents_cmd.resume_removals(project, tmux=object())
    assert starts == [] and result == {"acted": ["claude-qa2"], "failed": {}}
    assert len(_answers(fake, env.card)) == 3 and "claude-qa2" not in project.reload_config().seats


def test_router_start_completes_a_handoff_interrupted_after_its_transaction(kproject, fake_trello, monkeypatch,
                                                                             qa_list):
    """qapool T§9: a handoff whose ``parked`` transaction committed before a crash (no owner left on the ref) is
    completed by the router's next start, as by a removal (final review, Codex #8)."""
    from kankan import qapool
    project, fake = kproject, fake_trello
    env = _conversation(project, fake, monkeypatch, qa_list)
    _crash(monkeypatch, "qa_after_handoff_tx", lambda: qa_answer.handoff(env.ctx, "summary", env.token))
    seats._release_fd(env.lease.fd)
    assert _pending(project, env) is not None
    lease = seats.acquire_qa(project, "claude", None)
    qapool.router_start(project, lease)
    assert _pending(project, env) is None and len(_answers(fake, env.card, "qh")) == 1
    meta = project.refs.read(card_ref(env.card["id"])).meta
    assert meta["phase"] == "parked" and meta["cause"] == "handoff"
