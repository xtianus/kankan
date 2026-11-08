import multiprocessing
import os
import subprocess
import sys
import time

import pytest

from kankan import auditlog, procutil, seats
from kankan.config import load_config
from kankan.errors import KankanError, Status
from kankan.project import Project
from kankan.refstore import Create, card_ref, seat_ref
from kankan.seatstate import load_state, update_state
from tests.fakes.fake_trello import FakeTrello
from tests.helpers import bind_project


@pytest.fixture
def fake():
    with FakeTrello() as server:
        yield server


@pytest.fixture
def project(git_repo, fake, monkeypatch):
    monkeypatch.setattr(seats, "KILL_GRACE", 0.5)
    return bind_project(git_repo, fake, monkeypatch)


def _acquire_worker(repo, barrier, queue, hold):
    seats.pane_sid = lambda: None
    project = Project.open(repo)
    barrier.wait()
    try:
        lease = seats.acquire(project, "claude")
        queue.put(lease.seat)
        hold.wait(30)
    except KankanError as err:
        queue.put(err.status.value + ":" + err.message)


def test_parallel_acquire_no_duplicates(project, fake, git_repo):
    ctx = multiprocessing.get_context("fork")
    barrier, queue, hold = ctx.Barrier(10), ctx.Queue(), ctx.Event()
    procs = [ctx.Process(target=_acquire_worker, args=(git_repo, barrier, queue, hold)) for _ in range(10)]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=60) for _ in procs]
    hold.set()
    for proc in procs:
        proc.join()
    seats_taken = [r for r in results if not r.startswith("config_error")]
    errors = [r for r in results if r.startswith("config_error")]
    assert sorted(seats_taken) == ["claude-1", "claude-2", "claude-3", "claude-4"]
    assert len(errors) == 6 and all("cap" in e for e in errors)
    seat_lists = [l for l in fake.state["lists"].values() if l["name"].startswith("claude-")]
    assert len(seat_lists) == 12
    assert len(load_config(project.paths).seats) == 4


def test_live_seat_never_recovered(project):
    lease = seats.acquire(project, "claude")
    assert seats.liveness(project, "claude-1") == "alive"
    with pytest.raises(KankanError):
        seats.acquire(project, "claude", seat="claude-1")
    assert project.refs.read(seat_ref("claude-1")).oid == lease.seat_oid
    assert seats.acquire(project, "claude").seat == "claude-2"


def test_release_frees_seat(project):
    lease = seats.acquire(project, "claude")
    seats.release(project, lease)
    assert seats.liveness(project, "claude-1") == "free"
    assert seats.acquire(project, "claude").seat == "claude-1"


def _simulate_death(project, lease, **proc):
    """The holder dies: the flock goes away but the seat ref stays."""
    os.close(lease.fd)
    if proc:
        seats.write_proc(project, lease.seat, reset=True, **proc)


def test_zombie_session_not_owner(project):
    lease = seats.acquire(project, "claude")
    seats.verify_owner(project, "claude-1", lease.session)
    _simulate_death(project, lease)
    assert seats.liveness(project, "claude-1") == "dead"
    new = seats.acquire(project, "claude", seat="claude-1")
    with pytest.raises(KankanError) as exc:
        seats.verify_owner(project, "claude-1", lease.session)
    assert exc.value.status == Status.NOT_OWNER
    seats.verify_owner(project, "claude-1", new.session)


def test_boot_id_change_recovers(project):
    lease = seats.acquire(project, "claude")
    os.close(lease.fd)
    proc_file = project.paths.seats_dir / "claude-1.proc.json"
    data = __import__("json").loads(proc_file.read_text())
    data["boot_id"] = "another-boot"
    data["sid"] = 999999
    proc_file.write_text(__import__("json").dumps(data))
    seats.acquire(project, "claude", seat="claude-1")
    events = [e["event"] for e in auditlog.read(project.paths)]
    assert "seat_reboot" in events and "seat_recovered" in events


HELPER = """
import signal, subprocess, sys, time
subprocess.Popen(['sleep', '1000'])
subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(1000)'])
print('ready', flush=True)
time.sleep(1000)
"""


def test_orphan_child_killed_before_reuse(project):
    lease = seats.acquire(project, "claude")
    old = subprocess.Popen([sys.executable, "-c", HELPER], start_new_session=True, stdout=subprocess.PIPE, text=True)
    assert old.stdout.readline().strip() == "ready"
    _simulate_death(project, lease, sid=old.pid, agent=old.pid)
    assert len(procutil.pids_in_session(old.pid)) == 3
    seats.acquire(project, "claude", seat="claude-1")
    old.wait(5)
    assert procutil.pids_in_session(old.pid) == []


def test_setsid_escapee_documented(project):
    lease = seats.acquire(project, "claude")
    worktree = project.paths.seat_worktree("claude-1")
    escapee = subprocess.Popen(["sleep", "1000"], cwd=worktree, start_new_session=True)
    try:
        _simulate_death(project, lease, sid=999999)
        seats.acquire(project, "claude", seat="claude-1")
        assert escapee.poll() is None
        events = [e for e in auditlog.read(project.paths) if e["event"] == "seat_escapees"]
        assert events and escapee.pid in events[0]["pids"]
    finally:
        escapee.kill()
        escapee.wait()


RECOVER_FROM_CHILD = """
import os, sys
sys.path.insert(0, {root!r})
from kankan import seats
from kankan.project import Project
seats.KILL_GRACE = 0.5
project = Project.open({repo!r})
seats.pane_sid = lambda: os.getsid(0)\nseats.write_proc(project, 'claude-1', reset=True, sid=os.getsid(0))
lease = seats.acquire(project, 'claude', seat='claude-1')
print('acquired', lease.seat, flush=True)
"""


def test_recovery_excludes_own_ancestors(project, git_repo):
    lease = seats.acquire(project, "claude")
    os.close(lease.fd)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child = RECOVER_FROM_CHILD.format(root=root, repo=str(git_repo))
    parent_script = ("import subprocess, sys, time\n"
                     "subprocess.Popen(['sleep', '1000'])\n"
                     f"r = subprocess.run([sys.executable, '-c', {child!r}], capture_output=True, text=True)\n"
                     "print(r.stdout.strip() or r.stderr[-500:], flush=True)\n"
                     "time.sleep(1000)\n")
    parent = subprocess.Popen([sys.executable, "-c", parent_script], start_new_session=True, stdout=subprocess.PIPE,
                              text=True)
    try:
        assert parent.stdout.readline().strip() == "acquired claude-1"
        assert parent.poll() is None
        members = procutil.pids_in_session(parent.pid)
        assert members == [parent.pid]
    finally:
        parent.kill()
        parent.wait()


def _state_worker(repo, field):
    project = Project.open(repo)
    for _ in range(50):
        def bump(state):
            state.convergence_failures[field] = state.convergence_failures.get(field, 0) + 1
        update_state(project.paths, "claude-1", bump)


def test_concurrent_state_updates_not_lost(project, git_repo):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_state_worker, args=(git_repo, f"p{i}")) for i in range(3)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
    assert load_state(project.paths, "claude-1").convergence_failures == {"p0": 50, "p1": 50, "p2": 50}


def test_survivor_marks_unavailable(project, monkeypatch):
    lease = seats.acquire(project, "claude")
    _simulate_death(project, lease, sid=999999)
    monkeypatch.setattr(procutil, "terminate_session", lambda sid, exclude, grace=5.0: [os.getpid()])
    with pytest.raises(KankanError):
        seats.acquire(project, "claude", seat="claude-1")
    assert seats.liveness(project, "claude-1") == "unavailable"
    assert project.refs.read(seat_ref("claude-1")).oid == lease.seat_oid


def test_flock_fd_not_inherited(project):
    lease = seats.acquire(project, "claude")
    assert os.get_inheritable(lease.fd) is False


def test_interrupted_seat_creation_completes(project, fake, monkeypatch):
    from kankan import faults
    monkeypatch.setenv("KANKAN_FAULT", "after_seat_lists")
    with pytest.raises(faults.SimulatedCrash):
        seats.ensure_created(project, "claude-1", "claude")
    monkeypatch.delenv("KANKAN_FAULT")
    seats.ensure_created(project, "claude-1", "claude")
    names = sorted(l["name"] for l in fake.state["lists"].values() if l["name"].startswith("claude-1"))
    assert names == ["claude-1 · Implemented", "claude-1 · In Progress", "claude-1 · Review"]
    assert list(load_config(project.paths).seats) == ["claude-1"]
    assert (project.paths.seat_worktree("claude-1") / ".git").exists()


def test_prefers_free_seat_owning_cards(project):
    one, two = seats.acquire(project, "claude"), seats.acquire(project, "claude")
    seats.release(project, one)
    seats.release(project, two)
    oid = project.refs.write_blob({"schema": 1, "phase": "impl", "owner": "claude-2", "card": "a" * 24})
    project.refs.create(card_ref("a" * 24), oid)
    assert seats.acquire(project, "claude").seat == "claude-2"


def test_agent_env_missing():
    with pytest.raises(KankanError) as exc:
        seats.AgentEnv.from_environ()
    assert exc.value.status == Status.USAGE_ERROR


def test_pane_sid_only_for_supervisor_led_session(monkeypatch):
    monkeypatch.delenv(seats.SUPERVISOR_ENV, raising=False)
    assert seats.pane_sid() is None
    monkeypatch.setenv(seats.SUPERVISOR_ENV, str(os.getpid()))
    if os.getsid(0) != os.getpid():
        assert seats.pane_sid() is None
    monkeypatch.setenv(seats.SUPERVISOR_ENV, str(os.getsid(0)))
    assert seats.pane_sid() == os.getsid(0)


def test_reused_sid_not_killed(project):
    lease = seats.acquire(project, "claude")
    other = subprocess.Popen(["sleep", "1000"], start_new_session=True)
    try:
        os.close(lease.fd)
        import json
        proc_file = project.paths.seats_dir / "claude-1.proc.json"
        data = json.loads(proc_file.read_text())
        data.update(sid=other.pid, sid_start=1)  # recorded leader had another start time
        proc_file.write_text(json.dumps(data))
        seats.acquire(project, "claude", seat="claude-1")
        assert other.poll() is None
        assert any(e["event"] == "sid_reused" for e in auditlog.read(project.paths))
    finally:
        other.kill()
        other.wait()


def _hold_project_lock(repo, ready, seconds):
    from kankan.config import Paths, project_lock
    with project_lock(Paths.discover(repo)):
        ready.set()
        time.sleep(seconds)


def test_liveness_serialized_with_occupation(project, git_repo):
    """A liveness probe must not grab a seat flock while an occupation holds the project lock."""
    seats.release(project, seats.acquire(project, "claude"))
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    holder = ctx.Process(target=_hold_project_lock, args=(git_repo, ready, 1.0))
    holder.start()
    ready.wait(10)
    start = time.monotonic()
    assert seats.liveness(project, "claude-1") == "free"
    assert time.monotonic() - start >= 0.7
    holder.join()
