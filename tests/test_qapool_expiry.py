"""Prompt cache expiry (qapool T§3): exact readings of the current agent process, estimates from the last turn's
end, and the launcher's part (``turn_ended_at``, the status line setting)."""

import json
import os
import time
from types import SimpleNamespace

from kankan import procutil, qapool
from kankan.adapters import ModelEffort, load_adapter
from kankan.config import atomic_write_json
from kankan.qa_launcher import QaLauncher
from kankan.seats import write_proc
from kankan.seatstate import load_state, update_state


def _reading(project, seat, expires_at, pid=None, start=None):
    pid = pid or os.getpid()
    atomic_write_json(project.paths.seats_dir / f"{seat}.cache.json",
                      {"expires_at": expires_at, "ttl": "1h", "agent": {"pid": pid, "start": start or
                                                                        procutil.start_time(pid)},
                       "read_at": time.time()})


def _turn_ended(project, seat, when):
    update_state(project.paths, seat, lambda s: setattr(s, "turn_ended_at", when))


def test_expiry_exact_current_process(kproject):
    write_proc(kproject, "claude-qa", agent=os.getpid())
    _reading(kproject, "claude-qa", time.time() + 600)
    when, exact = qapool.expiry(kproject, "claude-qa")
    assert exact and abs(when - (time.time() + 600)) < 5
    assert qapool.warm(kproject, "claude-qa")
    _reading(kproject, "claude-qa", time.time() - 1)  # a past expires_at means cold
    when, exact = qapool.expiry(kproject, "claude-qa")
    assert exact and not qapool.warm(kproject, "claude-qa")


def test_expiry_reading_of_previous_process_ignored(kproject):
    write_proc(kproject, "claude-qa", agent=os.getpid())
    _reading(kproject, "claude-qa", time.time() + 600, start=1)  # another process with the same pid
    assert qapool.expiry(kproject, "claude-qa") == (None, False)
    _turn_ended(kproject, "claude-qa", time.time() - 60)
    when, exact = qapool.expiry(kproject, "claude-qa")
    assert not exact and abs(when - (time.time() - 60 + 3600)) < 5  # Claude's estimated lifetime: 60 minutes
    _reading(kproject, "claude-qa", None)  # no request yet (prompt_cache null): estimated
    assert qapool.expiry(kproject, "claude-qa")[1] is False


def test_expiry_estimated_from_turn_ended_at(kproject):
    write_proc(kproject, "codex-qa2", agent=os.getpid())
    _reading(kproject, "codex-qa2", time.time() + 6000)  # Codex has no reporter: a reading never counts
    ended = time.time() - 100
    _turn_ended(kproject, "codex-qa2", ended)
    when, exact = qapool.expiry(kproject, "codex-qa2")
    assert not exact and abs(when - (ended + 30 * 60)) < 1
    assert qapool.warm(kproject, "codex-qa2")
    assert not qapool.warm(kproject, "codex-qa2", now=ended + 30 * 60 + 1)


def test_cold_without_finished_turn(kproject):
    assert qapool.expiry(kproject, "claude-qa3") == (None, False)
    assert not qapool.warm(kproject, "claude-qa3")


def _launcher(kproject, seat="claude-qa"):
    from kankan.seats import Lease
    launcher = QaLauncher(kproject, load_adapter(kproject.paths, "claude", read_only=True), None, "claude", seat)
    launcher.lease = Lease(seat=seat, type="claude", session="s" * 32, seat_oid="0" * 40, fd=-1)
    return launcher


def test_restart_clears_turn_ended_at(kproject, monkeypatch):
    launcher = _launcher(kproject)
    launcher.task = {"id": "t1", "card": "c" * 24, "conversation": "a" * 16}
    launcher.record_turn_end()  # a turn's end (its signal, or none)
    assert load_state(kproject.paths, "claude-qa").turn_ended_at is not None
    from kankan import launcher as base
    monkeypatch.setattr(base, "subprocess", SimpleNamespace(Popen=FakePopen))
    launcher.spawn_agent(ModelEffort("sonnet", "high"))
    assert load_state(kproject.paths, "claude-qa").turn_ended_at is None


class FakePopen:
    def __init__(self, command, **kwargs):
        self.command = command
        self.pid = os.getpid()

    def poll(self):
        return None


def test_agent_command_gets_settings_for_reporter(kproject, monkeypatch):
    from kankan import launcher as base
    monkeypatch.setattr(base, "subprocess", SimpleNamespace(Popen=FakePopen))
    launcher = _launcher(kproject)
    launcher.spawn_agent(ModelEffort("sonnet", "high"))
    command = launcher.agent.command
    index = command.index("--settings")
    settings = json.loads(command[index + 1])
    assert settings == {"statusLine": {"type": "command",
                                       "command": f"{kproject.paths.kankan_dir / 'bin' / 'kankan'} statusline"}}
    codex = QaLauncher(kproject, load_adapter(kproject.paths, "codex", read_only=True), None, "codex", "codex-qa")
    codex.lease = launcher.lease.__class__(seat="codex-qa", type="codex", session="s" * 32, seat_oid="0" * 40, fd=-1)
    codex.spawn_agent(ModelEffort("gpt-6.1-sol", "high"))
    assert "--settings" not in codex.agent.command
