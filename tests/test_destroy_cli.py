"""``kankan destroy`` on the command line (destroy spec F§3 and F§5, T§5)."""

import builtins
import json

import pytest

from kankan import cli, destroy, messages
from kankan.config import Paths
from kankan.errors import Status
from tests.conftest import git
from tests.helpers import NoTmux
from tests.test_destroy_run import NAME, disk, full_project, writes  # noqa: F401 - the fixture


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    monkeypatch.setattr("kankan.tmux.Tmux", NoTmux)


def tty(monkeypatch, value=True):
    monkeypatch.setattr("sys.stdin.isatty", lambda: value)


def typed(monkeypatch, *replies):
    queue = list(replies)

    def fake_input(prompt=""):
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(builtins, "input", fake_input)


def destroy_cli(repo, monkeypatch, *argv):
    monkeypatch.chdir(repo)
    return cli.main(["destroy", *argv])


def test_help_lists_destroy(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "destroy" in capsys.readouterr().out


def test_cli_no_tty_requires_confirm(full_project, monkeypatch, capsys):
    tty(monkeypatch, False)
    code = destroy_cli(full_project.repo, monkeypatch)
    captured = capsys.readouterr()
    assert code == Status.USAGE_ERROR.exit_code
    assert "kankan destroy needs the phrase 'destroy <board name>'" in captured.err
    assert NAME not in captured.out + captured.err
    assert not writes(full_project.fake)


def test_cli_no_tty_with_confirm(full_project, monkeypatch, capsys):
    tty(monkeypatch, False)
    code = destroy_cli(full_project.repo, monkeypatch, "--confirm", f"destroy {NAME}")
    assert code == 0
    assert set(full_project.fake.state["cards"]) == {full_project.ideas_card}
    assert git(full_project.repo, "for-each-ref", "refs/agent-seats") == ""
    assert not (full_project.paths.kankan_dir / "config.json").exists()


def test_cli_json_envelope(full_project, monkeypatch, capsys):
    tty(monkeypatch, False)
    code = destroy_cli(full_project.repo, monkeypatch, "--json", "--confirm", f"destroy {NAME}")
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True and out["status"] == "success"
    assert out["data"]["board"]["cards_deleted"]


@pytest.mark.parametrize("error", [KeyboardInterrupt(), EOFError()])
def test_cli_interrupt_second_prompt(full_project, monkeypatch, capsys, error):
    full = full_project
    (full.seat_wt / "new.txt").write_text("work")
    tty(monkeypatch)
    prompts = []
    queue = [destroy.DISCARD_PHRASE, error]

    def fake_input(prompt=""):
        prompts.append(prompt)
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(builtins, "input", fake_input)
    before = disk(full.paths)
    code = destroy_cli(full.repo, monkeypatch, "--json")
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == 0 and out["data"] == {"cancelled": True} and out["message"] == messages.DESTROY_REPORT_CANCELLED
    assert "nothing changed" in out["message"]
    assert queue == [] and len(prompts) == 2  # the prompt text goes to stderr, not to input()
    discard_prompt = messages.DESTROY_ASK_DISCARD.format(phrase=destroy.DISCARD_PHRASE)
    assert 0 <= captured.err.find(discard_prompt) < captured.err.find(messages.DESTROY_ASK_BOARD)
    assert disk(full.paths) == before
    assert (full.seat_wt / "new.txt").exists() and not writes(full.fake)


def test_unbound_clone_nothing_to_destroy(git_repo, monkeypatch, capsys):
    tty(monkeypatch, False)
    assert destroy_cli(git_repo, monkeypatch) == 0
    assert capsys.readouterr().out.strip() == messages.DESTROY_REPORT_NOTHING


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_corrupt_config_local_only(git_repo, monkeypatch, content):
    paths = Paths.discover(git_repo)
    paths.kankan_dir.mkdir()
    (paths.kankan_dir / "config.json").write_text(content)
    git(git_repo, "update-ref", "refs/agent-seats/claude-1", git(git_repo, "rev-parse", "HEAD"))
    tty(monkeypatch, False)
    assert destroy_cli(git_repo, monkeypatch, "--confirm", destroy.LOCAL_PHRASE) == 0
    assert git(git_repo, "for-each-ref", "refs/agent-seats") == ""
    assert not (paths.kankan_dir / "config.json").exists()


def test_second_run_nothing_to_destroy(full_project, monkeypatch, capsys):
    tty(monkeypatch, False)
    hook = full_project.paths.gcd / "hooks" / "pre-push"
    assert destroy_cli(full_project.repo, monkeypatch, "--confirm", f"destroy {NAME}") == 0
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\n# foreign\n")
    capsys.readouterr()
    assert destroy_cli(full_project.repo, monkeypatch) == 0
    assert capsys.readouterr().out.strip() == messages.DESTROY_REPORT_NOTHING
    assert hook.read_text().endswith("# foreign\n")


def test_ref_only_clone_twice(git_repo, monkeypatch, capsys):
    tty(monkeypatch, False)
    paths = Paths.discover(git_repo)
    git(git_repo, "update-ref", "refs/agent-seats/claude-1", git(git_repo, "rev-parse", "HEAD"))
    assert destroy_cli(git_repo, monkeypatch, "--confirm", destroy.LOCAL_PHRASE) == 0
    assert git(git_repo, "for-each-ref", "refs/agent-seats") == ""
    assert not (paths.kankan_dir / "log.jsonl").exists()
    capsys.readouterr()
    assert destroy_cli(git_repo, monkeypatch) == 0
    assert capsys.readouterr().out.strip() == messages.DESTROY_REPORT_NOTHING


def test_lock_only_clone_nothing_to_destroy(git_repo, monkeypatch, capsys):
    tty(monkeypatch, False)
    paths = Paths.discover(git_repo)
    lock = paths.kankan_dir / "qa" / "c1" / "op.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    for _ in range(2):
        assert destroy_cli(git_repo, monkeypatch) == 0
        assert capsys.readouterr().out.strip() == messages.DESTROY_REPORT_NOTHING
        assert lock.exists() and not (paths.kankan_dir / "log.jsonl").exists()


@pytest.mark.parametrize("setup, phrase", [
    ("discard", destroy.DISCARD_PHRASE),
    ("board", messages.DESTROY_QUESTION_BOARD),
])
def test_no_tty_message_names_the_public_phrase(full_project, monkeypatch, capsys, setup, phrase):
    if setup == "discard":
        (full_project.seat_wt / "new.txt").write_text("work")
    tty(monkeypatch, False)
    assert destroy_cli(full_project.repo, monkeypatch) == Status.USAGE_ERROR.exit_code
    err = capsys.readouterr().err
    assert messages.DESTROY_NEEDS_TERMINAL.format(phrase=phrase) in err and NAME not in err


def test_no_tty_message_for_the_local_phrase(git_repo, monkeypatch, capsys):
    git(git_repo, "update-ref", "refs/agent-seats/claude-1", git(git_repo, "rev-parse", "HEAD"))
    tty(monkeypatch, False)
    assert destroy_cli(git_repo, monkeypatch) == Status.USAGE_ERROR.exit_code
    assert messages.DESTROY_NEEDS_TERMINAL.format(phrase=destroy.LOCAL_PHRASE) in capsys.readouterr().err


def test_inventory_output_failure_writes_nothing(full_project, monkeypatch, capsys):
    tty(monkeypatch)
    real_print = print
    calls = []

    def broken_print(*args, **kwargs):
        if kwargs.get("file") is not None and not calls:
            calls.append(1)
            raise BrokenPipeError("stderr closed")
        real_print(*args, **kwargs)

    monkeypatch.setattr(cli, "print", broken_print, raising=False)
    before = disk(full_project.paths)
    code = destroy_cli(full_project.repo, monkeypatch)
    assert code == Status.INTERNAL_ERROR.exit_code and calls
    assert disk(full_project.paths) == before and not writes(full_project.fake)
