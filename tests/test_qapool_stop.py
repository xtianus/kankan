"""Stop and start of the Q&A pool (qapool F§7, T§9; acceptance tests F§8 12, 13, 15, 16, 18, 21, 23): the stopping
mark, the members removed as they stop (their cut-short answers completed, or at the next start when Trello cannot be
reached), the router's start, the stop record and the seat commands on the pool."""

import json
import os
import signal
import threading
import time
from types import SimpleNamespace

import pytest

from kankan import agents_cmd, auditlog, cli, qa, qapool, seats, stopall
from kankan.adapters import ModelEffort
from kankan.config import SeatConfig, load_config, save_config
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref, seat_ref
from kankan.seatstate import load_state
from kankan.tmux import Tmux
from tests.helpers import NoTmux, add_qa_list, add_questions_seed, bind_project, hold
from tests.launcher_helpers import agent_events, agent_inputs, launcher_state, proc_info, wait_for
from tests.test_seatremove_qa_pending import _answers, _conversation, _cut_short, _pending, _posts

ROUTER = "claude-qa"
MEMBER = "claude-qa2"
THIRD = "claude-qa3"
PAIR = ModelEffort("sonnet", "high")
FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=0.05, qa_poll_seconds=1)


def _register(project, *names):
    cfg = load_config(project.paths)
    for name in names:
        cfg.seats[name] = SeatConfig(type=seats.seat_type(name), role="qa")
    save_config(project.paths, cfg)
    project.reload_config()


def _parts(fake, card):
    return [text.split("\n", 1)[0] for text in _answers(fake, card)]


def _stop(project, graceful=False):
    """``kankan stop --yes`` (immediate unless ``graceful``) without tmux sessions."""
    return stopall.stop_all(project, graceful=graceful, confirm=lambda text: True, say=lambda text: None,
                            tmux=NoTmux(), poll=0.05)


def _stub_starts(monkeypatch):
    """``agents_cmd.agent`` records the seats it would start instead of starting a supervisor."""
    started = []

    def agent(project, agent_type, seat=None, attach=False, tmux=None, preflight=True, qa=False, create=True):
        if qa and create:
            agents_cmd._register_qa_seat(project, agent_type)
            qapool.clear_for_start(project)
        started.append(seats.qa_seat_name(agent_type) if qa else seat)
        return f"ai-proj-{agent_type}-starting-abc"
    monkeypatch.setattr(agents_cmd, "agent", agent)
    monkeypatch.setattr(agents_cmd, "_wait_started", lambda project, session, what, started: None)
    return started


@pytest.fixture(autouse=True)
def private_tmux(monkeypatch, tmux_server):
    """Whatever a command does with tmux goes to the test's private server, never to the user's."""
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)


@pytest.fixture
def qa_list(kproject, fake_trello):
    add_questions_seed()
    return add_qa_list(kproject, fake_trello)


# ---------------------------------------------------------------------- unit: immediate stop of a member (F test 13)

def test_immediate_stop_completes_member_answer_as_it_is_removed(kproject, fake_trello, monkeypatch, qa_list,
                                                                 holders):
    """F test 13 (member): the member whose agent was killed while posting its answer is removed by the stop, its
    answer completed without repeating the posted part; the router stays registered and the record keeps qa=claude."""
    project, fake = kproject, fake_trello
    _register(project, ROUTER)
    holders.append(hold(project, ROUTER))
    env = _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER)
    posts = _posts(fake)
    report = _stop(project)
    assert report["remaining"] == []
    assert _parts(fake, env.card) == ["1/3", "2/3", "3/3"] and _posts(fake) - posts == 2
    assert set(project.reload_config().seats) == {ROUTER}
    assert project.refs.read(card_ref(env.card["id"])) is None and fake.card_list(env.card["id"]) == qa_list
    assert qapool.stopping(project.paths)
    assert stopall.load_record(project.paths)["up"] == "qa=claude"
    assert report["stopped"] == [ROUTER]


def test_immediate_stop_trello_unreachable_leaves_member_then_start_completes(kproject, fake_trello, monkeypatch,
                                                                              qa_list, holders):
    """F test 13 (member, Trello unreachable at its removal): the stop reports it and leaves it registered with its
    pending answer; ``kankan start`` completes the answer (nothing repeated) and removes it before the router starts."""
    project, fake = kproject, fake_trello
    _register(project, ROUTER)
    holders.append(hold(project, ROUTER))
    env = _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER)
    fake.inject("GET", f"^/1/cards/{env.card['id']}$", "500", times=1000)
    with pytest.raises(KankanError) as exc:
        _stop(project)
    assert exc.value.status == Status.NEEDS_ACTION and MEMBER in exc.value.message
    assert any(MEMBER in entry["what"] for entry in exc.value.data["remaining"])
    assert MEMBER in project.reload_config().seats and _pending(project, env) is not None
    assert _parts(fake, env.card) == ["1/3"]
    assert stopall.load_record(project.paths)["up"] == "qa=claude"
    fake.faults.clear()
    started = _stub_starts(monkeypatch)
    posts = _posts(fake)
    stopall.start_all(project)
    assert started == [ROUTER]
    assert _parts(fake, env.card) == ["1/3", "2/3", "3/3"] and _posts(fake) - posts == 2
    assert set(project.reload_config().seats) == {ROUTER} and not qapool.stopping(project.paths)


def test_router_start_completes_a_member_left_registered(kproject, fake_trello, monkeypatch, qa_list):
    """T§9: a member whose removal failed at the stop is completed and removed by the next router start."""
    project, fake = kproject, fake_trello
    _register(project, ROUTER)
    env = _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER)
    cfg = project.reload_config()
    cfg.seats[MEMBER].removing, cfg.seats[MEMBER].remove_error = "immediate", "Trello unreachable"
    save_config(project.paths, cfg)
    posts = _posts(fake)
    lease = seats.acquire_qa(project, "claude", None)
    try:
        qapool.router_start(project, lease)
    finally:
        seats.release(project, lease)
    assert _parts(fake, env.card) == ["1/3", "2/3", "3/3"] and _posts(fake) - posts == 2
    assert set(project.reload_config().seats) == {ROUTER}


def test_router_start_completes_its_own_cut_short_answer(kproject, fake_trello, monkeypatch, qa_list):
    """F test 13 (router), at its start: the router's own cut-short answer is completed with its own lease, nothing
    repeated, and its conversation ended with the card idle in Q&A."""
    project, fake = kproject, fake_trello
    env = _cut_short(project, fake, monkeypatch, qa_list, seat=ROUTER)
    posts = _posts(fake)
    lease = seats.acquire_qa(project, "claude", None)
    try:
        qapool.router_start(project, lease)
    finally:
        seats.release(project, lease)
    # nothing repeated: the two missing parts, besides the recovery comment of the router's dead seat
    assert _parts(fake, env.card) == ["1/3", "2/3", "3/3"] and _posts(fake) - posts == 3
    assert project.refs.read(card_ref(env.card["id"])) is None and fake.card_list(env.card["id"]) == qa_list
    entry = qapool.load_record(project.paths)[env.card["id"]]
    assert entry["answered"]["id"] and not qapool.needs_agent(entry, None, holder_alive=False)


# ---------------------------------------------------------------------- unit: registration and stop

def test_stop_between_member_registration_and_start(kproject, fake_trello, monkeypatch, qa_list, holders):
    """T§12 Stop: the router registers a member under its locks; a stop comes before the member is started. The stop
    removes the registered member, and its launcher, started afterwards, is refused: no member registered or running."""
    project, fake = kproject, fake_trello
    _register(project, ROUTER)
    holders.append(hold(project, ROUTER))
    card_a = fake.add_card(qa_list, "a", desc="first?")
    card_b = fake.add_card(qa_list, "b", desc="second?")
    router = qapool.Router(project, ROUTER, lambda *args, **fields: None)
    refused = []

    def start_member(project_, name, tmux=None):
        assert name in project_.reload_config().seats  # registered, not started
        _stop(project_)  # `kankan stop` now
        with pytest.raises(KankanError) as exc:  # what the member's launcher meets when its supervisor runs
            seats.acquire_qa(project_, "claude", None, create=False, seat=name)
        refused.append((name, exc.value.status))
        return "ai-proj-claude-starting-abc"
    monkeypatch.setattr(agents_cmd, "start_member", start_member)
    views = [qapool.SeatView(name=ROUTER, state="working", holds=card_a["id"], warm=True, expiry=time.time() + 600)]
    decision = router.decide([(card_b["id"], qapool.wait_key(card_b["id"], None))], views)
    assert decision.openings == [MEMBER]
    router.after_locks(decision, views)
    assert refused == [(MEMBER, Status.CONFIG_ERROR)]
    assert set(project.reload_config().seats) == {ROUTER}
    assert seats.liveness(project, MEMBER) == "free" and project.refs.read(seat_ref(MEMBER)) is None
    assert not (project.paths.seats_dir / f"{MEMBER}.state.json").exists()
    assert router.decide([(card_b["id"], "0")], views).openings == []  # stopping: no member opens


def test_router_start_leaves_alive_members_alone(kproject, fake_trello, monkeypatch, qa_list):
    """T§9: the router's start removes the members whose launcher is gone (a gone one and a marked one) and leaves a
    running member's conversation alone."""
    project, fake = kproject, fake_trello
    _register(project, ROUTER)
    alive = _conversation(project, fake, monkeypatch, qa_list, seat=MEMBER, title="held")  # lease kept: alive
    gone = _cut_short(project, fake, monkeypatch, qa_list, seat=THIRD, title="gone")
    _register(project, "claude-qa4")  # registered by the router before a crash, never started
    before = project.refs.read(card_ref(alive.card["id"]))
    posts = _posts(fake)
    lease = seats.acquire_qa(project, "claude", None)
    try:
        qapool.router_start(project, lease)
    finally:
        seats.release(project, lease)
    assert set(project.reload_config().seats) == {ROUTER, MEMBER}
    after = project.refs.read(card_ref(alive.card["id"]))
    assert after.oid == before.oid and after.meta["owner"] == MEMBER
    assert qa.conversation_dir(project.paths, alive.task["conversation"]).exists()
    assert load_state(project.paths, MEMBER).task["id"] == alive.task["id"]
    assert seats.liveness(project, MEMBER) == "alive"
    assert _parts(fake, gone.card) == ["1/3", "2/3", "3/3"] and _posts(fake) - posts == 2
    assert project.refs.read(card_ref(gone.card["id"])) is None
    seats.release(project, alive.lease)


def test_router_restart_keeps_same_seat_take_over(kproject, fake_trello, monkeypatch, qa_list):
    """Task 11: ``qa.take_over`` stays for a seat's own conversation after its launcher restarts (a member here): a
    new task ID, the same checkout base, reflog length and turn."""
    project, fake = kproject, fake_trello
    _register(project, ROUTER)
    env = _conversation(project, fake, monkeypatch, qa_list, seat=MEMBER)
    old = load_state(project.paths, MEMBER).task
    assert old["turn"] == env.token and old.get("base")
    seats._release_fd(env.lease.fd)  # the launcher died; its supervisor starts another one
    lease = seats.acquire_qa(project, "claude", None, create=False, seat=MEMBER)
    try:
        value = project.refs.read(card_ref(env.card["id"]))
        task = qa.take_over(project, lease, value, PAIR)
    finally:
        seats.release(project, lease)
    assert task["id"] != old["id"] and task["conversation"] == old["conversation"]
    assert (task["base"], task.get("reflog", 0), task["turn"]) == (old["base"], old.get("reflog", 0), old["turn"])
    assert project.refs.read(card_ref(env.card["id"])).meta["owner"] == MEMBER


def test_stop_record_keeps_qa_type_with_router_between_restarts(kproject, fake_trello, qa_list, holders):
    """T§9: with the router between two supervisor restarts (dead), a live member and no autostart unit, the stop
    record still restores ``qa=claude`` (the router only), never the member."""
    project = kproject
    seats._release_fd(seats.acquire_qa(project, "claude", None).fd)  # the router's launcher died: seat ref kept
    assert seats.liveness(project, ROUTER) == "dead"
    _register(project, MEMBER)
    holders.append(hold(project, MEMBER))
    report = _stop(project)
    record = stopall.load_record(project.paths)
    assert record["up"] == "qa=claude" and record["autostart"] is False
    assert report["restart"] == ["kankan up qa=claude"]
    assert set(project.reload_config().seats) == {ROUTER}


# ---------------------------------------------------------------------- unit: seat commands (F test 15)

def _cli(capsys, *argv):
    code = cli.main(list(argv) + ["--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


@pytest.fixture
def in_repo(kproject, monkeypatch, tmux_server):
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    monkeypatch.chdir(kproject.paths.repo)
    return kproject


@pytest.mark.parametrize("action", ["start", "stop", "remove"])
def test_seat_commands_refuse_a_member(in_repo, qa_list, capsys, action):
    project = in_repo
    _register(project, ROUTER, MEMBER)
    argv = ["seat", action, MEMBER] + (["--yes"] if action == "remove" else [])
    code, data = _cli(capsys, *argv)
    assert data["status"] == Status.USAGE_ERROR.value and code != 0
    assert ROUTER in data["message"]
    assert set(project.reload_config().seats) == {ROUTER, MEMBER} and not qapool.stopping(project.paths)


def test_seat_remove_router_removes_the_whole_pool(in_repo, fake_trello, monkeypatch, qa_list, holders, capsys):
    """F test 15: ``kankan seat remove claude-qa`` stops and removes the members (a running one, and one whose
    answer was cut short, completed) and then the router."""
    project, fake = in_repo, fake_trello
    _register(project, ROUTER, MEMBER)
    holders.append(hold(project, ROUTER))
    holders.append(hold(project, MEMBER))
    env = _cut_short(project, fake, monkeypatch, qa_list, seat=THIRD)
    code, data = _cli(capsys, "seat", "remove", ROUTER, "--yes")
    assert code == 0, data
    assert not [name for name in project.reload_config().seats if seats.is_qa(name)]
    assert _parts(fake, env.card) == ["1/3", "2/3", "3/3"]
    for name in (ROUTER, MEMBER, THIRD):
        assert seats.liveness(project, name) == "free"
        assert not (project.paths.seat_worktree(name) / ".git").exists()


def test_seat_stop_router_stops_the_pool(in_repo, fake_trello, qa_list, holders, capsys):
    """F§7: ``kankan seat stop claude-qa`` sets the stopping mark, marks the members for graceful removal and asks
    every seat of the pool to stop; a member already stopped is removed at once."""
    project = in_repo
    _register(project, ROUTER, MEMBER, THIRD)
    holders.append(hold(project, ROUTER))
    holders.append(hold(project, MEMBER, mode="ignore"))  # still answering: it completes its removal as it exits
    code, data = _cli(capsys, "seat", "stop", ROUTER)
    assert code == 0, data
    assert qapool.stopping(project.paths)
    cfg = project.reload_config()
    assert set(cfg.seats) == {ROUTER, MEMBER} and cfg.seats[MEMBER].removing == "graceful"
    assert cfg.seats[ROUTER].removing is None
    assert load_state(project.paths, MEMBER).stop_requested
    assert wait_for(lambda: seats.liveness(project, ROUTER) == "free", timeout=10)


# ---------------------------------------------------------------------- processes

LONG = (("word " * 199 + "word") + "\n\n") * 40  # three parts


def _answer(text, sleep=0.5):
    return {"do": [["kankan", "answer", "--turn", "{turn}", "--file", "{file}"]], "text": text, "sleep": sleep}


def _late_answer(text, delay):
    """An answer posted ``delay`` seconds into the turn: the agent is ``working`` meanwhile."""
    return {"shell": f"sleep {delay}; kankan answer --turn {{turn}} --file {{file}}", "text": text}


CUT_SHORT = {"shell": "KANKAN_FAULT=qa_after_chunk kankan answer --turn {turn} --file {file}", "text": LONG,
             "sleep": 120}  # part 1 posted, then the agent keeps working until it is killed
THINKING = {"sleep": 120}  # a long turn that posts nothing


@pytest.fixture
def chain(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    monkeypatch.setenv("FAKE_AGENT_SCENARIO_DIR", str(scenarios))
    project = bind_project(git_repo, fake_trello, monkeypatch, **FAST)
    add_questions_seed()
    tmux_server.new_session("keep-server", str(tmp_path), ["sleep", "600"])  # the server outlives a full stop
    env = SimpleNamespace(project=project, fake=fake_trello, tmux=tmux_server, tmp=tmp_path, scenarios=scenarios)
    env.qa_list = add_qa_list(project, fake_trello)
    return env


def scenario(env, seat, steps, **extra):
    log = env.tmp / f"agent-{seat}-{time.monotonic_ns()}.log"
    (env.scenarios / f"{seat}.json").write_text(json.dumps(dict(extra, steps=steps, log=str(log))))
    return log


def with_text(env, card, needle):
    return [text for text in env.fake.card_comments(card["id"]) if needle in text]


def title(env, card):
    return env.fake.state["cards"][card["id"]]["name"]


def _polls(env, kind):
    return sum(1 for e in auditlog.read(env.project.paths) if e["event"] == "poll_done" and e.get("kind") == kind)


def wait_polls(env, kind="route", cycles=3, timeout=30):
    start = _polls(env, kind)
    assert wait_for(lambda: _polls(env, kind) >= start + cycles, timeout=timeout), f"no {cycles} {kind} polls"


def pool_sessions(env):
    return [name for name in env.tmux.sessions() if name.startswith("ai-")]


def start_router(env):
    agents_cmd.up(env.project, {}, qa="claude")
    assert seats.liveness(env.project, ROUTER) == "alive"


def start_member_on(env, seat, card):
    """A member opened for ``card`` (registered with its offer, as the router does), started before the router."""
    _register(env.project, seat)
    assert qapool.write_offer(env.project, seat, card["id"])
    agents_cmd.start_member(env.project, seat)


def stop(env, graceful=False):
    return stopall.stop_all(env.project, graceful=graceful, confirm=lambda text: True, say=lambda text: None,
                            poll=0.2)


def _begin(env, seat, steps):
    """``seat`` working on a new card with ``steps``; the router runs in both cases."""
    card = env.fake.add_card(env.qa_list, "question", desc="how does it work?")
    scenario(env, seat, steps)
    if seat != ROUTER:
        start_member_on(env, seat, card)
        assert wait_for(lambda: launcher_state(env.project, seat) == "working", timeout=40)
        scenario(env, ROUTER, [])
    start_router(env)  # its start leaves a running member alone
    assert seat in env.project.reload_config().seats
    assert wait_for(lambda: launcher_state(env.project, seat) == "working", timeout=40)
    return card


@pytest.mark.tmux
@pytest.mark.slow
@pytest.mark.parametrize("seat", [ROUTER, MEMBER])
def test_immediate_stop_during_a_long_answer(chain, seat):
    """F test 13: an immediate stop while ``seat`` posts a long answer (part 1 posted): the answer is completed once,
    for a member as it is removed by the stop, for the router at ``kankan start``; nothing is answered again."""
    env = chain
    card = _begin(env, seat, [CUT_SHORT])
    assert wait_for(lambda: _parts(env.fake, card) == ["1/3"], timeout=40)
    stop(env)
    assert not pool_sessions(env) and qapool.stopping(env.project.paths)
    assert stopall.load_record(env.project.paths)["up"] == "qa=claude"
    if seat == MEMBER:
        assert _parts(env.fake, card) == ["1/3", "2/3", "3/3"]
        assert set(env.project.reload_config().seats) == {ROUTER}
    else:
        assert _parts(env.fake, card) == ["1/3"]
    scenario(env, ROUTER, [_answer("Answered again.")])
    stopall.start_all(env.project)
    assert wait_for(lambda: _parts(env.fake, card) == ["1/3", "2/3", "3/3"], timeout=30)
    wait_polls(env)
    assert _parts(env.fake, card) == ["1/3", "2/3", "3/3"] and not with_text(env, card, "Answered again.")
    assert set(env.project.reload_config().seats) == {ROUTER} and not qapool.stopping(env.project.paths)


@pytest.mark.tmux
@pytest.mark.slow
@pytest.mark.parametrize("seat", [ROUTER, MEMBER])
def test_immediate_stop_while_thinking_answers_once_after_start(chain, seat):
    """F test 18: an immediate stop while ``seat`` thinks, before it posted anything: after ``kankan start`` the
    question is answered once."""
    env = chain
    card = _begin(env, seat, [THINKING])
    stop(env)
    assert not pool_sessions(env) and set(env.project.reload_config().seats) == {ROUTER}
    assert not _answers(env.fake, card)
    scenario(env, ROUTER, [_answer("Answered after start."), _answer("Answered twice.")])
    stopall.start_all(env.project)
    assert wait_for(lambda: with_text(env, card, "Answered after start."), timeout=40)
    wait_polls(env)
    assert len(_answers(env.fake, card)) == 1 and not with_text(env, card, "Answered twice.")


@pytest.mark.tmux
@pytest.mark.slow
def test_stop_and_start_with_three_agents(chain):
    """F test 12: ``kankan stop`` (graceful) with three agents, one answering: every agent stops, the answer is
    completed, the members are removed; ``kankan start`` starts only claude-qa, removes the prefixes, and a card with
    a comment written while stopped is answered once."""
    env = chain
    card_a = env.fake.add_card(env.qa_list, "first", desc="first?")
    scenario(env, ROUTER, [_answer("A answer")])
    scenario(env, MEMBER, [_answer("B answer")])
    scenario(env, THIRD, [_late_answer("C answer", 6)])
    start_router(env)
    assert wait_for(lambda: with_text(env, card_a, "A answer")
                    and launcher_state(env.project, ROUTER) == "qa_waiting", timeout=40)
    card_b = env.fake.add_card(env.qa_list, "second", desc="second?")  # claude-qa is warm: claude-qa2 opens
    assert wait_for(lambda: with_text(env, card_b, "B answer")
                    and launcher_state(env.project, MEMBER) == "qa_waiting", timeout=60)
    card_c = env.fake.add_card(env.qa_list, "third", desc="third?")  # both warm: claude-qa3 opens
    assert wait_for(lambda: launcher_state(env.project, THIRD) == "working", timeout=60)
    assert wait_for(lambda: title(env, card_a).startswith("🟢 "), timeout=20)
    report = stop(env, graceful=True)
    assert report["remaining"] == [] and ROUTER in report["stopped"]
    assert len(with_text(env, card_c, "C answer")) == 1  # finished before its agent stopped
    assert wait_for(lambda: set(env.project.reload_config().seats) == {ROUTER}, timeout=20)
    assert not pool_sessions(env) and qapool.stopping(env.project.paths)
    assert stopall.load_record(env.project.paths)["up"] == "qa=claude"
    assert not any(title(env, card).startswith("🟢") for card in (card_a, card_b, card_c))
    env.fake.user_comment(card_b["id"], "and while you were stopped?")
    scenario(env, ROUTER, [_answer("B follow-up answer"), _answer("answered twice")])
    started = stopall.start_all(env.project)["started"]
    assert len(started) == 1
    assert wait_for(lambda: with_text(env, card_b, "B follow-up answer"), timeout=40)
    wait_polls(env)
    assert len(with_text(env, card_b, "B follow-up answer")) == 1
    assert not any(with_text(env, card, "answered twice") for card in (card_a, card_b, card_c))
    assert title(env, card_a) == "first" and title(env, card_c) == "third"
    assert set(env.project.reload_config().seats) == {ROUTER}


@pytest.mark.tmux
@pytest.mark.slow
def test_router_launcher_restart_leaves_members_their_cards(chain):
    """F test 16: the launcher of claude-qa killed and restarted by its supervisor: claude-qa2 keeps its card and
    answers its follow-up in the same session."""
    env = chain
    card_a = env.fake.add_card(env.qa_list, "first", desc="first?")
    scenario(env, ROUTER, [_answer("A answer")])
    member_log = scenario(env, MEMBER, [_answer("B answer"), _answer("B follow-up answer")])
    start_router(env)
    assert wait_for(lambda: with_text(env, card_a, "A answer")
                    and launcher_state(env.project, ROUTER) == "qa_waiting", timeout=40)
    card_b = env.fake.add_card(env.qa_list, "second", desc="second?")
    assert wait_for(lambda: with_text(env, card_b, "B answer")
                    and launcher_state(env.project, MEMBER) == "qa_waiting", timeout=60)
    before = env.project.refs.read(card_ref(card_b["id"])).meta
    first = proc_info(env.project, ROUTER)["launcher"]["pid"]
    os.kill(first, signal.SIGKILL)
    assert wait_for(lambda: (proc_info(env.project, ROUTER).get("launcher") or {}).get("pid") not in (None, first)
                    and seats.liveness(env.project, ROUTER) == "alive", timeout=40)
    wait_polls(env)
    after = env.project.refs.read(card_ref(card_b["id"])).meta
    assert (after["owner"], after["conversation"]) == (MEMBER, before["conversation"])
    env.fake.user_comment(card_b["id"], "and what about X?")
    assert wait_for(lambda: with_text(env, card_b, "B follow-up answer"), timeout=40)
    assert len([e for e in agent_events(member_log) if e["event"] == "start"]) == 1  # same session
    inputs = agent_inputs(member_log)
    assert "/clear" not in inputs[inputs.index("/clear") + 1:]  # only the clear before its first card
    assert MEMBER in env.project.reload_config().seats


@pytest.mark.tmux
@pytest.mark.slow
def test_stop_while_router_opens_a_member(chain):
    """F test 21: ``kankan stop`` while claude-qa is opening claude-qa2 (its agent still starting): after the stop no
    agent of the pool runs and only claude-qa is registered."""
    env = chain
    env.fake.add_card(env.qa_list, "first", desc="first?")
    scenario(env, ROUTER, [THINKING])
    scenario(env, MEMBER, [], ready_delay=60)
    start_router(env)
    assert wait_for(lambda: launcher_state(env.project, ROUTER) == "working", timeout=40)
    env.fake.add_card(env.qa_list, "second", desc="second?")
    assert wait_for(lambda: MEMBER in env.project.reload_config().seats, timeout=30)
    assert wait_for(lambda: seats.liveness(env.project, MEMBER) == "alive", timeout=30)
    stop(env)
    assert set(env.project.reload_config().seats) == {ROUTER}
    assert not pool_sessions(env)
    time.sleep(3)  # a supervisor would have restarted a launcher by now
    assert not pool_sessions(env) and set(env.project.reload_config().seats) == {ROUTER}
    assert all(seats.liveness(env.project, name) != "alive" for name in (ROUTER, MEMBER))


@pytest.mark.tmux
@pytest.mark.slow
def test_router_restarted_during_stop_stays_stopped(chain):
    """F test 23: the launcher of claude-qa killed during ``kankan stop`` and restarted by its supervisor: the pool
    stays stopped and no agent opens; ``kankan start`` starts it again and the questions are answered once."""
    env = chain
    card_a = env.fake.add_card(env.qa_list, "first", desc="first?")
    scenario(env, ROUTER, [{"sleep": 30}])
    start_router(env)
    assert wait_for(lambda: launcher_state(env.project, ROUTER) == "working", timeout=40)
    reports = []
    stopper = threading.Thread(target=lambda: reports.append(stop(env, graceful=True)))
    stopper.start()
    assert wait_for(lambda: load_state(env.project.paths, ROUTER).stop_requested, timeout=20)
    first = proc_info(env.project, ROUTER)["launcher"]["pid"]
    os.kill(first, signal.SIGKILL)
    stopper.join(timeout=90)
    assert not stopper.is_alive() and reports
    assert qapool.stopping(env.project.paths)
    assert (proc_info(env.project, ROUTER).get("launcher") or {}).get("pid") == first  # never occupied again
    assert any(e["event"] == "launcher_error" and e.get("status") == "config_error"
               for e in auditlog.read(env.project.paths))
    card_b = env.fake.add_card(env.qa_list, "second", desc="second?")
    time.sleep(3)
    assert not pool_sessions(env) and set(env.project.reload_config().seats) == {ROUTER}
    assert seats.liveness(env.project, ROUTER) != "alive"
    scenario(env, ROUTER, [_answer("first answer"), _answer("answered twice")])
    scenario(env, MEMBER, [_answer("second answer"), _answer("answered twice")])  # both cards need an agent at once
    stopall.start_all(env.project)
    assert not qapool.stopping(env.project.paths)
    assert wait_for(lambda: _answers(env.fake, card_a) and _answers(env.fake, card_b), timeout=60)
    wait_polls(env)
    assert len(_answers(env.fake, card_a)) == 1 and len(_answers(env.fake, card_b)) == 1
    assert not any(with_text(env, card, "answered twice") for card in (card_a, card_b))


def test_stop_record_keeps_the_router_dying_between_the_two_inventories(kproject, fake_trello, monkeypatch):
    """The second inventory (after the stopping mark) only adds seats to stop: a router that died in between still
    gives the record its ``qa=<type>`` (final review, Codex #4)."""
    from kankan import stopall
    from kankan.config import SeatConfig, load_config, save_config
    from tests.helpers import NoTmux
    cfg = load_config(kproject.paths)
    cfg.seats["claude-qa"] = SeatConfig(type="claude", role="qa")
    save_config(kproject.paths, cfg)
    first = stopall.Inventory(liveness={"claude-qa": "alive"}, sessions=[], unit=False, alive=["claude-qa"])
    second = stopall.Inventory(liveness={"claude-qa": "dead"}, sessions=[], unit=False, alive=[])
    inventories = [first, second]
    monkeypatch.setattr(stopall, "inventory", lambda project, tmux: inventories.pop(0) if inventories else second)
    monkeypatch.setattr(stopall, "_quit_now", lambda project, tmux, alive, report: None)
    stopall.stop_all(kproject, confirm=lambda text: True, say=lambda text: None, tmux=NoTmux())
    assert stopall.load_record(kproject.paths)["up"] == "qa=claude"
