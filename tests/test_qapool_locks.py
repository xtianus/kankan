"""Lock order of the Q&A pool (qapool T§4): concurrent member registration, answer completion and member removal
finish without a deadlock, and ``seatremove.complete`` is never entered with the Q&A lifecycle lock or the pool lock
held, on every path that removes members (router start, routing rounds closing members, ``kankan stop``, type change,
``kankan seat remove``)."""

import subprocess
import threading
import time

import pytest

from kankan import agents_cmd, config, qa, qa_answer, qapool, seatremove, seats, stopall
from kankan.errors import KankanError
from kankan.project import Project
from tests.flow import agent_ctx
from tests.helpers import NoTmux, add_qa_list, add_questions_seed, hold
from tests.test_qapool_start import FakeTmux
from tests.test_qapool_stop import MEMBER, ROUTER, THIRD, _parts, _register
from tests.test_seatremove_qa_pending import _conversation, _cut_short, long_text


@pytest.fixture(autouse=True)
def private_tmux(monkeypatch, tmux_server):
    """Whatever a command does with tmux goes to the test's private server, never to the user's."""
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)


@pytest.fixture
def qa_list(kproject, fake_trello):
    add_questions_seed()
    return add_qa_list(kproject, fake_trello)


def _held_here(path) -> bool:
    entry = config._LOCK_DEPTH.get(str(path))
    return entry is not None and entry[1] == threading.get_ident()


@pytest.fixture
def guarded(kproject, monkeypatch, tmux_server):
    """Every ``seatremove.complete`` (also the router's background ``pool-remove``, run in-process here) records
    whether the lifecycle lock or the pool lock was held by the calling thread."""
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)  # never the user's tmux server
    paths = kproject.paths
    locks = {"lifecycle": paths.kankan_dir / "qa-lifecycle.lock", "pool": paths.kankan_dir / "qa-pool.lock"}
    calls, violations = [], []
    real_complete = seatremove.complete

    def complete(project, seat):
        calls.append(seat)
        held = [name for name, path in locks.items() if _held_here(path)]
        if held:
            violations.append((seat, held))
        return real_complete(project, seat)
    monkeypatch.setattr(seatremove, "complete", complete)
    real_popen = subprocess.Popen

    class Done:
        pid = 0

        def poll(self):
            return 0

    def popen(argv, *args, **kwargs):
        if "pool-remove" in argv:  # the router's background removal of a closed member
            held = [name for name, path in locks.items() if _held_here(path)]
            if held:
                violations.append((argv[-1], held))
            qapool.remove_member_main(kwargs.get("cwd") or str(paths.repo), argv[-1])
            return Done()
        return real_popen(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls, violations


def test_router_start_removes_members_without_pool_locks(kproject, fake_trello, monkeypatch, qa_list, guarded):
    project, fake = kproject, fake_trello
    calls, violations = guarded
    _register(project, ROUTER)
    env = _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER)
    lease = seats.acquire_qa(project, "claude", None)
    try:
        qapool.router_start(project, lease)
    finally:
        seats.release(project, lease)
    assert calls == [MEMBER] and violations == []
    assert _parts(fake, env.card) == ["1/3", "2/3", "3/3"]


def test_routing_round_closings_remove_members_without_pool_locks(kproject, fake_trello, monkeypatch, qa_list,
                                                                  guarded):
    """A round closes a gone member whose answer was cut short and a member whose card left Q&A; their removal runs
    after the round's locks."""
    project, fake = kproject, fake_trello
    calls, violations = guarded
    _register(project, ROUTER)
    gone = _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER, title="gone")
    ended = _cut_short(project, fake, monkeypatch, qa_list, seat=THIRD, title="ended")
    fake.user_move(ended.card["id"], project.cfg.done)
    monkeypatch.setattr(agents_cmd, "start_member", lambda project_, name, tmux=None: "ai-proj-claude-starting-x")
    router = qapool.Router(project, ROUTER, lambda *args, **fields: None)
    for _ in range(3):  # a member opened meanwhile for a card needing an agent is never started here
        router.round()
        if not {MEMBER, THIRD} & set(qapool.members(project.reload_config())):
            break
    assert {MEMBER, THIRD} <= set(calls) and violations == []
    assert not {MEMBER, THIRD} & set(qapool.members(project.reload_config()))
    assert _parts(fake, gone.card) == ["1/3", "2/3", "3/3"]


def test_stop_removes_members_without_pool_locks(kproject, fake_trello, monkeypatch, qa_list, holders, guarded):
    project, fake = kproject, fake_trello
    calls, violations = guarded
    _register(project, ROUTER)
    holders.append(hold(project, ROUTER))
    _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER)
    stopall.stop_all(project, confirm=lambda text: True, say=lambda text: None, tmux=NoTmux(), poll=0.05)
    assert calls == [MEMBER] and violations == []


def test_type_change_removes_the_pool_without_pool_locks(kproject, fake_trello, monkeypatch, qa_list, holders,
                                                         guarded):
    project, fake = kproject, fake_trello
    calls, violations = guarded
    _register(project, ROUTER)
    holders.append(hold(project, ROUTER))
    _cut_short(project, fake, monkeypatch, qa_list, seat=MEMBER)
    agents_cmd.agent(project, "codex", tmux=FakeTmux(project), preflight=False, qa=True)
    assert calls == [MEMBER, ROUTER] and violations == []


def test_concurrent_registration_completion_and_removal_finish(kproject, fake_trello, monkeypatch, qa_list):
    """Member registrations (the router's choice under the lifecycle and pool locks), answers posted by the router's
    agent (conversation lock, project lock, answer-record lock) and member removals that complete cut-short answers
    (removal lock, conversation lock, project lock, then lifecycle lock), all at once from three threads with their
    own project objects: every thread finishes, without a lock timeout."""
    project, fake = kproject, fake_trello
    _register(project, ROUTER)
    cut = [_cut_short(project, fake, monkeypatch, qa_list, seat=f"claude-qa{n}", title=f"cut {n}") for n in (5, 6, 7)]
    router_conv = _conversation(project, fake, monkeypatch, qa_list, seat=ROUTER, title="router")
    card = fake.add_card(qa_list, "waiting", desc="next?")
    monkeypatch.setattr(qapool, "choose", lambda needing, views, **kw: qapool.Decision(openings=[card["id"]]))
    errors, done = [], set()
    registering = threading.Event()

    def register():
        p = Project.open(project.paths.repo)
        router = qapool.Router(p, ROUTER, lambda *args, **fields: None)
        for _ in range(6):
            router.decide([(card["id"], "0")], [])
            time.sleep(0.01)
        registering.set()

    def answer():
        ctx = router_conv.ctx
        token = router_conv.token
        for n in range(4):
            qa_answer.answer(ctx, long_text(2) if n % 2 else "short answer", token)
            p = Project.open(project.paths.repo)
            task = seats_task(p)
            token = qa.start_turn(p, router_conv.lease, task, qa.Inputs([], []))

    def remove():
        p = Project.open(project.paths.repo)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            left = qapool.members(p.reload_config())
            if not left and registering.is_set():
                return
            for name in left:
                seatremove.complete(p, name)

    def run(name, target):
        try:
            target()
        except BaseException as err:  # noqa: BLE001 - reported below
            errors.append((name, repr(err)))
        finally:
            done.add(name)

    threads = [threading.Thread(target=run, args=(name, target), daemon=True)
               for name, target in (("register", register), ("answer", answer), ("remove", remove))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=50)  # well under the 60 s lock timeouts: a deadlock shows as a thread still running
    assert done == {"register", "answer", "remove"}, f"still running: {set(['register', 'answer', 'remove']) - done}"
    assert errors == []
    assert qapool.members(project.reload_config()) == []
    for item in cut:
        assert _parts(fake, item.card) == ["1/3", "2/3", "3/3"]
    seats.release(project, router_conv.lease)


def seats_task(project):
    from kankan.seatstate import load_state
    return load_state(project.paths, ROUTER).task
