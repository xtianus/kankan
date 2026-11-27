"""``kankan statusline`` (qapool T§3): Claude Code's status line command for Q&A agents."""

import json
import os
import subprocess
import sys

from kankan import auditlog, statusline
from kankan.config import Paths, read_json
from kankan.seats import write_proc
from tests.conftest import ROOT

INPUT = {"session_id": "s", "prompt_cache": {"expires_at": 1890000000, "ttl": "1h", "warm": True}}


def _seat_env(monkeypatch, seat="claude-qa2"):
    monkeypatch.setenv("KANKAN_SEAT", seat)
    monkeypatch.setenv("KANKAN_SESSION", "x" * 32)


def test_statusline_writes_reading(kproject, monkeypatch):
    _seat_env(monkeypatch)
    write_proc(kproject, "claude-qa2", agent=os.getpid())  # the agent: an ancestor of the command (here: itself)
    out = statusline.run(json.dumps(INPUT), str(kproject.paths.repo))
    assert out == ""
    reading = read_json(kproject.paths.seats_dir / "claude-qa2.cache.json", None)
    assert reading["expires_at"] == 1890000000 and reading["ttl"] == "1h"
    assert reading["agent"]["pid"] == os.getpid() and reading["agent"]["start"] is not None
    assert reading["read_at"] > 0
    statusline.run(json.dumps({"prompt_cache": None}), str(kproject.paths.repo))  # before the first request
    assert read_json(kproject.paths.seats_dir / "claude-qa2.cache.json", None)["expires_at"] is None


def test_statusline_chains_user_status_line_by_precedence(kproject, monkeypatch, tmp_path):
    _seat_env(monkeypatch)
    worktree = tmp_path / "wt"
    (worktree / ".claude").mkdir(parents=True)
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "echo home"}}))
    monkeypatch.setattr(statusline, "_paths", lambda cwd: kproject.paths)
    assert statusline.run(json.dumps(INPUT), str(worktree)) == "home\n"
    (worktree / ".claude" / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "cat > got.json; pwd"}}))
    assert statusline.run(json.dumps(INPUT), str(worktree)) == f"{worktree}\n"
    assert json.loads((worktree / "got.json").read_text()) == INPUT  # the same standard input
    (worktree / ".claude" / "settings.local.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "echo local"}}))
    assert statusline.run(json.dumps(INPUT), str(worktree)) == "local\n"
    (worktree / ".claude" / "settings.local.json").write_text(json.dumps({"statusLine": {"type": "static"}}))
    assert statusline.run(json.dumps(INPUT), str(worktree)) == f"{worktree}\n"  # not a command: the next file


def test_statusline_no_seat_does_nothing(kproject, monkeypatch):
    for var in ("KANKAN_SEAT", "KANKAN_SESSION"):
        monkeypatch.delenv(var, raising=False)
    assert statusline.run(json.dumps(INPUT), str(kproject.paths.repo)) == ""
    assert not list(kproject.paths.seats_dir.glob("*.cache.json")) if kproject.paths.seats_dir.exists() else True


def test_statusline_failing_user_command_exits_zero(kproject, monkeypatch, tmp_path):
    _seat_env(monkeypatch)
    worktree = kproject.paths.repo
    (worktree / ".claude").mkdir(exist_ok=True)
    (worktree / ".claude" / "settings.local.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "echo partial; exit 7"}}))
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run([sys.executable, "-m", "kankan", "statusline"], input="not json", cwd=worktree, env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout == "partial\n"
    events = [e["event"] for e in auditlog.read(Paths.discover(worktree))]
    assert "statusline_failed" in events
