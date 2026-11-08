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
    for var in ("KANKAN_FAULT", "KANKAN_TRELLO_KEY", "KANKAN_TRELLO_TOKEN", "KANKAN_SEAT", "KANKAN_SESSION", "KANKAN_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    from kankan import trello
    monkeypatch.setattr(trello._BUCKET, "limit", 10 ** 9)
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
