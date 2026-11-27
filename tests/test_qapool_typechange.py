"""The type change of the Q&A pool (qapool F§7 "Changing type", T§9; acceptance test F§8 14): ``kankan up qa=codex``
and ``kankan agent codex --qa`` with a Claude pool stop it at once, remove its seats (members first, completing the
answers they were posting) and start ``codex-qa`` with every card idle; a failed removal stops before ``codex-qa`` and
a rerun resumes. Also the replacement record of an earlier version, dropped by the router's start."""

import json

import pytest

from kankan import agents_cmd, auditlog, config, qa, qapool, seatremove, seats
from kankan.config import save_config
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref, seat_ref
from tests.helpers import add_qa_list, add_questions_seed, hold
from tests.launcher_helpers import wait_for
from tests.test_qapool_start import FakeTmux
from tests.test_qapool_stop import (CUT_SHORT, MEMBER, ROUTER, _answer, _begin, _parts, _register, chain,  # noqa: F401
                                    scenario, wait_polls, with_text)
from tests.test_seatremove_qa_pending import PAIR, _cut_short, _pending

NEW = "codex-qa"


@pytest.fixture(autouse=True)
def private_tmux(monkeypatch, tmux_server):
    """Whatever a command does with tmux goes to the test's private server, never to the user's."""
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)


@pytest.fixture
def qa_list(kproject, fake_trello):
    add_questions_seed()
    return add_qa_list(kproject, fake_trello)


def _change(project, command, monkeypatch, tmux=None):
    """``kankan up qa=codex`` or ``kankan agent codex --qa``, starting nothing (the session goes to ``tmux``)."""
    tmux = tmux or FakeTmux(project)
    if command == "up":
        monkeypatch.setattr(agents_cmd, "_wait_started", lambda project, session, what, started: None)
        agents_cmd.up(project, {}, tmux=tmux, qa="codex")
    else:
        agents_cmd.agent(project, "codex", tmux=tmux, preflight=False, qa=True)
    return tmux


def _qa_seats(project):
    return sorted(name for name, seat in project.reload_config().seats.items() if seat.role == "qa")


# ---------------------------------------------------------------------- unit: failure and rerun

@pytest.mark.parametrize("command", ["up", "agent"])
def test_type_change_trello_unreachable_then_rerun(kproject, fake_trello, monkeypatch, qa_list, holders, command):
    """F test 14 (Trello unreachable): removing the member that was posting fails, the command stops with
    ``needs_action`` naming it before codex-qa is registered, the pool staying stopped; a rerun completes the answer
    (nothing repeated), removes the old pool and starts codex-qa."""
    project, fake = kproject, fake_trello
    _register(project, ROUTER)
    holders.append(hold(project, ROUTER))
    env = _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER)
    fake.inject("GET", f"^/1/cards/{env.card['id']}$", "500", times=1000)
    tmux = FakeTmux(project)
    with pytest.raises(KankanError) as exc:
        _change(project, command, monkeypatch, tmux)
    assert exc.value.status == Status.NEEDS_ACTION and MEMBER in exc.value.message
    assert tmux.seen == [] and NEW not in project.reload_config().seats
    assert qapool.stopping(project.paths)
    assert MEMBER in project.cfg.seats and _pending(project, env) is not None and _parts(fake, env.card) == ["1/3"]
    assert seats.liveness(project, ROUTER) != "alive"  # stopped at once
    fake.faults.clear()
    tmux = _change(project, command, monkeypatch)
    assert tmux.seen == [False]  # the mark is cleared before codex-qa starts
    assert _parts(fake, env.card) == ["1/3", "2/3", "3/3"]
    assert _qa_seats(project) == [NEW] and not qapool.stopping(project.paths)
    assert project.refs.read(card_ref(env.card["id"])) is None and fake.card_list(env.card["id"]) == qa_list


@pytest.mark.parametrize("marked", [True, False])
@pytest.mark.parametrize("command", ["up", "agent"])
def test_type_change_rerun_finds_old_pool_after_router_removed(kproject, fake_trello, monkeypatch, qa_list, command,
                                                               marked):
    """T§9: an earlier run removed the old router but left a member (marked for removal or not): the rerun finds the
    old pool through it, completes its answer and removes it before registering codex-qa."""
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER)
    if marked:
        cfg = project.reload_config()
        cfg.seats[MEMBER].removing = "immediate"
        save_config(project.paths, cfg)
    qapool.set_stopping(project, True)
    assert _qa_seats(project) == [MEMBER] and project.reload_config().qa_seat() is None
    tmux = _change(project, command, monkeypatch)
    assert tmux.seen == [False]
    assert _parts(fake, env.card) == ["1/3", "2/3", "3/3"]
    assert _qa_seats(project) == [NEW] and not qapool.stopping(project.paths)


def test_upgrade_with_outstanding_qa_replacing_record(kproject, fake_trello, monkeypatch, qa_list):
    """Task 11: a ``qa-replacing.json`` left by the previous version (codex-qa being replaced by claude-qa) and a
    ``qa`` ref owned by the no longer registered codex-qa: the router's start drops the record and ends that
    conversation as after a type change (the card stays in Q&A, its directory goes)."""
    project, fake = kproject, fake_trello
    lease = seats.acquire_qa(project, "codex", None)
    card = fake.add_card(qa_list, "question", desc="how does it work?")
    task = qa.claim(project, lease, project.trello.card(card["id"]), PAIR)
    qa.start_turn(project, lease, task, qa.Inputs([], []))
    seats._release_fd(lease.fd)  # the old seat's launcher is gone, its seat ref stays
    assert qa.conversation_dir(project.paths, task["conversation"]).exists()
    cfg = project.reload_config()
    del cfg.seats[NEW]  # the previous version swapped the entries in one configuration write
    save_config(project.paths, cfg)
    record = project.paths.kankan_dir / "qa-replacing.json"
    record.write_text(json.dumps({"old": NEW, "new": ROUTER}))
    lease = seats.acquire_qa(project, "claude", None)
    try:
        qapool.router_start(project, lease)
    finally:
        seats.release(project, lease)
    assert not record.exists()
    assert project.refs.read(card_ref(card["id"])) is None and fake.card_list(card["id"]) == qa_list
    assert not qa.conversation_dir(project.paths, task["conversation"]).exists()
    assert project.refs.read(seat_ref(NEW)) is None
    assert _qa_seats(project) == [ROUTER]
    events = [e["event"] for e in auditlog.read(project.paths)]
    assert "qa_replacement_record_dropped" in events and "qa_unregistered_ended" in events


def test_type_change_race_stops_before_registering(kproject, fake_trello, monkeypatch, qa_list):
    """T§9: a Claude pool registered by a concurrent command after ``up qa=codex`` removed the old pool and before it
    takes ``up.lock`` again: the command stops with ``needs_action`` without registering codex-qa, and removes no
    seat while it holds ``up.lock`` (lock order: the seat-removal lock comes before it)."""
    project = kproject
    real_change, real_complete = qapool.change_type, seatremove.complete
    up_lock = project.paths.kankan_dir / "up.lock"
    under_up_lock = []

    def change_type(project_, agent_type):
        removed = real_change(project_, agent_type)
        _register(project_, ROUTER)  # `kankan agent claude --qa` meanwhile
        monkeypatch.setattr(qapool, "change_type", real_change)
        return removed

    def complete(project_, seat):
        under_up_lock.append(str(up_lock) in config._LOCK_DEPTH)
        return real_complete(project_, seat)
    monkeypatch.setattr(qapool, "change_type", change_type)
    monkeypatch.setattr(seatremove, "complete", complete)
    tmux = FakeTmux(project)
    with pytest.raises(KankanError) as exc:
        _change(project, "up", monkeypatch, tmux)
    assert exc.value.status == Status.NEEDS_ACTION and ROUTER in exc.value.message
    assert tmux.seen == [] and NEW not in project.reload_config().seats and ROUTER in project.cfg.seats
    assert True not in under_up_lock


# ---------------------------------------------------------------------- processes

@pytest.mark.tmux
@pytest.mark.slow
@pytest.mark.parametrize("command", ["up", "agent"])
def test_type_change_completes_member_answer_and_starts_new_router(chain, command):
    """F test 14: with claude-qa2 posting a long answer (part 1 posted), ``kankan up qa=codex`` (or ``kankan agent
    codex --qa``) stops the Claude pool at once, completes the answer without repeating its posted part, removes the
    Claude seats and starts codex-qa; every card is idle: nothing is answered again."""
    env = chain
    card = _begin(env, MEMBER, [CUT_SHORT])
    assert wait_for(lambda: _parts(env.fake, card) == ["1/3"], timeout=40)
    scenario(env, NEW, [_answer("Answered by codex.")])
    if command == "up":
        agents_cmd.up(env.project, {}, qa="codex")
    else:
        agents_cmd.agent(env.project, "codex", qa=True)
    assert _parts(env.fake, card) == ["1/3", "2/3", "3/3"]
    assert _qa_seats(env.project) == [NEW] and not qapool.stopping(env.project.paths)
    for name in (ROUTER, MEMBER):
        assert seats.liveness(env.project, name) == "free"
        assert not (env.project.paths.seat_worktree(name) / ".git").exists()
    assert wait_for(lambda: seats.liveness(env.project, NEW) == "alive", timeout=30)
    wait_polls(env)
    assert project_ref(env, card) is None  # idle
    assert _parts(env.fake, card) == ["1/3", "2/3", "3/3"] and not with_text(env, card, "Answered by codex.")
    env.fake.user_comment(card["id"], "and what about X?")
    assert wait_for(lambda: with_text(env, card, "Answered by codex."), timeout=40)
    assert project_ref(env, card).meta["owner"] == NEW


def project_ref(env, card):
    return env.project.refs.read(card_ref(card["id"]))


def test_type_change_lists_the_old_pool_after_setting_the_stopping_mark(kproject, fake_trello, monkeypatch):
    """A member the old router registers while the type change starts is removed with the rest: the seats are listed
    once the stopping mark is set (final review, Codex #1)."""
    from kankan import qapool, seatremove
    from kankan.config import SeatConfig, load_config, save_config
    cfg = load_config(kproject.paths)
    cfg.seats["codex-qa"] = SeatConfig(type="codex", role="qa")
    save_config(kproject.paths, cfg)
    real = qapool.set_stopping

    def racing(project, value):
        current = load_config(project.paths)  # the old router registers a member just before the mark
        current.seats["codex-qa2"] = SeatConfig(type="codex", role="qa")
        save_config(project.paths, current)
        real(project, value)
    monkeypatch.setattr(qapool, "set_stopping", racing)
    monkeypatch.setattr(seatremove, "stop_seat", lambda project, seat, kill_alive: None)
    removed = []
    monkeypatch.setattr(seatremove, "complete", lambda project, seat: removed.append(seat))
    assert qapool.change_type(kproject, "claude") == ["codex-qa2", "codex-qa"]
    assert removed == ["codex-qa2", "codex-qa"]
