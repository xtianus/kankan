import os
import subprocess
from pathlib import Path

import pytest


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.config, ~/coding/.kankan and skills."""
    monkeypatch.setenv("KANKAN_REAL_HOME", os.environ.get("KANKAN_REAL_HOME") or os.path.expanduser("~"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("KANKAN_FAULT", "KANKAN_TRELLO_KEY", "KANKAN_TRELLO_TOKEN", "KANKAN_SEAT", "KANKAN_SESSION", "KANKAN_PROJECT", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("KANKAN_SKIP_PREFLIGHT", "1")  # tests/test_doctor.py unsets it where it matters
    from kankan import trello
    monkeypatch.setattr(trello._BUCKET, "limit", 10 ** 9)
    # `kankan init` waits settle_seconds (3 s) before reading the registry back from the real Trello; the fake
    # server is consistent at once, so tests skip that wait (it is the only clock call in kankan.init)
    import types
    from kankan import init as init_mod
    monkeypatch.setattr(init_mod, "clock", types.SimpleNamespace(sleep=lambda seconds: None))
    # Explicit offline choices; runtime never supplies implicit seeds.
    import json
    import sys
    overrides = home / ".config/kankan/agents.json"
    overrides.parent.mkdir(parents=True)
    overrides.write_text(json.dumps({agent_type: {
        "version_command": [sys.executable, "-c", "print('fake-v1')"],
        "model_check": [sys.executable, "-c", "import sys; sys.exit(0)", "{model}", "{effort}"],
        **({"models_from": {"command": [sys.executable, "-c", "print(chr(123)+chr(125))"], "file": str(home / 'absent-models.json')}} if agent_type == 'codex' else {}),
        "seed": {
        role: {"model": model, "effort": "high"} for role in ("implementation", "review")}}
        for agent_type, model in (("claude", "sonnet"), ("codex", "gpt-6.1-sol"))}))
    return home


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "coding" / "proj"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def virtual_time(monkeypatch):
    """Sleeps advance a virtual offset instead of blocking; monotonic includes it."""
    import time
    from kankan import clock
    offset = [0.0]
    monkeypatch.setattr(clock, "sleep", lambda s: offset.__setitem__(0, offset[0] + s))
    monkeypatch.setattr(clock, "monotonic", lambda: time.monotonic() + offset[0])
    return offset


@pytest.fixture
def fake_trello():
    from tests.fakes.fake_trello import FakeTrello
    with FakeTrello() as server:
        yield server


@pytest.fixture
def kproject(git_repo, fake_trello, monkeypatch):
    """A project bound to a fake board, with fast process kills."""
    from kankan import seats
    from tests.helpers import bind_project
    monkeypatch.setattr(seats, "KILL_GRACE", 0.5)
    return bind_project(git_repo, fake_trello, monkeypatch)


@pytest.fixture
def tmux_server():
    """A private tmux server, killed at teardown."""
    from kankan.tmux import Tmux
    server = Tmux(f"kankan-test-{os.getpid()}-{id(object())}")
    yield server
    server.run(["kill-server"], check=False)
    socket = Path(f"/tmp/tmux-{os.getuid()}") / server.socket
    if socket.exists():
        socket.unlink()


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def agent_path(tmp_path, monkeypatch):
    """A PATH directory with ``claude``/``codex`` (the fake agent) and ``kankan`` (this checkout)."""
    import sys
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = ROOT / "tests" / "fakes" / "fake_agent.py"
    for name in ("claude", "codex"):
        script = bindir / name
        script.write_text(f"#!{sys.executable}\nimport runpy, sys\nsys.argv.insert(1, '--type={name}')\n"
                          f"runpy.run_path({str(fake)!r}, run_name='__main__')\n")
        script.chmod(0o755)
    kankan = bindir / "kankan"
    kankan.write_text(f"#!/bin/sh\nexec {sys.executable} -m kankan \"$@\"\n")
    kankan.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return bindir


@pytest.fixture
def external_host():
    """An HTTP server on ``localhost``: another origin than the fake Trello server on ``127.0.0.1``."""
    from tests.fakes.external_host import ExternalHost
    with ExternalHost(b"external content\n" * 1000) as server:
        yield server
