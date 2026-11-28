"""Gone members (qapool T§5 step 2, F test 22): a member is gone when its seat is ``dead`` and its supervisor no longer
runs or gave up, or ``free`` past the startup deadline of its opening; the supervisor records ``gave_up``."""

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from kankan import qapool, seats, supervisor
from kankan.config import SeatConfig, atomic_write_json, load_config, save_config
from tests.helpers import add_qa_list
from tests.launcher_helpers import proc_info

MEMBER = "claude-qa2"


@pytest.fixture
def pool(kproject, fake_trello):
    add_qa_list(kproject, fake_trello)
    cfg = load_config(kproject.paths)
    cfg.seats["claude-qa"] = SeatConfig(type="claude", role="qa")
    cfg.seats[MEMBER] = SeatConfig(type="claude", role="qa")
    save_config(kproject.paths, cfg)
    kproject.reload_config()
    return kproject


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield proc
    proc.kill()
    proc.wait()


def _dead(project, supervisor_pid=None):
    """The member's launcher crashed: its seat ref stays, its liveness lock is free."""
    lease = seats.acquire_qa(project, "claude", None, seat=MEMBER)
    seats._release_fd(lease.fd)
    seats.write_proc(project, MEMBER, reset=True, supervisor=supervisor_pid)
    assert seats.liveness(project, MEMBER) == "dead"


def test_dead_during_supervisor_restart_not_gone(pool, sleeper):
    _dead(pool, sleeper.pid)
    assert seats.supervisor_running(pool, MEMBER)
    assert not qapool.gone(pool, MEMBER)
    assert not qapool.mark_gone(pool, MEMBER)
    assert pool.reload_config().seats[MEMBER].removing is None


def test_dead_with_supervisor_gone_is_gone(pool, sleeper):
    _dead(pool, sleeper.pid)
    sleeper.kill()
    sleeper.wait()
    assert not seats.supervisor_running(pool, MEMBER)
    assert qapool.gone(pool, MEMBER)
    assert qapool.mark_gone(pool, MEMBER)
    assert pool.reload_config().seats[MEMBER].removing == "graceful"
    assert not qapool.mark_gone(pool, MEMBER)  # already marked: never rewritten


def test_supervisor_gave_up_is_gone(pool, monkeypatch):
    """The real supervisor loop gives up after MAX_RESTARTS: it records ``gave_up`` while it stays alive."""
    _dead(pool, None)
    seats.write_proc(pool, MEMBER, supervisor=os.getpid())  # this process plays the supervisor
    assert not qapool.gone(pool, MEMBER)

    class Child:
        def wait(self):
            return 1

    class GaveUp(Exception):
        pass

    def sleep(seconds):
        if seconds >= 3600:
            raise GaveUp
    monkeypatch.setattr(supervisor, "subprocess", SimpleNamespace(Popen=lambda *a, **k: Child()))
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(sleep=sleep, monotonic=time.monotonic))
    with pytest.raises(GaveUp):
        supervisor.run(str(pool.paths.repo), "claude", MEMBER)
    record = proc_info(pool, MEMBER)
    assert record["gave_up"] is True and record["gave_up_at"]
    assert record["supervisor"]["pid"] == os.getpid()  # the other fields are kept
    assert not seats.supervisor_running(pool, MEMBER)
    assert qapool.gone(pool, MEMBER)
    # a new occupation resets the record: the seat's supervisor counts again
    seats.write_proc(pool, MEMBER, reset=True, supervisor=os.getpid())
    assert seats.supervisor_running(pool, MEMBER)


def test_config_error_exit_records_gave_up(pool, monkeypatch):
    """Exit code 3 (a refused launcher): the supervisor keeps its window open and never counts as running."""
    _dead(pool, None)
    seats.write_proc(pool, MEMBER, supervisor=os.getpid())

    class Child:
        def wait(self):
            return 3
    monkeypatch.setattr(supervisor, "subprocess", SimpleNamespace(Popen=lambda *a, **k: Child()))
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(sleep=lambda seconds: None, monotonic=time.monotonic))
    assert supervisor.run(str(pool.paths.repo), "claude", MEMBER) == 1
    assert proc_info(pool, MEMBER)["gave_up"] is True
    assert qapool.gone(pool, MEMBER)


def test_gave_up_not_recorded_over_another_supervisor(pool, sleeper, monkeypatch):
    _dead(pool, sleeper.pid)

    class Child:
        def wait(self):
            return 3
    monkeypatch.setattr(supervisor, "subprocess", SimpleNamespace(Popen=lambda *a, **k: Child()))
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(sleep=lambda seconds: None, monotonic=time.monotonic))
    supervisor.run(str(pool.paths.repo), "claude", MEMBER)
    assert "gave_up" not in proc_info(pool, MEMBER)
    assert seats.supervisor_running(pool, MEMBER)


def test_free_member_within_startup_deadline_not_gone(pool):
    assert seats.liveness(pool, MEMBER) == "free"
    now = time.time()
    assert not qapool.gone(pool, MEMBER, opened_at=now - 5, now=now)
    deadline = qapool.startup_deadline_seconds(pool, MEMBER)
    assert not qapool.gone(pool, MEMBER, opened_at=now - deadline + 1, now=now)
    assert not qapool.mark_gone(pool, MEMBER, opened_at=now - 5, now=now)


def test_free_member_past_deadline_gone(pool):
    now = time.time()
    deadline = qapool.startup_deadline_seconds(pool, MEMBER)
    assert qapool.gone(pool, MEMBER, opened_at=now - deadline - 1, now=now)
    assert qapool.gone(pool, MEMBER)  # no opening in progress: a free member is gone at once (router start)
    assert qapool.mark_gone(pool, MEMBER, opened_at=now - deadline - 1, now=now)
    assert pool.reload_config().seats[MEMBER].removing == "graceful"


def test_alive_member_never_gone(pool):
    lease = seats.acquire_qa(pool, "claude", None, seat=MEMBER)
    try:
        assert seats.liveness(pool, MEMBER) == "alive"
        assert not qapool.gone(pool, MEMBER, opened_at=0, now=time.time())
    finally:
        seats.release(pool, lease)


def test_reboot_members_gone(pool, sleeper):
    _dead(pool, sleeper.pid)
    record = proc_info(pool, MEMBER)
    record["boot_id"] = "another-boot"
    atomic_write_json(pool.paths.seats_dir / f"{MEMBER}.proc.json", record)
    assert not seats.supervisor_running(pool, MEMBER)  # its pid and start time match, but not its boot
    assert qapool.gone(pool, MEMBER)


def test_router_and_unregistered_never_marked(pool):
    assert not qapool.mark_gone(pool, "claude-qa")
    assert not qapool.mark_gone(pool, "claude-qa3")
    assert "claude-qa3" not in pool.reload_config().seats


def test_free_member_with_its_supervisor_running_is_not_gone(pool, sleeper):
    """A launcher that exited through ``finally`` released its seat ref (``free``) while its supervisor waits to
    restart it: the member is not gone, not removable, whatever its opening time (final review, Codex #2)."""
    seats.write_proc(pool, MEMBER, reset=True, supervisor=sleeper.pid)
    assert seats.liveness(pool, MEMBER) == "free" and seats.supervisor_running(pool, MEMBER)
    assert not qapool.gone(pool, MEMBER)
    assert not qapool.gone(pool, MEMBER, opened_at=time.time() - 10_000)
    assert not qapool.removable(pool, MEMBER)
