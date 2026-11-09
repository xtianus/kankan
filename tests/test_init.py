import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kankan import seats, workflow
from kankan.config import Paths, load_config, registry_block
from kankan.errors import KankanError, Status
from kankan.init import init, parse_board_url
from kankan.project import Project
from kankan.properties import PropertiesFile
from tests.conftest import git
from tests.fakes.fake_trello import KEY, TOKEN

FIXTURE = Path(__file__).parent / "fixtures" / "messy.properties"


@pytest.fixture
def env(fake_trello, monkeypatch):
    monkeypatch.setenv("KANKAN_TRELLO_URL", fake_trello.url)
    monkeypatch.setenv("KANKAN_TRELLO_KEY", KEY)
    monkeypatch.setenv("KANKAN_TRELLO_TOKEN", TOKEN)
    board = fake_trello.add_board("proj", "Our board.")
    return fake_trello, board


def answers(*values, default=""):
    queue = list(values)
    return lambda question: queue.pop(0) if queue else default


def quiet(_):
    pass


def run_init(repo, board, **kw):
    kw.setdefault("prompt", answers())
    kw.setdefault("say", quiet)
    cfg, warnings = init(repo, board["url"], **kw)
    load_config(Paths.discover(repo)).settle_seconds  # config saved
    return cfg, warnings


def test_parse_board_url():
    assert parse_board_url("https://trello.com/b/s5C4vWbG/test1") == "s5C4vWbG"
    assert parse_board_url("s5C4vWbG") == "s5C4vWbG"
    with pytest.raises(KankanError):
        parse_board_url("https://example.com/b/x")


def test_init_idempotent(git_repo, env, monkeypatch):
    fake, board = env
    monkeypatch.setattr("kankan.config.Config.settle_seconds", 0, raising=False)
    cfg1, _ = run_init(git_repo, board, prompt=answers("make test"))
    cfg2, _ = run_init(git_repo, board)
    for attr in ("backlog", "done", "blocked", "label_hold", "label_question", "label_error", "registry_id"):
        assert getattr(cfg1, attr) == getattr(cfg2, attr)
    assert cfg2.test_command == "make test"
    desc = fake.state["boards"][board["id"]]["desc"]
    assert desc.count("--- kankan-registry ---") == 1 and desc.startswith("Our board.")
    assert registry_block(desc)["registry-id"] == cfg1.registry_id
    assert len([l for l in fake.state["lists"].values() if l["name"] == "Backlog"]) == 1
    assert cfg1.notify_username == "kankanbot"
    assert (git_repo / ".git" / "hooks" / "pre-push").exists()
    assert "/.kankan/" in (git_repo / ".git" / "info" / "exclude").read_text()


def test_second_registry_refused_takeover_accepted(git_repo, tmp_path, env, monkeypatch):
    fake, board = env
    run_init(git_repo, board)
    project = Project.open(git_repo)
    fake.add_card(project.cfg.backlog, "c")
    lease = seats.acquire(project, "claude")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(git_repo), str(other))
    with pytest.raises(KankanError) as exc:
        run_init(other, board)
    assert exc.value.status == Status.CONFIG_ERROR
    run_init(other, board, takeover=True)
    with pytest.raises(KankanError) as exc:
        workflow.tick(project, lease)
    assert exc.value.status == Status.CONFIG_ERROR


def test_init_adopts_existing_lists_when_confirmed(git_repo, env):
    fake, board = env
    backlog = fake.add_list(board["id"], "Backlog")
    done = fake.add_list(board["id"], "Done")
    cfg, _ = run_init(git_repo, board, prompt=answers("y", "n", "", ""))
    assert cfg.backlog == backlog["id"]
    assert cfg.done != done["id"]


def test_pre_push_blocks_agent_refs(git_repo, tmp_path, env):
    fake, board = env
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(git_repo, "remote", "add", "origin", str(remote))
    run_init(git_repo, board)
    head = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "update-ref", "refs/agent-cards/x", head)
    proc = subprocess.run(["git", "-C", str(git_repo), "push", "origin", "refs/agent-cards/x:refs/agent-cards/x"],
                          capture_output=True, text=True)
    assert proc.returncode != 0 and "kankan" in proc.stderr
    assert subprocess.run(["git", "-C", str(git_repo), "push", "-q", "origin", "main"]).returncode == 0


def test_existing_hook_not_overwritten(git_repo, env):
    fake, board = env
    hook = git_repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    _, warnings = run_init(git_repo, board)
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"
    assert any("pre-push" in w for w in warnings)


def test_archived_workflow_list_config_error(git_repo, env):
    fake, board = env
    cfg, _ = run_init(git_repo, board)
    fake.state["lists"][cfg.blocked]["closed"] = True
    with pytest.raises(KankanError) as exc:
        run_init(git_repo, board)
    assert exc.value.status == Status.CONFIG_ERROR


def test_init_preserves_properties(git_repo, tmp_path, env, monkeypatch):
    fake, board = env
    monkeypatch.delenv("KANKAN_TRELLO_KEY")
    monkeypatch.delenv("KANKAN_TRELLO_TOKEN")
    secrets_file = tmp_path / "secrets.properties"
    shutil.copy(FIXTURE, secrets_file)
    os.chmod(secrets_file, 0o600)
    run_init(git_repo, board, credentials=secrets_file, prompt=answers(KEY, TOKEN))
    original = FIXTURE.read_bytes().split(b"\r\n")
    updated = secrets_file.read_bytes().split(b"\r\n")
    assert updated[:8] == original[:8] and updated[9:13] == original[9:13]
    props = PropertiesFile.load(secrets_file)
    assert props.get("kankan.trello.key") == KEY and props.get("kankan.trello.token") == TOKEN
    assert props.get("db.password") == "s3crét"
    assert Project.open(git_repo).trello.me()["username"] == "kankanbot"
    assert KEY not in (git_repo / ".git" / "kankan" / "config.json").read_text()


def test_notify_other_user(git_repo, env):
    fake, board = env
    other = fake.add_member("alice")
    cfg, _ = run_init(git_repo, board, notify="@alice")
    assert cfg.notify_id == other["id"] and cfg.notify_username == "alice"


def test_main_checked_out_warning(git_repo, env):
    fake, board = env
    _, warnings = run_init(git_repo, board)
    assert not any("checked out" in w for w in warnings)  # main is the user's branch now
    _, warnings = run_init(git_repo, board, prompt=answers("", "main"))
    assert any("main is checked out" in w for w in warnings)
