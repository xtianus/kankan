"""kankan doctor and the agent preflight (spec §15.5)."""

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from kankan import agents_cmd, cli, doctor, skill_install
from kankan.config import Paths, load_config, save_config, write_registry_block
from kankan.errors import KankanError, Status
from tests.conftest import git
from tests.flow import commit


def by_name(checks):
    return {c.name: c for c in checks}


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    """A PATH with only system tools plus the scripts a test writes."""
    path = tmp_path / "fakebin"
    path.mkdir()
    monkeypatch.setenv("PATH", f"{path}:/usr/bin:/bin")

    def script(name, body="exit 0"):
        f = path / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(0o755)
        return f
    script.dir = path
    return script


# ---------------------------------------------------------------- machine

def test_skill_missing_is_fixed(tmp_path):
    check = doctor.check_skill(fix=False)
    assert check.status == doctor.MANUAL and not any(t.exists() for t in skill_install.targets())
    check = doctor.check_skill(fix=True)
    assert check.status == doctor.FIXED
    assert all(t.is_symlink() and os.readlink(t) == str(skill_install.skill_dir()) for t in skill_install.targets())
    assert doctor.check_skill(fix=True).status == doctor.OK


def test_skill_real_directory_is_manual():
    target = skill_install.targets()[0]
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("mine")
    check = doctor.check_skill(fix=True)
    assert check.status == doctor.MANUAL and str(target) in check.action
    assert (target / "SKILL.md").read_text() == "mine"


def test_agents_missing(fakebin):
    checks = by_name(doctor.check_agents(None, ("claude", "codex")))
    assert checks["agent:claude"].status == doctor.MANUAL and checks["agent:codex"].status == doctor.MANUAL
    fakebin("claude", 'exit 0')
    checks = by_name(doctor.check_agents(None, ("claude", "codex")))
    assert checks["agent:claude"].status == doctor.OK
    assert checks["agent:codex"].status == doctor.WARN and checks["login:codex"].status == doctor.SKIPPED


def test_login_check(fakebin, monkeypatch):
    fakebin("codex", 'exit 1')
    check = by_name(doctor.check_agents(None, ("codex",)))["login:codex"]
    assert check.status == doctor.WARN and "codex" in check.action
    fakebin("codex", 'exit 0')
    assert by_name(doctor.check_agents(None, ("codex",)))["login:codex"].status == doctor.OK
    monkeypatch.setattr(doctor, "LOGIN_TIMEOUT", 0.5)
    fakebin("codex", 'sleep 3')
    assert by_name(doctor.check_agents(None, ("codex",)))["login:codex"].status == doctor.SKIPPED
    user_agents = Path(os.path.expanduser("~")) / ".config" / "kankan" / "agents.json"
    user_agents.parent.mkdir(parents=True, exist_ok=True)
    user_agents.write_text(json.dumps({"codex": {"login_check": []}}))
    fakebin("codex", 'exit 1')
    assert by_name(doctor.check_agents(None, ("codex",)))["login:codex"].status == doctor.SKIPPED


def test_preflight_ignores_login(fakebin, monkeypatch):
    monkeypatch.delenv("KANKAN_SKIP_PREFLIGHT")
    monkeypatch.setattr(doctor, "check_kankan_path", lambda: doctor.Check("kankan-path", doctor.OK, "x"))
    fakebin("codex", 'exit 1')  # logged out
    doctor.preflight(["codex"])
    assert skill_install.targets()[1].is_symlink()  # the codex link was installed on the way
    assert not skill_install.targets()[0].exists()  # only for the type being started


# ---------------------------------------------------------------- project

def test_outside_project(tmp_path, fakebin):
    checks = by_name(doctor.run(tmp_path, fix=False, online=False))
    assert checks["project"].status == doctor.SKIPPED
    assert "hook" not in checks


def test_uninitialized_project(git_repo, fakebin):
    checks = by_name(doctor.run(git_repo, fix=False, online=False))
    assert checks["project"].status == doctor.MANUAL and checks["project"].action == "kankan init <board-url>"
    assert "hook" not in checks


@pytest.fixture
def bound(git_repo, fake_trello, monkeypatch):
    from tests.helpers import bind_project
    git(git_repo, "branch", "kankan-agents")
    git(git_repo, "config", "kankan.integrationBranch", "kankan-agents")
    project = bind_project(git_repo, fake_trello, monkeypatch, integration_branch="kankan-agents")
    from kankan.init import add_excludes, install_hook
    install_hook(project.paths)
    add_excludes(project.paths)
    return project


def test_bound_project_all_ok(bound, fakebin):
    checks = by_name(doctor.run(bound.paths.repo, fix=False, online=True))
    for name in ("project", "adapters", "hook", "exclude", "integration-branch", "branches", "credentials",
                 "registry", "lists", "labels", "reconcile"):
        assert checks[name].status == doctor.OK, (name, checks[name])
    assert checks["autostart"].status == doctor.SKIPPED


def test_hook_and_exclude_fixed(bound, fakebin):
    from kankan.init import hook_path
    hook = hook_path(bound.paths)
    hook.unlink()
    exclude = bound.paths.gcd / "info" / "exclude"
    exclude.write_text("")
    checks = by_name(doctor.run(bound.paths.repo, fix=True, online=False))
    assert checks["hook"].status == doctor.FIXED and os.access(hook, os.X_OK)
    assert checks["exclude"].status == doctor.FIXED and "/.kankan/" in exclude.read_text()
    hook.chmod(0o644)
    assert by_name(doctor.run(bound.paths.repo, fix=True, online=False))["hook"].status == doctor.FIXED
    assert os.access(hook, os.X_OK)


def test_no_fix_never_mutates(bound, fake_trello, fakebin):
    from kankan.init import hook_path
    hook = hook_path(bound.paths)
    hook.unlink()
    exclude = bound.paths.gcd / "info" / "exclude"
    exclude.write_text("")
    del fake_trello.state["labels"][bound.cfg.label_question]
    labels_before = dict(fake_trello.state["labels"])
    def files():
        return {str(path): path.read_bytes() for root in (bound.paths.kankan_dir, Path.home()/'.config', Path.home()/'.cache')
                if root.exists() for path in root.rglob('*') if path.is_file() and not path.is_symlink()}
    files_before = files()
    checks = by_name(doctor.run(bound.paths.repo, fix=False, online=True))
    assert checks["hook"].status == checks["exclude"].status == checks["labels"].status == doctor.MANUAL
    assert checks["skill"].status == doctor.MANUAL
    assert not hook.exists() and exclude.read_text() == "" and fake_trello.state["labels"] == labels_before
    assert not any(t.exists() for t in skill_install.targets())
    assert files() == files_before


def test_missing_label_recreated(bound, fake_trello, fakebin):
    old = bound.cfg.label_question
    del fake_trello.state["labels"][old]
    checks = by_name(doctor.run(bound.paths.repo, fix=True, online=True))
    assert checks["labels"].status == doctor.FIXED and checks["labels-restart"].status == doctor.WARN
    new = load_config(bound.paths).label_question
    assert new != old and new in fake_trello.state["labels"]


def test_foreign_registry_manual_and_no_board_repair(bound, fake_trello, fakebin):
    board_id = bound.cfg.board_id
    fake_trello.state["boards"][board_id]["desc"] = write_registry_block("x", {"registry-id": "other", "host": "h",
                                                                               "path": "/p"})
    del fake_trello.state["labels"][bound.cfg.label_error]
    checks = by_name(doctor.run(bound.paths.repo, fix=True, online=True))
    assert checks["registry"].status == doctor.MANUAL and "--takeover" in checks["registry"].action
    assert checks["labels"].status == doctor.SKIPPED
    assert bound.cfg.label_error not in fake_trello.state["labels"]


def test_archived_list_manual(bound, fake_trello, fakebin):
    fake_trello.state["lists"][bound.cfg.blocked]["closed"] = True
    check = by_name(doctor.run(bound.paths.repo, fix=True, online=True))["lists"]
    assert check.status == doctor.MANUAL and "Blocked" in check.action


def test_offline_skips_online_checks_without_credentials(bound, fakebin, monkeypatch):
    monkeypatch.delenv("KANKAN_TRELLO_KEY")
    monkeypatch.delenv("KANKAN_TRELLO_TOKEN")
    checks = by_name(doctor.run(bound.paths.repo, fix=False, online=False))
    assert checks["hook"].status == doctor.OK
    assert all(checks[n].status == doctor.SKIPPED for n in ("credentials", "registry", "lists", "labels", "reconcile"))


def test_integration_branch_checked_out_warns(bound, fakebin):
    repo = bound.paths.repo
    checks = by_name(doctor.run(repo, fix=False, online=False))
    assert checks["integration-branch"].status == doctor.OK  # main checked out is fine
    git(repo, "switch", "-q", "kankan-agents")
    checks = by_name(doctor.run(repo, fix=False, online=False))
    assert checks["integration-branch"].status == doctor.WARN
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "kankan-agents"


def test_branches_check(bound, fakebin, monkeypatch):
    repo = bound.paths.repo
    wt = repo.parent / "wt-agents"
    git(repo, "worktree", "add", "-q", str(wt), "kankan-agents")
    commit(wt, "card.txt")
    git(wt, "switch", "-q", "--detach")
    old = "2020-01-01T00:00:00+00:00"
    env = {**os.environ, "GIT_COMMITTER_DATE": old}
    import subprocess
    tree = git(repo, "rev-parse", "kankan-agents^{tree}")
    merge = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-p", "main", "-p", "kankan-agents", "-m", "m"],
                           capture_output=True, text=True, env=env, check=True).stdout.strip()
    git(repo, "update-ref", "refs/heads/kankan-agents", merge)
    commit(repo, "mine.txt")
    check = by_name(doctor.run(repo, fix=False, online=False))["branches"]
    assert check.status == doctor.WARN and "kankan-agents is 2 commits ahead / 1 behind main" in check.detail
    git(repo, "merge", "-q", "--no-edit", "kankan-agents")
    check = by_name(doctor.run(repo, fix=False, online=False))["branches"]
    assert check.status == doctor.OK
    cfg = load_config(bound.paths)
    cfg.user_branch = "missing"
    save_config(bound.paths, cfg)
    assert by_name(doctor.run(repo, fix=False, online=False))["branches"].status == doctor.SKIPPED


def test_autostart_generated_unit_rewritten(bound, fakebin, monkeypatch):
    log = fakebin.dir / "systemctl.log"
    fakebin("systemctl", f'echo "$@" >> {log}')
    fakebin("claude")
    unit = agents_cmd.unit_path(bound)
    unit.parent.mkdir(parents=True)
    generated = agents_cmd.unit_content(bound, {"claude": 1})
    unit.write_text(re.sub(r"^Environment=PATH=.*$", "Environment=PATH=/usr/bin:/bin", generated, flags=re.M))
    check = by_name(doctor.run(bound.paths.repo, fix=True, online=False, types=("claude",)))["autostart"]
    assert check.status == doctor.FIXED and str(fakebin.dir) in unit.read_text()
    assert log.read_text().split("\n")[0] == "--user daemon-reload" and "enable" not in log.read_text()
    unit.write_text(re.sub(r"^Environment=PATH=.*$", "Environment=PATH=/opt", generated, flags=re.M) + "# mine\n")
    check = by_name(doctor.run(bound.paths.repo, fix=True, online=False, types=("claude",)))["autostart"]
    assert check.status == doctor.MANUAL and unit.read_text().endswith("# mine\n")


# ---------------------------------------------------------------- CLI and preflight

def test_cli_exit_codes(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "run", lambda *a, **k: [doctor.Check("git", doctor.OK, "x"),
                                                        doctor.Check("logind", doctor.WARN, "y", "do z")])
    assert cli.main(["doctor", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in out["data"]["checks"]] == ["git", "logind"]
    monkeypatch.setattr(doctor, "run", lambda *a, **k: [doctor.Check("skill", doctor.MANUAL, "x", "kankan skill install")])
    assert cli.main(["doctor", "--json"]) == Status.NEEDS_ACTION.exit_code
    assert json.loads(capsys.readouterr().out)["data"]["checks"][0]["status"] == "manual"


def test_preflight_blocks_agent_start(kproject, fakebin, monkeypatch, tmux_server):
    monkeypatch.delenv("KANKAN_SKIP_PREFLIGHT")
    fakebin("claude")
    with pytest.raises(KankanError) as exc:
        agents_cmd.up(kproject, {"claude": 1, "codex": 1}, tmux=tmux_server)
    assert exc.value.status == Status.CONFIG_ERROR and "Codex CLI" in exc.value.message
    assert tmux_server.sessions() == []


def test_preflight_skipped_when_running(kproject, monkeypatch, tmux_server):
    monkeypatch.delenv("KANKAN_SKIP_PREFLIGHT")
    monkeypatch.setattr(agents_cmd, "live_counts", lambda project: {"claude": 1, "codex": 1})

    def boom(*a, **k):
        raise AssertionError("preflight must not run")
    monkeypatch.setattr(doctor, "preflight", boom)
    assert agents_cmd.up(kproject, {"claude": 1, "codex": 1}, tmux=tmux_server) == []


def test_preflight_custom_adapter(kproject, fakebin):
    fakebin("my-codex")
    (kproject.paths.kankan_dir / "agents.json").write_text(json.dumps({"codex": {"command": ["my-codex"]}}))
    assert by_name(doctor.check_agents(kproject.paths, ("codex",)))["agent:codex"].status == doctor.OK


def test_agent_session_path(kproject, tmp_path, monkeypatch):
    wrapper_dir = str(kproject.paths.kankan_dir / "bin")
    assert agents_cmd.agent_path("/usr/bin:/bin", kproject.paths).split(":")[0] == wrapper_dir
    assert agents_cmd.agent_path(f"/usr/bin:{wrapper_dir}", kproject.paths) == f"{wrapper_dir}:/usr/bin"

    class StubTmux:
        def __init__(self):
            self.env = None

        def sessions(self):
            return []

        def sessions_with_option(self, option):
            return {}

        def has_session(self, name):
            return False

        def new_session(self, name, cwd, argv, env=None):
            self.env = env

        def set_option(self, *a):
            pass
    stub = StubTmux()
    monkeypatch.setenv("PATH", "/stale/bin:/usr/bin")
    agents_cmd.agent(kproject, "claude", tmux=stub, preflight=False)
    assert stub.env["PATH"].split(":")[0] == wrapper_dir and "/stale/bin" in stub.env["PATH"]


# ---------------------------------------------------------------- review fixes

def test_agent_path_holds_only_kankan(kproject, tmp_path):
    """I1: the agents' PATH gets a private directory with only a kankan wrapper, never the venv's python/pip."""
    path = agents_cmd.agent_path("/usr/bin:/bin", kproject.paths)
    first = Path(path.split(":")[0])
    assert sorted(p.name for p in first.iterdir()) == ["kankan"]
    assert sys.executable in (first / "kankan").read_text()
    assert str(Path(sys.executable).parent) not in path.split(":")[:1]


def test_old_config_does_not_block_user_push(git_repo, fake_trello, monkeypatch, fakebin):
    """I2: integration branch == user branch (pre-1.2 config): no kankan.integrationBranch, so pushes of main pass."""
    from tests.helpers import bind_project
    project = bind_project(git_repo, fake_trello, monkeypatch, integration_branch="main")
    git(git_repo, "config", "kankan.integrationBranch", "main")
    all_checks = doctor.run(git_repo, fix=True, online=False)
    checks = by_name(all_checks)
    assert any(c.name == "integration-branch" and c.status == doctor.FIXED for c in all_checks)
    assert git(git_repo, "config", "--get", "kankan.integrationBranch", check=False) == ""
    assert checks["branches"].status == doctor.SKIPPED and "does not exist" not in checks["branches"].detail


def test_preflight_keeps_existing_skill_links(fakebin, monkeypatch, tmp_path):
    """M2: preflight installs a missing link but never re-points or refuses an existing one."""
    monkeypatch.delenv("KANKAN_SKIP_PREFLIGHT")
    monkeypatch.setattr(doctor, "check_kankan_path", lambda: doctor.Check("kankan-path", doctor.OK, "x"))
    fakebin("claude")
    claude_target, codex_target = skill_install.targets()
    claude_target.parent.mkdir(parents=True)
    claude_target.symlink_to(tmp_path)  # a deliberate dev link
    codex_target.mkdir(parents=True)    # a real directory, for the other agent type
    doctor.preflight(["claude"])
    assert os.readlink(claude_target) == str(tmp_path) and codex_target.is_dir()
    claude_target.unlink()
    doctor.preflight(["claude"])
    assert os.readlink(claude_target) == str(skill_install.skill_dir())


def test_label_fix_keeps_concurrent_seat(bound, fake_trello, fakebin, monkeypatch):
    """M6: the label fix reloads the configuration under the lock."""
    from kankan import seats
    del fake_trello.state["labels"][bound.cfg.label_question]
    from kankan.trello import TrelloClient
    real_snapshot = TrelloClient.snapshot
    added = []

    def snapshot_and_add_seat(self, board_id):
        if not added:  # a launcher registers a seat after doctor read the configuration
            added.append(True)
            seats.ensure_created(bound, "claude-1", "claude")
        return real_snapshot(self, board_id)
    monkeypatch.setattr(TrelloClient, "snapshot", snapshot_and_add_seat)
    doctor.run(bound.paths.repo, fix=True, online=True)
    assert "claude-1" in load_config(bound.paths).seats


def test_kankan_not_on_path_is_manual(fakebin):
    """Codex's command daemon sees only the login PATH: kankan must be on it."""
    check = doctor.check_kankan_path()
    assert check.status == doctor.MANUAL and "PATH" in check.action


def test_token_owner_as_board_admin_is_manual(bound, fakebin, fake_trello):
    assert by_name(doctor.run(bound.paths.repo, fix=False, online=True))["notifications"].status == doctor.OK
    fake_trello.state["boards"][bound.cfg.board_id]["memberships"] = [
        {"idMember": fake_trello.me["id"], "memberType": "admin"}]
    check = by_name(doctor.run(bound.paths.repo, fix=False, online=True))["notifications"]
    assert check.status == doctor.MANUAL and "own Trello account" in check.action


def test_agents_run_in_full_access_mode_by_default(fakebin):
    """kankan's agents must run without permission prompts or sandbox (they need git, Trello and the network)."""
    from kankan.adapters import load_adapter
    assert "--dangerously-skip-permissions" in load_adapter(None, "claude").command
    assert "--dangerously-bypass-approvals-and-sandbox" in load_adapter(None, "codex").command
    fakebin("claude")
    assert by_name(doctor.check_agents(None, ("claude",)))["full-access:claude"].status == doctor.OK


def test_agent_without_full_access_refuses_to_start(fakebin, monkeypatch):
    monkeypatch.delenv("KANKAN_SKIP_PREFLIGHT")
    monkeypatch.setattr(doctor, "check_kankan_path", lambda: doctor.Check("kankan-path", doctor.OK, "x"))
    fakebin("claude")
    user_agents = Path(os.path.expanduser("~")) / ".config" / "kankan" / "agents.json"
    user_agents.parent.mkdir(parents=True, exist_ok=True)
    user_agents.write_text(json.dumps({"claude": {"command": ["claude"], "seed": {role: {"model": "sonnet", "effort": "high"} for role in ("implementation", "review")}}}))  # override without the flag
    check = by_name(doctor.check_agents(None, ("claude",)))["full-access:claude"]
    assert check.status == doctor.MANUAL and "--dangerously-skip-permissions" in check.action
    with pytest.raises(KankanError) as exc:
        doctor.preflight(["claude"])
    assert exc.value.status == Status.CONFIG_ERROR and "full-access" in exc.value.message


def test_offline_settings_need_no_credentials(bound, monkeypatch, fakebin):
    monkeypatch.delenv('KANKAN_TRELLO_KEY', raising=False)
    monkeypatch.delenv('KANKAN_TRELLO_TOKEN', raising=False)
    checks = by_name(doctor.run(bound.paths.repo, fix=False, online=False))
    assert checks['settings:implementation'].status == doctor.OK
    assert checks['settings:review'].status == doctor.OK


def test_missing_seed_still_reports_recorded_effective_values(bound, fakebin):
    from kankan.config import atomic_write_json
    target = Path.home()/'.config/kankan/agents.json'
    data = json.loads(target.read_text())
    data['claude'].pop('seed')
    target.write_text(json.dumps(data))
    atomic_write_json(bound.paths.kankan_dir/'board-settings.json', {'settings': {
        role: {'value': {'claude': {'model': 'opus', 'effort': 'high'}}} for role in ('implementation', 'review')}})
    checks = by_name(doctor.run(bound.paths.repo, fix=False, online=False))
    assert checks['settings'].status == doctor.MANUAL
    assert 'opus/high' in checks['settings:implementation'].detail
