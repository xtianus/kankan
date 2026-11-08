import os
import subprocess
from pathlib import Path

import pytest


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.config, ~/coding/.kankan and skills."""
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
