"""``kankan destroy``: local inventory (destroy spec T§3)."""

import os
import shutil
import subprocess

import pytest

from kankan import destroy, init, messages, seats
from kankan.config import Paths, SeatConfig, load_config, save_config
from kankan.project import Project
from kankan.seatstate import update_state
from kankan.errors import KankanError, Status
from tests.conftest import git

SHORT = "Ab12Cd34"


def with_seats(project, *names, **cfg_changes):
    cfg = load_config(project.paths)
    for name in names:
        cfg.seats[name] = SeatConfig(type=name.split("-")[0])
    for key, value in cfg_changes.items():
        setattr(cfg, key, value)
    save_config(project.paths, cfg)
    return Project.open(project.paths.repo)


def plan_of(repo):
    ctx = destroy.Ctx.open(repo)
    return destroy.local_plan(ctx, destroy.known_seats(ctx))


def seat_wt(kproject, seat="claude-1"):
    project = with_seats(kproject, seat)
    return project, seats.ensure_worktree(project, seat)


def only_target(plan):
    assert len(plan.worktrees) == 1
    return plan.worktrees[0]


def conflicting_commits(project, wt):
    """A ``topic`` commit in the worktree and a ``main`` commit both changing README.md."""
    git(wt, "switch", "-q", "-c", "topic")
    (wt / "README.md").write_text("topic\n")
    git(wt, "commit", "-qam", "topic change")
    (project.paths.repo / "README.md").write_text("main\n")
    git(project.paths.repo, "commit", "-qam", "main change")


def tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*") if not p.is_symlink())


# --- Worktrees ----------------------------------------------------------------------------------------------

def test_dirty_untracked_staged(kproject):
    project, wt = seat_wt(kproject)
    (wt / "new.txt").write_text("x")
    (wt / "README.md").write_text("changed\n")
    (wt / "staged.txt").write_text("s")
    git(wt, "add", "staged.txt")
    plan = plan_of(project.paths.repo)
    target = only_target(plan)
    assert target.path == wt and not target.missing and target.branch is None
    assert sorted(target.dirty) == ["README.md", "new.txt", "staged.txt"]
    assert target.at_risk and not plan.empty


def test_clean_worktree_not_at_risk(kproject):
    project, wt = seat_wt(kproject)
    target = only_target(plan_of(project.paths.repo))
    assert target.dirty == [] and target.operation is None and target.unreachable_total == 0
    assert not target.at_risk
    assert target.admin_dir == project.paths.gcd / "worktrees" / "claude-1"


def test_rebase_in_progress(kproject):
    project, wt = seat_wt(kproject)
    conflicting_commits(project, wt)
    assert subprocess.run(["git", "-C", str(wt), "rebase", "main"], capture_output=True).returncode != 0
    target = only_target(plan_of(project.paths.repo))
    assert target.operation == "rebase" and target.at_risk


def test_cherry_pick_in_progress(kproject):
    project, wt = seat_wt(kproject)
    conflicting_commits(project, wt)
    assert subprocess.run(["git", "-C", str(wt), "cherry-pick", "main"], capture_output=True).returncode != 0
    target = only_target(plan_of(project.paths.repo))
    assert target.operation == "cherry-pick" and target.branch == "topic"


def test_merge_in_progress(kproject):
    project, wt = seat_wt(kproject)
    conflicting_commits(project, wt)
    assert subprocess.run(["git", "-C", str(wt), "merge", "main"], capture_output=True).returncode != 0
    assert only_target(plan_of(project.paths.repo)).operation == "merge"


def test_unreachable_detached_commit(kproject):
    project, wt = seat_wt(kproject)
    (wt / "README.md").write_text("detached\n")
    git(wt, "commit", "-qam", "msg")
    sha7 = git(wt, "rev-parse", "--short=7", "HEAD")
    target = only_target(plan_of(project.paths.repo))
    assert target.branch is None
    assert target.unreachable == [f"{sha7} msg"] and target.unreachable_total == 1 and target.at_risk
    git(wt, "branch", "keep")
    target = only_target(plan_of(project.paths.repo))
    assert target.unreachable == [] and target.unreachable_total == 0 and not target.at_risk


def test_unreachable_capped_at_ten(kproject):
    project, wt = seat_wt(kproject)
    for index in range(12):
        git(wt, "commit", "-q", "--allow-empty", "-m", f"c{index}")
    target = only_target(plan_of(project.paths.repo))
    assert len(target.unreachable) == 10 and target.unreachable_total == 12


def test_ignored_counted_not_at_risk(kproject):
    repo = kproject.paths.repo
    (repo / ".gitignore").write_text("build/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore build")
    project, wt = seat_wt(kproject)
    (wt / "build" / "sub").mkdir(parents=True)
    for index in range(6):
        (wt / "build" / f"f{index}").write_text("o")
    (wt / "build" / "sub" / "x").write_text("o")
    target = only_target(plan_of(project.paths.repo))
    assert target.ignored == 7 and len(target.ignored_examples) == 5
    assert all(example.startswith("build/") for example in target.ignored_examples)
    assert target.dirty == [] and not target.at_risk


def test_custom_root_user_worktree_kept(kproject, tmp_path):
    root = tmp_path / "my root"
    project = with_seats(kproject, "claude-1", worktree_root=str(root))
    wt = seats.ensure_worktree(project, "claude-1")
    assert wt == root / "claude-1"
    git(project.paths.repo, "worktree", "add", "-q", "--detach", str(root / "mine"))
    plan = plan_of(project.paths.repo)
    assert [t.path for t in plan.worktrees] == [wt]
    assert plan.kept_worktrees == [root / "mine"]


def test_blocked_worktree_is_target(kproject):
    project = with_seats(kproject)
    blocked = project.paths.blocked_worktree(SHORT)
    blocked.parent.mkdir(parents=True, exist_ok=True)
    git(project.paths.repo, "worktree", "add", "-q", "--detach", str(blocked))
    invalid = project.paths.worktree_root / "blocked-nope"
    git(project.paths.repo, "worktree", "add", "-q", "--detach", str(invalid))
    plan = plan_of(project.paths.repo)
    assert [t.path for t in plan.worktrees] == [blocked]
    assert plan.kept_worktrees == [invalid]


def test_symlink_alias_not_followed(kproject, tmp_path):
    project, wt = seat_wt(kproject)
    project = with_seats(project, "claude-2")
    elsewhere = tmp_path / "elsewhere"
    git(project.paths.repo, "worktree", "add", "-q", "--detach", str(elsewhere))
    alias = project.paths.worktree_root / "claude-2"
    os.symlink(elsewhere, alias)
    plan = plan_of(project.paths.repo)
    assert [t.path for t in plan.worktrees] == [wt]
    assert plan.kept_worktrees == [alias]


def test_unregistered_directory_kept(kproject):
    project, wt = seat_wt(kproject)
    stray = project.paths.worktree_root / "claude-9"
    stray.mkdir()
    plan = plan_of(project.paths.repo)
    assert [t.path for t in plan.worktrees] == [wt]
    assert plan.kept_worktrees == [stray]


def test_missing_registered_dir(kproject):
    project, wt = seat_wt(kproject)
    shutil.rmtree(wt)
    target = only_target(plan_of(project.paths.repo))
    assert target.path == wt and target.missing
    assert target.admin_dir == project.paths.gcd / "worktrees" / "claude-1" and target.admin_dir.is_dir()
    assert target.dirty == [] and not target.at_risk


def test_inventory_writes_nothing(kproject):
    project, wt = seat_wt(kproject)
    (wt / "README.md").write_text("changed\n")
    os.utime(wt / "README.md", (1, 1))  # a stat mismatch that a status with optional locks would refresh
    update_state(project.paths, "claude-1", lambda s: setattr(s, "block_journal", {"short": SHORT}))
    before = tree(project.paths.gcd), tree(project.paths.worktree_root)
    plan = plan_of(project.paths.repo)
    assert plan.worktrees[0].dirty == ["README.md"]
    assert (tree(project.paths.gcd), tree(project.paths.worktree_root)) == before


# --- Interrupted blocks -------------------------------------------------------------------------------------

def snapshot(project, *files):
    snap = project.paths.kankan_dir / "blocks" / SHORT
    for name in files:
        (snap / "files" / name).parent.mkdir(parents=True, exist_ok=True)
        (snap / "files" / name).write_text("work")
    (snap / "index").parent.mkdir(parents=True, exist_ok=True)
    (snap / "index").write_text("idx")
    return snap


def journal(project, **extra):
    update_state(project.paths, "claude-1", lambda s: setattr(s, "block_journal", {
        "card": "c" * 24, "short": SHORT, "branch": "26kankan-" + SHORT, "merging": True, "done": [], **extra}))


def test_block_journal_only(kproject):
    project = with_seats(kproject, "claude-1")
    journal(project)
    plan = plan_of(project.paths.repo)
    assert len(plan.blocks) == 1
    block = plan.blocks[0]
    assert (block.seat, block.short, block.snapshot, block.files) == ("claude-1", SHORT, None, [])
    assert not plan.empty


def test_block_snapshot_only(kproject):
    project = with_seats(kproject)
    snap = snapshot(project, "a.txt", "dir/b.txt")
    plan = plan_of(project.paths.repo)
    assert len(plan.blocks) == 1
    block = plan.blocks[0]
    assert block.seat is None and block.short == SHORT and block.snapshot == snap
    assert block.files == ["a.txt", "dir/b.txt"]


def test_block_both(kproject):
    project = with_seats(kproject, "claude-1")
    snap = snapshot(project, "a.txt")
    journal(project, done=["snapshot"], files=["a.txt", "gone.txt"])
    plan = plan_of(project.paths.repo)
    assert len(plan.blocks) == 1
    block = plan.blocks[0]
    assert (block.seat, block.short, block.snapshot, block.files) == ("claude-1", SHORT, snap, ["a.txt", "gone.txt"])


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "[]", "false", "null", "0", '""', '{"block_journal": {"short": "../x"}}',
                                     '{"block_journal": "text"}', '{"block_journal": {"short": "Ab12Cd34", "files": 3}}'])
def test_block_indeterminate_seat_state_needs_action(kproject, content):
    project = with_seats(kproject, "claude-1")
    project.paths.seats_dir.mkdir(parents=True, exist_ok=True)
    (project.paths.seats_dir / "claude-1.state.json").write_text(content)
    with pytest.raises(KankanError) as caught:
        plan_of(project.paths.repo)
    assert caught.value.status == Status.NEEDS_ACTION and "claude-1.state.json" in caught.value.message


def test_block_absent_journal_is_no_block(kproject):
    project = with_seats(kproject, "claude-1")
    project.paths.seats_dir.mkdir(parents=True, exist_ok=True)
    (project.paths.seats_dir / "claude-1.state.json").write_text('{"block_journal": null}')
    assert plan_of(project.paths.repo).blocks == []


# --- Refs, hook, configuration, exclude ---------------------------------------------------------------------

def test_refs_listed(kproject):
    from tests.helpers import put_card_ref
    value = put_card_ref(kproject, "a" * 24, SHORT, phase="impl")
    plan = plan_of(kproject.paths.repo)
    assert plan.refs == {"refs/agent-cards/" + "a" * 24: value.oid}
    assert plan.symbolic_refs == []


def test_symbolic_ref_reported(kproject):
    repo = kproject.paths.repo
    git(repo, "symbolic-ref", "refs/agent-seats/x", "refs/heads/main")
    plan = plan_of(repo)
    assert plan.symbolic_refs == ["refs/agent-seats/x"]
    assert not plan.empty


def test_hook_kinds(kproject):
    paths = kproject.paths
    hook = init.hook_path(paths)
    assert plan_of(paths.repo).hook == "absent"
    init.install_hook(paths)
    plan = plan_of(paths.repo)
    assert plan.hook == "kankan" and plan.hook_path == hook
    data = bytearray(hook.read_bytes())
    data[-2] = ord("X") if data[-2] != ord("X") else ord("Y")
    hook.write_bytes(bytes(data))
    assert plan_of(paths.repo).hook == "foreign"
    hook.unlink()
    hook.mkdir()
    assert plan_of(paths.repo).hook == "foreign"


def test_hook_unreadable_is_foreign(kproject, monkeypatch):
    init.install_hook(kproject.paths)
    hook = init.hook_path(kproject.paths)
    real = type(hook).read_bytes

    def denied(self):
        if self == hook:
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(type(hook), "read_bytes", denied)
    assert plan_of(kproject.paths.repo).hook == "foreign"


def test_git_config(git_repo):
    assert plan_of(git_repo).git_config is None
    git(git_repo, "config", "--local", "kankan.integrationBranch", "agents-x")
    assert plan_of(git_repo).git_config == "agents-x"


def test_exclude_pair_only(kproject):
    paths = kproject.paths
    exclude = paths.gcd / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# mine\n/.kankan/\n")
    assert plan_of(paths.repo).exclude is False
    exclude.write_text("/.kankan/\n# kankan\n")
    assert plan_of(paths.repo).exclude is False
    exclude.write_text("*.o\n")
    init.add_excludes(paths)
    assert plan_of(paths.repo).exclude is True


# --- State and emptiness ------------------------------------------------------------------------------------

def test_state_keeps_lock_files(kproject):
    kdir = kproject.paths.kankan_dir
    for name in ("attachments/Ab12Cd34/x/yarn.lock", "project.lock", "seats/claude-1.lock",
                 "seats/claude-1.state.lock", "attachment-locks/Ab12Cd34.lock", "qa/c1/op.lock", "qa/c1/record.json",
                 "seats/sub/deep.lock", "qa/op.lock"):
        (kdir / name).parent.mkdir(parents=True, exist_ok=True)
        (kdir / name).write_text("")
    state = plan_of(kproject.paths.repo).state
    assert all(path.is_absolute() for path in state)
    rel = {str(path.relative_to(kdir)) for path in state}
    assert {"config.json", "attachments/Ab12Cd34/x/yarn.lock", "qa/c1/record.json", "seats/sub/deep.lock",
            "qa/op.lock"} <= rel
    for name in ("project.lock", "seats/claude-1.lock", "seats/claude-1.state.lock", "attachment-locks/Ab12Cd34.lock",
                 "qa/c1/op.lock"):
        assert name not in rel


def test_state_empty_directory_listed(git_repo):
    kdir = Paths.discover(git_repo).kankan_dir
    (kdir / "blocks").mkdir(parents=True)
    plan = plan_of(git_repo)
    assert plan.state == [kdir / "blocks"] and not plan.empty


def test_empty_with_only_lock_files(git_repo):
    paths = Paths.discover(git_repo)
    for name in ("qa/c1/op.lock", "seats/claude-1.lock", "project.lock", "attachment-locks/Ab12Cd34.lock"):
        (paths.kankan_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (paths.kankan_dir / name).write_text("")
    init.install_hook(paths)
    hook = init.hook_path(paths)
    hook.write_text("#!/bin/sh\nexit 0\n")  # a foreign hook is kept and does not count
    plan = plan_of(git_repo)
    assert plan.state == [] and plan.hook == "foreign" and plan.integration is None
    assert plan.empty


def test_unbound_clone_empty(git_repo):
    plan = plan_of(git_repo)
    assert plan.empty and plan.worktrees == [] and plan.kept_worktrees == [] and plan.state == []


# --- Integration branch -------------------------------------------------------------------------------------

def bare_origin(repo, tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    return origin


def name_branch(repo, name="kankan-agents"):
    git(repo, "config", "--local", "kankan.integrationBranch", name)


def test_integration_local_only(git_repo):
    git(git_repo, "branch", "kankan-agents")
    integration = plan_of(git_repo).integration
    assert (integration.name, integration.local, integration.origin) == ("kankan-agents", True, False)
    assert integration.default and not integration.is_user_branch
    assert integration.commands == ["git branch -m kankan-agents kankan-agents-old"]


def test_integration_remote_only(git_repo, tmp_path):
    bare_origin(git_repo, tmp_path)
    git(git_repo, "push", "-q", "origin", "main:refs/heads/kankan-agents")
    git(git_repo, "update-ref", "-d", "refs/remotes/origin/kankan-agents")
    integration = plan_of(git_repo).integration
    assert (integration.local, integration.origin) == (False, True)
    assert integration.commands == ["git fetch origin refs/heads/kankan-agents",
                                    "git push origin FETCH_HEAD:refs/heads/kankan-agents-old",
                                    "git push origin --delete kankan-agents"]


def test_integration_both(git_repo, tmp_path):
    bare_origin(git_repo, tmp_path)
    git(git_repo, "branch", "kankan-agents")
    git(git_repo, "push", "-q", "origin", "kankan-agents")
    integration = plan_of(git_repo).integration
    assert (integration.local, integration.origin) == (True, True)
    assert integration.commands == ["git branch -m kankan-agents kankan-agents-old",
                                    "git push origin kankan-agents-old", "git push origin --delete kankan-agents"]


def test_integration_foreign_hook_note(git_repo, tmp_path):
    bare_origin(git_repo, tmp_path)
    git(git_repo, "branch", "kankan-agents")
    git(git_repo, "push", "-q", "origin", "kankan-agents")
    paths = Paths.discover(git_repo)
    hook = init.hook_path(paths)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    commands = plan_of(git_repo).integration.commands
    assert commands[-1] == messages.DESTROY_INTEGRATION_HOOK_NOTE.format(path=hook)
    hook.write_text(init.hook_content())
    assert messages.DESTROY_INTEGRATION_HOOK_NOTE.format(path=hook) not in plan_of(git_repo).integration.commands


def test_integration_unreachable_origin(git_repo, tmp_path):
    git(git_repo, "remote", "add", "origin", str(tmp_path / "nowhere" / "repo.git"))
    git(git_repo, "branch", "kankan-agents")
    integration = plan_of(git_repo).integration
    assert integration.origin == "unknown" and integration.local
    assert integration.commands == ["git branch -m kankan-agents kankan-agents-old"]
    git(git_repo, "branch", "-D", "kankan-agents")
    integration = plan_of(git_repo).integration
    assert integration is not None and (integration.local, integration.origin) == (False, "unknown")
    assert integration.commands == []


def test_integration_timeout_unknown(git_repo, tmp_path, monkeypatch):
    bare_origin(git_repo, tmp_path)
    real = destroy.gitutil.run

    def slow(repo, args, **kwargs):
        if "ls-remote" in args:
            assert kwargs.get("timeout") == 10
            raise subprocess.TimeoutExpired(args, 10)
        return real(repo, args, **kwargs)

    monkeypatch.setattr(destroy.gitutil, "run", slow)
    git(git_repo, "branch", "kankan-agents")
    assert plan_of(git_repo).integration.origin == "unknown"


def test_integration_is_user_branch(kproject):
    integration = plan_of(kproject.paths.repo).integration  # kproject: integration_branch == user_branch == main
    assert integration.name == "main" and integration.local and integration.is_user_branch
    assert integration.commands == [] and not integration.default


def test_integration_custom_name(git_repo):
    name_branch(git_repo, "agents-x")
    git(git_repo, "branch", "agents-x")
    integration = plan_of(git_repo).integration
    assert integration.name == "agents-x" and integration.default is False


def test_integration_nested_remote_name_not_present(git_repo, tmp_path):
    bare_origin(git_repo, tmp_path)
    name_branch(git_repo, "zz")
    git(git_repo, "push", "-q", "origin", "main:refs/heads/team/zz")
    assert plan_of(git_repo).integration is None


def test_integration_absent(git_repo):
    assert plan_of(git_repo).integration is None


def test_ref_listing_failure_propagates(git_repo, monkeypatch):
    from kankan.errors import KankanError, Status
    ctx = destroy.Ctx.open(git_repo)

    def broken(prefix):
        raise KankanError(Status.INTERNAL_ERROR, "git for-each-ref failed")

    monkeypatch.setattr(ctx.refs, "list_raw", broken)
    with pytest.raises(KankanError):
        destroy.local_plan(ctx, [])


def test_unreadable_seat_state_needs_action(kproject, monkeypatch):
    from kankan.errors import KankanError, Status
    project = with_seats(kproject, "claude-1")
    journal(project)

    def denied(paths, seat):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(destroy, "read_json", denied)
    with pytest.raises(KankanError) as caught:
        plan_of(project.paths.repo)
    assert caught.value.status == Status.NEEDS_ACTION
    assert "claude-1.state.json" in caught.value.message and "Permission denied" in caught.value.message


def test_unreadable_state_directory_needs_action(kproject, monkeypatch):
    from kankan.errors import KankanError, Status
    real = os.scandir

    def denied(path="."):
        if str(path).endswith(os.sep + "kankan"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(KankanError) as caught:
        plan_of(kproject.paths.repo)
    assert caught.value.status == Status.NEEDS_ACTION


def test_replaced_directory_kept(kproject):
    project, wt = seat_wt(kproject)
    shutil.rmtree(wt)
    wt.mkdir()
    (wt / "mine.txt").write_text("user data")
    plan = plan_of(project.paths.repo)
    assert plan.worktrees == [] and plan.kept_worktrees == [wt]


def test_independent_clone_at_registered_path_kept(kproject, tmp_path):
    project, wt = seat_wt(kproject)
    shutil.rmtree(wt)
    git(tmp_path, "clone", "-q", str(project.paths.repo), str(wt))
    plan = plan_of(project.paths.repo)
    assert plan.worktrees == [] and plan.kept_worktrees == [wt]


# --- Review round 1 -----------------------------------------------------------------------------------------

def test_symlinked_state_root_needs_action(git_repo, tmp_path, monkeypatch):
    import builtins
    outside = tmp_path / "shared-config"
    (outside / "seats").mkdir(parents=True)
    (outside / "credentials").write_text("secret")
    (outside / "seats" / "claude-1.state.json").write_text('{"block_journal": null}')
    (outside / "blocks" / SHORT / "files").mkdir(parents=True)
    kdir = Paths.discover(git_repo).kankan_dir
    os.symlink(outside, kdir)
    ctx = destroy.Ctx.open(git_repo)
    seat_names = destroy.known_seats(ctx)
    touched = []
    marker = str(outside)

    def recorder(real, name):
        def wrapper(path, *args, **kwargs):
            text = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
            # only the guard's own lstat of the link is allowed
            no_follow = name == "lstat" or kwargs.get("follow_symlinks") is False
            if text.startswith((marker, str(kdir) + os.sep)) or (text == str(kdir) and not no_follow):
                touched.append(f"{name} {text}")
            return real(path, *args, **kwargs)
        return wrapper

    for module, name in ((builtins, "open"), (os, "scandir"), (os, "listdir"), (os, "lstat"), (os, "stat")):
        monkeypatch.setattr(module, name, recorder(getattr(module, name), name))
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(ctx, seat_names)
    monkeypatch.undo()
    assert caught.value.status == Status.NEEDS_ACTION and "symbolic link" in caught.value.message
    assert f"rm {kdir}" in caught.value.message and "stays in place" in caught.value.message
    assert touched == []
    assert (outside / "credentials").read_text() == "secret"


def test_missing_detached_with_unreachable_commit(kproject):
    project, wt = seat_wt(kproject)
    (wt / "README.md").write_text("detached\n")
    git(wt, "commit", "-qam", "lost")
    sha7 = git(wt, "rev-parse", "--short=7", "HEAD")
    shutil.rmtree(wt)
    target = only_target(plan_of(project.paths.repo))
    assert target.missing and target.branch is None
    assert target.unreachable == [f"{sha7} lost"] and target.unreachable_total == 1 and target.at_risk


def test_missing_on_branch_and_operation(kproject):
    project, wt = seat_wt(kproject)
    git(wt, "switch", "-q", "-c", "topic")
    shutil.rmtree(wt)
    admin = project.paths.gcd / "worktrees" / "claude-1"
    (admin / "MERGE_HEAD").write_text(git(project.paths.repo, "rev-parse", "main") + "\n")
    target = only_target(plan_of(project.paths.repo))
    assert target.missing and target.branch == "topic" and target.unreachable_total == 0
    assert target.operation == "merge" and target.at_risk


def test_missing_invalid_admin_head_needs_action(kproject):
    project, wt = seat_wt(kproject)
    shutil.rmtree(wt)
    (project.paths.gcd / "worktrees" / "claude-1" / "HEAD").write_text("garbage\n")
    with pytest.raises(KankanError) as caught:
        plan_of(project.paths.repo)
    assert caught.value.status == Status.NEEDS_ACTION


def test_rev_list_failure_needs_action(kproject, monkeypatch):
    project, wt = seat_wt(kproject)
    real = destroy.gitutil.run_bytes

    def failing(repo, args, **kwargs):
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: bad object")
        return real(repo, args, **kwargs)

    monkeypatch.setattr(destroy.gitutil, "run_bytes", failing)
    with pytest.raises(KankanError) as caught:
        plan_of(project.paths.repo)
    assert caught.value.status == Status.NEEDS_ACTION and "rev-list" in caught.value.message


def test_fsmonitor_hook_not_run(kproject, tmp_path):
    project, wt = seat_wt(kproject)
    sentinel = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n")
    hook.chmod(0o755)
    git(project.paths.repo, "config", "core.fsmonitor", str(hook))
    (wt / "new.txt").write_text("x")
    assert only_target(plan_of(project.paths.repo)).dirty == ["new.txt"]
    assert not sentinel.exists()


def test_status_names_are_raw_bytes(kproject):
    project, wt = seat_wt(kproject)
    with open(os.path.join(os.fsencode(wt), b"bad\xff.txt"), "wb") as handle:
        handle.write(b"x")
    (wt / "cr\rname.txt").write_text("x")
    dirty = only_target(plan_of(project.paths.repo)).dirty
    assert sorted(dirty) == sorted([os.fsdecode(b"bad\xff.txt"), "cr\rname.txt"])


def test_dangling_symbolic_ref_reported(kproject):
    repo = kproject.paths.repo
    git(repo, "symbolic-ref", "refs/agent-cards/y", "refs/heads/gone")
    plan = plan_of(repo)
    assert plan.symbolic_refs == ["refs/agent-cards/y"] and not plan.empty


def test_git_config_empty_value_counts(git_repo):
    git(git_repo, "config", "--local", "kankan.integrationBranch", "")
    plan = plan_of(git_repo)
    assert plan.git_config == "" and not plan.empty


def test_git_config_failure_needs_action(git_repo, monkeypatch):
    ctx = destroy.Ctx.open(git_repo)
    real = destroy.gitutil.run_bytes

    def failing(repo, args, **kwargs):
        if args[:2] == ["config", "--file"] and args[-1] == "kankan.integrationBranch":
            return subprocess.CompletedProcess(args, 3, b"", b"error: invalid config file")
        return real(repo, args, **kwargs)

    monkeypatch.setattr(destroy.gitutil, "run_bytes", failing)
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(ctx, [])
    assert caught.value.status == Status.NEEDS_ACTION


def test_remote_listing_failure_unknown(git_repo, monkeypatch):
    git(git_repo, "branch", "kankan-agents")
    ctx = destroy.Ctx.open(git_repo)
    real = destroy.gitutil.run

    def failing(repo, args, **kwargs):
        if args == ["remote"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad config")
        return real(repo, args, **kwargs)

    monkeypatch.setattr(destroy.gitutil, "run", failing)
    assert destroy.local_plan(ctx, []).integration.origin == "unknown"


# --- Review round 2 -----------------------------------------------------------------------------------------

def test_ancestor_checkout_not_discovered(kproject):
    project, wt = seat_wt(kproject)
    admin = project.paths.gcd / "worktrees" / "claude-1"
    root = project.paths.worktree_root
    # the checkout was moved up to the root by hand, and a user directory was recreated at the registered path
    (root / ".git").write_text(f"gitdir: {admin}\n")
    shutil.rmtree(wt)
    wt.mkdir()
    (wt / "mine.txt").write_text("user data")
    plan = plan_of(project.paths.repo)
    assert plan.worktrees == [] and wt in plan.kept_worktrees


def test_loose_ref_scan_skips_fifo_and_invalid_names(kproject):
    refs_dir = kproject.paths.gcd / "refs" / "agent-cards"
    refs_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(refs_dir / "x.lock")
    seats_dir = kproject.paths.gcd / "refs" / "agent-seats"
    seats_dir.mkdir(parents=True, exist_ok=True)
    (seats_dir / "bad.lock").write_text("ref: refs/heads/main\n")
    assert plan_of(kproject.paths.repo).symbolic_refs == []


def fake_show_ref_format(monkeypatch, returncode, stdout, stderr=b""):
    real = destroy.gitutil.run_bytes
    calls = []

    def patched(repo, args, **kwargs):
        calls.append(args)
        if args == ["rev-parse", "--show-ref-format"]:
            return subprocess.CompletedProcess(args, returncode, stdout, stderr)
        return real(repo, args, **kwargs)

    monkeypatch.setattr(destroy.gitutil, "run_bytes", patched)
    return calls


def test_reftable_refused(git_repo, monkeypatch):
    ctx = destroy.Ctx.open(git_repo)
    calls = fake_show_ref_format(monkeypatch, 0, b"reftable\n")
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(ctx, [])
    assert caught.value.status == Status.NEEDS_ACTION and "reftable" in caught.value.message
    assert calls == [["rev-parse", "--show-ref-format"]]  # refused before anything else


def test_show_ref_format_files(git_repo, monkeypatch):
    ctx = destroy.Ctx.open(git_repo)
    fake_show_ref_format(monkeypatch, 0, b"files\n")
    assert destroy.local_plan(ctx, []).empty


@pytest.mark.parametrize("answer", [(0, b"--show-ref-format\n", b""),
                                    (129, b"", b"error: unknown option `show-ref-format'\nusage: git rev-parse")])
def test_old_git_falls_back_to_repository_config(git_repo, monkeypatch, answer):
    ctx = destroy.Ctx.open(git_repo)
    fake_show_ref_format(monkeypatch, *answer)
    assert destroy.local_plan(ctx, []).empty  # no extensions.refStorage: files
    git(git_repo, "config", "--file", str(ctx.paths.gcd / "config"), "extensions.refStorage", "reftable")
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(ctx, [])
    assert caught.value.status == Status.NEEDS_ACTION and "reftable" in caught.value.message


def test_ref_format_ignores_git_config_env(git_repo, tmp_path, monkeypatch):
    other = tmp_path / "other.config"
    other.write_text("[extensions]\n\trefStorage = reftable\n")
    monkeypatch.setenv("GIT_CONFIG", str(other))
    ctx = destroy.Ctx.open(git_repo)
    fake_show_ref_format(monkeypatch, 0, b"--show-ref-format\n")  # the fallback path, whatever git is installed
    assert destroy.local_plan(ctx, []).empty


def test_ref_format_failure_needs_action(git_repo, monkeypatch):
    ctx = destroy.Ctx.open(git_repo)
    fake_show_ref_format(monkeypatch, 128, b"", b"fatal: not a git repository")
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(ctx, [])
    assert caught.value.status == Status.NEEDS_ACTION and "rev-parse" in caught.value.message


def test_reftable_repository_refused(tmp_path):
    repo = tmp_path / "rt"
    proc = subprocess.run(["git", "init", "-q", "--ref-format=reftable", str(repo)], capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("this git does not support the reftable format")
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(destroy.Ctx.open(repo), [])
    assert caught.value.status == Status.NEEDS_ACTION and "reftable" in caught.value.message


# --- Review round 3 -----------------------------------------------------------------------------------------

def test_unreadable_invalid_ref_name_ignored(kproject, monkeypatch):
    import builtins
    seats_dir = kproject.paths.gcd / "refs" / "agent-seats"
    seats_dir.mkdir(parents=True, exist_ok=True)
    (seats_dir / "bad.lock").write_text("ref: refs/heads/main\n")
    ctx = destroy.Ctx.open(kproject.paths.repo)
    real = builtins.open

    def denied(path, *args, **kwargs):
        if str(path).endswith("bad.lock"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    assert destroy.local_plan(ctx, destroy.known_seats(ctx)).symbolic_refs == []


def test_dangling_symlink_ref_reported(kproject):
    seats_dir = kproject.paths.gcd / "refs" / "agent-seats"
    seats_dir.mkdir(parents=True, exist_ok=True)
    os.symlink("refs/heads/gone", seats_dir / "claude-5")
    plan = plan_of(kproject.paths.repo)
    assert plan.symbolic_refs == ["refs/agent-seats/claude-5"] and not plan.empty


# --- Fix round 4 --------------------------------------------------------------------------------------------

def test_kankan_ref_name_with_trailing_newline_invalid(git_repo):
    assert destroy._valid_ref_name(git_repo, "refs/agent-seats/claude-5")
    assert not destroy._valid_ref_name(git_repo, "refs/agent-seats/claude-5\n")
    assert not destroy._valid_ref_name(git_repo, "refs/agent-cards/" + "a" * 24 + "\n")


def test_custom_integration_branch_under_git_config_env(git_repo, tmp_path, monkeypatch):
    bare_origin(git_repo, tmp_path)
    name_branch(git_repo, "agents-x")
    git(git_repo, "branch", "agents-x")
    git(git_repo, "branch", "kankan-agents")
    git(git_repo, "push", "-q", "origin", "agents-x", "kankan-agents")
    assert git(git_repo, "rev-parse", "--verify", "-q", "refs/remotes/origin/kankan-agents")
    other = tmp_path / "other.config"
    other.write_text("[kankan]\n\tintegrationBranch = kankan-agents\n")
    monkeypatch.setenv("GIT_CONFIG", str(other))
    ctx = destroy.Ctx.open(git_repo)
    assert ctx.refs.integration_branch == "agents-x"
    plan = destroy.local_plan(ctx, destroy.known_seats(ctx))
    assert plan.git_config == "agents-x"
    integration = plan.integration
    assert integration.name == "agents-x" and (integration.local, integration.origin) == (True, True)
    assert integration.commands == ["git branch -m agents-x agents-x-old", "git push origin agents-x-old",
                                    "git push origin --delete agents-x"]


# --- Fix round 5 --------------------------------------------------------------------------------------------

def test_integration_branch_not_utf8_needs_action(git_repo, tmp_path):
    bare_origin(git_repo, tmp_path)
    git(git_repo, "branch", "kankan-agents")
    git(git_repo, "push", "-q", "origin", "kankan-agents")
    config = git_repo / ".git" / "config"
    subprocess.run([b"git", b"config", b"--file", os.fsencode(config), b"kankan.integrationBranch",
                    b"agents-\xff"], check=True)
    ctx = destroy.Ctx.open(git_repo)  # never raises for configuration problems
    assert "�" not in ctx.refs.integration_branch
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(ctx, destroy.known_seats(ctx))
    assert caught.value.status == Status.NEEDS_ACTION
    assert caught.value.message == messages.DESTROY_INTEGRATION_BRANCH_NOT_UTF8.format(path=config)


# --- Carried ruling (Task 5) --------------------------------------------------------------------------------

def test_integration_branch_fallback_then_fixed_needs_action(git_repo, monkeypatch):
    """``Ctx.open`` reads ``agents-\\xff`` and falls back; ``local_plan`` reads ``agents-x``: the commands must never
    name the fallback branch, so the plan stops instead."""
    real = destroy._read_git_config
    values = [b"agents-\xff\n", b"agents-x\n"]

    def successive(paths):
        proc = real(paths)
        value = values.pop(0) if values else b"agents-x\n"
        return subprocess.CompletedProcess(proc.args, 0, value, b"")
    monkeypatch.setattr(destroy, "_read_git_config", successive)
    git(git_repo, "branch", "kankan-agents")
    git(git_repo, "branch", "agents-x")
    ctx = destroy.Ctx.open(git_repo)
    assert ctx.refs.integration_branch == "kankan-agents"
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(ctx, destroy.known_seats(ctx))
    assert caught.value.status == Status.NEEDS_ACTION
    assert caught.value.message == messages.DESTROY_INTEGRATION_BRANCH_NOT_UTF8.format(
        path=git_repo / ".git" / "config")


def test_integration_branch_changed_between_reads_needs_action(git_repo, monkeypatch):
    git(git_repo, "config", "--local", "kankan.integrationBranch", "agents-a")
    git(git_repo, "branch", "agents-a")
    ctx = destroy.Ctx.open(git_repo)
    assert ctx.refs.integration_branch == "agents-a"
    git(git_repo, "config", "--local", "kankan.integrationBranch", "agents-b")
    with pytest.raises(KankanError) as caught:
        destroy.local_plan(ctx, destroy.known_seats(ctx))
    assert caught.value.status == Status.NEEDS_ACTION
    assert caught.value.message == messages.DESTROY_INTEGRATION_BRANCH_CHANGED


# --- Fix round 4 --------------------------------------------------------------------------------------------

def external_tree(tmp_path):
    outside = tmp_path / "outside"
    (outside / "secret").mkdir(parents=True)
    (outside / "secret" / "s.txt").write_text("s")
    (outside / "locked").mkdir()
    (outside / "locked" / "l.txt").write_text("l")
    os.chmod(outside / "locked", 0)
    return outside


def record_reads(monkeypatch):
    seen = []
    real_walk, real_scandir, real_listdir = os.walk, os.scandir, os.listdir

    def walk(top, *args, **kwargs):
        seen.append(os.fspath(top))
        return real_walk(top, *args, **kwargs)

    def scandir(path="."):
        seen.append(os.fspath(path))
        return real_scandir(path)

    def listdir(path="."):
        seen.append(os.fspath(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    return seen


@pytest.mark.parametrize("with_journal", [False, True])
def test_block_snapshot_files_link_is_never_followed(kproject, tmp_path, monkeypatch, with_journal):
    project = with_seats(kproject, "claude-1")
    outside = external_tree(tmp_path)
    try:
        snap = snapshot(project)
        os.symlink(outside, snap / "files")
        if with_journal:
            journal(project, files=["a.txt"])
        seen = record_reads(monkeypatch)
        plan = plan_of(project.paths.repo)
        assert not any(os.path.realpath(path).startswith(str(outside)) or "/files" in path for path in seen)
        assert len(plan.blocks) == 1
        block = plan.blocks[0]
        assert block.snapshot == snap and block.files_link
        assert block.files == (["a.txt"] if with_journal else [])
        text = "\n".join(destroy._block_risk(block))
        assert messages.DESTROY_RISK_BLOCK_FILES_LINK in text and "s.txt" not in text
        assert not plan.empty
    finally:
        os.chmod(outside / "locked", 0o700)
