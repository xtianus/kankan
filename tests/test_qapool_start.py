"""The member start path and the stopping mark (qapool T§2 "Starting a member", T§9 "Stopping mark", F§7, F test 23):
``agents_cmd.start_member`` starts a registered member under its own name, its supervisor keeps that name, its
launcher is refused once it is marked for removal or the pool is stopping, and the start commands clear the mark."""

import json
import os
import signal
import time
from types import SimpleNamespace

import pytest

from kankan import agents_cmd, auditlog, cli, messages, qapool, seats, stopall
from kankan.config import SeatConfig, atomic_write_json, load_config, save_config
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref, seat_ref
from tests.helpers import add_qa_list, add_questions_seed, bind_project
from tests.launcher_helpers import (agent_events, launcher_state, proc_info, seat_session, start_qa_agent,
                                    wait_for)

MEMBER = "claude-qa2"
ROUTER = "claude-qa"
FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=0.05, qa_poll_seconds=1)


def _register_pool(project):
    cfg = load_config(project.paths)
    cfg.seats[ROUTER] = SeatConfig(type="claude", role="qa")
    cfg.seats[MEMBER] = SeatConfig(type="claude", role="qa")
    save_config(project.paths, cfg)
    project.reload_config()


def _mark(project, seat, mode="graceful"):
    cfg = load_config(project.paths)
    cfg.seats[seat].removing = mode
    save_config(project.paths, cfg)
    project.reload_config()


# ---------------------------------------------------------------------- unit: refusals

@pytest.fixture
def pool(kproject, fake_trello):
    add_qa_list(kproject, fake_trello)
    _register_pool(kproject)
    return kproject


def test_member_acquires_its_own_seat(pool):
    lease = seats.acquire_qa(pool, "claude", None, seat=MEMBER)
    try:
        assert lease.seat == MEMBER and pool.refs.read(seat_ref(MEMBER)).oid == lease.seat_oid
        assert (pool.paths.seat_worktree(MEMBER) / ".git").exists()
        assert pool.refs.read(seat_ref(ROUTER)) is None
        assert set(pool.reload_config().seats) == {ROUTER, MEMBER}
    finally:
        seats.release(pool, lease)
    assert seats.acquire(pool, "claude", seat=MEMBER).seat == MEMBER  # the by-name path goes through acquire_qa


@pytest.mark.parametrize("mode", ["graceful", "immediate"])
def test_member_refused_after_removal(pool, mode):
    _mark(pool, MEMBER, mode)
    with pytest.raises(KankanError) as exc:
        seats.acquire_qa(pool, "claude", None, seat=MEMBER)
    assert exc.value.status == Status.CONFIG_ERROR and ROUTER in exc.value.message
    assert pool.refs.read(seat_ref(MEMBER)) is None


def test_unregistered_member_refused_and_never_registered(pool):
    for create in (True, False):
        with pytest.raises(KankanError) as exc:
            seats.acquire_qa(pool, "claude", None, create=create, seat="claude-qa3")
        assert exc.value.status == Status.CONFIG_ERROR and ROUTER in exc.value.message
    assert "claude-qa3" not in pool.reload_config().seats
    with pytest.raises(KankanError) as exc:
        seats.ensure_created(pool, "claude-qa3", "claude")
    assert "claude-qa3" not in pool.reload_config().seats


def test_member_refused_while_stopping(pool):
    qapool.set_stopping(pool, True)
    assert qapool.stopping(pool.paths)
    with pytest.raises(KankanError) as exc:
        seats.acquire_qa(pool, "claude", None, seat=MEMBER)
    assert exc.value.status == Status.CONFIG_ERROR and exc.value.message == messages.QA_POOL_STOPPING.format(
        router=ROUTER)
    qapool.set_stopping(pool, False)
    seats.release(pool, seats.acquire_qa(pool, "claude", None, seat=MEMBER))


def test_router_refused_while_stopping_unit(pool):
    qapool.set_stopping(pool, True)
    with pytest.raises(KankanError) as exc:
        seats.acquire_qa(pool, "claude", None)
    assert exc.value.status == Status.CONFIG_ERROR  # the supervisor treats it as fatal (exit code 3)
    assert pool.refs.read(seat_ref(ROUTER)) is None
    qapool.clear_for_start(pool)
    assert not qapool.stopping(pool.paths)
    seats.release(pool, seats.acquire_qa(pool, "claude", None))


def test_stopping_mark_file(pool):
    path = pool.paths.kankan_dir / "qa-pool.json"
    assert not qapool.stopping(pool.paths) and not path.exists()
    qapool.set_stopping(pool, True)
    assert json.loads(path.read_text()) == {"stopping": True}
    qapool.clear_for_start(pool)
    assert json.loads(path.read_text()) == {"stopping": False}


def test_agent_refuses_member_name(pool):
    with pytest.raises(KankanError) as exc:
        agents_cmd.agent(pool, "claude", seat=MEMBER, preflight=False, create=False)
    assert exc.value.status == Status.USAGE_ERROR and ROUTER in exc.value.message


# ---------------------------------------------------------------------- unit: start commands clear the mark

class FakeTmux:
    def __init__(self, project):
        self.project = project
        self.seen = []

    def has_session(self, name):
        return False

    def sessions_with_option(self, option):
        return {}

    def new_session(self, name, cwd, argv, env=None):
        self.seen.append(qapool.stopping(self.project.paths))

    def set_option(self, *args):
        pass


def test_start_commands_clear_mark(pool, monkeypatch, capsys):
    seen = []

    def stub_agent(project, agent_type, seat=None, attach=False, tmux=None, preflight=True, qa=False, create=True):
        seen.append(qapool.stopping(project.paths))
        return "ai-proj-claude-starting-abc"

    add_questions_seed()
    # `kankan agent claude --qa`
    qapool.set_stopping(pool, True)
    monkeypatch.setattr(agents_cmd, "_register_qa_seat", lambda project, agent_type, change=True: None)
    fake = FakeTmux(pool)
    agents_cmd.agent(pool, "claude", tmux=fake, preflight=False, qa=True)
    assert fake.seen == [False]
    # a restart of a seat being removed (create=False) does not clear it
    qapool.set_stopping(pool, True)
    agents_cmd.agent(pool, "claude", tmux=FakeTmux(pool), preflight=False, qa=True, create=False)
    assert qapool.stopping(pool.paths)

    # `kankan up qa=claude`
    monkeypatch.setattr(agents_cmd, "agent", stub_agent)
    monkeypatch.setattr(agents_cmd, "resume_removals", lambda project, tmux=None: None)
    monkeypatch.setattr(agents_cmd, "_wait_started", lambda project, session, what, started: None)
    agents_cmd.up(pool, {}, tmux=FakeTmux(pool), qa="claude")
    assert seen == [False]

    # `kankan seat start claude-qa`
    qapool.set_stopping(pool, True)
    from kankan import seatremove
    monkeypatch.setattr(seatremove, "await_start", lambda project, seat, session: None)
    monkeypatch.setattr("kankan.tmux.Tmux.has_session", lambda self, name: False)
    monkeypatch.chdir(pool.paths.repo)
    assert cli.main(["seat", "start", ROUTER, "--json"]) == 0
    capsys.readouterr()
    assert seen == [False, False]

    # `kankan start`
    qapool.set_stopping(pool, True)
    atomic_write_json(stopall.record_path(pool.paths), {"up": "qa=claude"})
    calls = []
    monkeypatch.setattr(agents_cmd, "up", lambda project, wanted, qa=None: calls.append(
        qapool.stopping(project.paths)) or [])
    stopall.start_all(pool)
    assert calls == [False]


def test_up_with_router_running_leaves_mark(pool, monkeypatch):
    """A no-op `kankan up` (router alive) starts nothing and does not clear the mark."""
    monkeypatch.setattr(agents_cmd, "resume_removals", lambda project, tmux=None: None)
    monkeypatch.setattr(seats, "liveness", lambda project, seat: "alive")
    qapool.set_stopping(pool, True)
    assert agents_cmd.up(pool, {}, tmux=FakeTmux(pool), qa="claude") == []
    assert qapool.stopping(pool.paths)


# ---------------------------------------------------------------------- processes

@pytest.fixture
def chain(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    monkeypatch.setenv("FAKE_AGENT_SCENARIO_DIR", str(scenarios))
    project = bind_project(git_repo, fake_trello, monkeypatch, **FAST)
    add_questions_seed()
    env = SimpleNamespace(project=project, fake=fake_trello, tmux=tmux_server, tmp=tmp_path, scenarios=scenarios)
    env.qa_list = add_qa_list(project, fake_trello)
    _register_pool(project)
    return env


def _scenario(env, seat, steps):
    log = env.tmp / f"agent-{seat}.log"
    (env.scenarios / f"{seat}.json").write_text(json.dumps({"steps": steps, "log": str(log)}))
    return log


def _answer(text):
    return {"do": [["kankan", "answer", "--turn", "{turn}", "--file", "{file}"]], "text": text}


def _launcher_pid(project, seat):
    return (proc_info(project, seat).get("launcher") or {}).get("pid")


@pytest.mark.tmux
@pytest.mark.slow
def test_member_started_reaches_qalauncher_under_its_name(chain):
    env = chain
    log = _scenario(env, MEMBER, [_answer("Member answer.")])
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    assert qapool.write_offer(env.project, MEMBER, card["id"])  # a member takes only the cards offered to it
    began = time.monotonic()
    session = agents_cmd.start_member(env.project, MEMBER)
    assert time.monotonic() - began < 10  # non-blocking: never waits for the launcher
    assert session.startswith("ai-proj-claude-starting-")
    assert wait_for(lambda: any("Member answer." in text for text in env.fake.card_comments(card["id"])), timeout=40)
    meta = env.project.refs.read(card_ref(card["id"])).meta
    assert meta["owner"] == MEMBER and meta["phase"] == "qa"  # a Q&A conversation: QaLauncher runs it
    assert wait_for(lambda: launcher_state(env.project, MEMBER) == "qa_waiting", timeout=10)
    assert seat_session(env.project, MEMBER) in env.tmux.sessions()
    assert any(e["event"] == "start" and e["seat"] == MEMBER for e in agent_events(log))
    assert set(env.project.reload_config().seats) == {ROUTER, MEMBER}  # registers nothing
    assert env.project.refs.read(seat_ref(ROUTER)) is None


@pytest.mark.tmux
@pytest.mark.slow
def test_member_survives_launcher_restart_under_name(chain):
    env = chain
    _scenario(env, MEMBER, [])
    agents_cmd.start_member(env.project, MEMBER)
    assert wait_for(lambda: launcher_state(env.project, MEMBER) == "idle", timeout=40)
    first = _launcher_pid(env.project, MEMBER)
    supervisor = proc_info(env.project, MEMBER)["supervisor"]["pid"]
    os.kill(first, signal.SIGKILL)
    assert wait_for(lambda: _launcher_pid(env.project, MEMBER) not in (None, first), timeout=40)
    assert wait_for(lambda: launcher_state(env.project, MEMBER) == "idle", timeout=40)
    assert proc_info(env.project, MEMBER)["supervisor"]["pid"] == supervisor
    assert seats.liveness(env.project, MEMBER) == "alive"
    assert seat_session(env.project, MEMBER) in env.tmux.sessions()
    assert env.project.refs.read(seat_ref(ROUTER)) is None
    assert set(env.project.reload_config().seats) == {ROUTER, MEMBER}


@pytest.mark.tmux
@pytest.mark.slow
def test_member_refused_after_removal_by_its_supervisor_is_gone(chain):
    """A member marked for removal is not brought back by its supervisor: the restarted launcher is refused and exits
    cleanly (an expected refusal of the pool), its supervisor ends with it and the member counts as gone."""
    env = chain
    _scenario(env, MEMBER, [])
    agents_cmd.start_member(env.project, MEMBER)
    assert wait_for(lambda: launcher_state(env.project, MEMBER) == "idle", timeout=40)
    _mark(env.project, MEMBER)
    first = _launcher_pid(env.project, MEMBER)
    os.kill(first, signal.SIGKILL)
    assert wait_for(lambda: not seats.supervisor_running(env.project, MEMBER), timeout=40)
    assert wait_for(lambda: seat_session(env.project, MEMBER) not in env.tmux.sessions(), timeout=20)
    assert _launcher_pid(env.project, MEMBER) == first  # never occupied again
    assert seats.liveness(env.project, MEMBER) == "dead"
    assert qapool.gone(env.project, MEMBER)


@pytest.mark.tmux
@pytest.mark.slow
def test_router_refused_while_stopping(chain):
    """F test 23: a router launcher (a supervisor restart during a stop) never starts while the pool is stopping."""
    env = chain
    qapool.set_stopping(env.project, True)
    start_qa_agent(env.tmux, env.project, scenario={"steps": []}, tmp_path=env.tmp)
    assert wait_for(lambda: any(e["event"] == "launcher_error" and e.get("status") == "config_error"
                                for e in auditlog.read(env.project.paths)), timeout=40)
    assert seats.liveness(env.project, ROUTER) == "free"
    assert env.project.refs.read(seat_ref(ROUTER)) is None


def test_qa_launcher_refused_while_stopping_before_any_trello_read(kproject, fake_trello):
    """A stopped pool refuses a Q&A launcher on its local mark alone, before the startup board read, so a supervisor
    restart is refused at once even with Trello unreachable (final review, Codex #5)."""
    from kankan.adapters import load_adapter
    from kankan.qa_launcher import QaLauncher
    qapool.set_stopping(kproject, True)
    requests = len(fake_trello.requests)
    launcher = QaLauncher(kproject, load_adapter(kproject.paths, "claude", read_only=True), None, "claude", ROUTER)
    with pytest.raises(KankanError) as exc:
        launcher.start()
    assert exc.value.data == seats.POOL_REFUSED and len(fake_trello.requests) == requests


def test_stopping_mark_is_a_stop_request_for_q_and_a_launchers(kproject, fake_trello):
    """The pool's stopping mark makes every Q&A launcher honour a stop between turns, also when a ``seat stop`` came
    during the router's start and its ``stop_requested`` was cleared by ``Launcher.start`` (final review, Codex #3)."""
    from kankan.adapters import load_adapter
    from kankan.qa_launcher import QaLauncher
    from kankan.seats import Lease
    launcher = QaLauncher(kproject, load_adapter(kproject.paths, "claude", read_only=True), None, "claude", ROUTER)
    launcher.lease = Lease(seat=ROUTER, type="claude", session="s" * 32, seat_oid="0" * 40, fd=-1)
    assert not launcher.stop_requested()
    qapool.set_stopping(kproject, True)
    assert launcher.stop_requested()
