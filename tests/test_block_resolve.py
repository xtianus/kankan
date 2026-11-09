import os

import pytest

from kankan import faults, integrate, seats, workflow
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref
from kankan.seatstate import load_state
from tests.conftest import git
from tests.flow import agent_ctx, commit, review_setup

BRANCH_FILES = {"shared.txt": "branch\n", "other.txt": "branch other\n", "keep.txt": "keep\n", "tool.sh": "echo\n"}


def advance_main(project, changes):
    repo = project.paths.repo
    git(repo, "checkout", "-q", "--detach")
    wt = project.paths.worktree_root / "main-editor"
    git(repo, "worktree", "add", "-q", str(wt), "main")
    for name, text in changes.items():
        (wt / name).write_text(text)
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "main change")
    git(repo, "worktree", "remove", str(wt))


@pytest.fixture
def conflicted(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch, files=BRANCH_FILES)
    advance_main(project, {"shared.txt": "main\n", "other.txt": "main other\n"})
    with pytest.raises(KankanError):
        integrate.sync(ctx)
    assert sorted(integrate.conflicts(ctx.worktree)) == ["other.txt", "shared.txt"]
    return project, fake, card, codex, ctx


def state_of(worktree):
    status = git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    files = {}
    for path in sorted(p for p in os.listdir(worktree) if p != ".git"):
        full = worktree / path
        if full.is_symlink():
            files[path] = ("link", os.readlink(full))
        elif full.is_file():
            files[path] = (full.read_text(), os.stat(full).st_mode & 0o777)
    return status, files, git(worktree, "diff", "--cached", "--binary"), git(worktree, "diff", "--binary")


VARIANTS = {
    "staged_only": lambda wt: ((wt / "shared.txt").write_text("resolved\n"), git(wt, "add", "shared.txt")),
    "staged_and_unstaged": lambda wt: ((wt / "shared.txt").write_text("resolved\n"), git(wt, "add", "shared.txt"),
                                       (wt / "shared.txt").write_text("resolved, then edited\n")),
    "deletion": lambda wt: git(wt, "rm", "-q", "keep.txt"),
    "symlink": lambda wt: ((wt / "link").symlink_to("keep.txt"), (wt / "new.txt").write_text("untracked\n")),
    "exec_bit": lambda wt: os.chmod(wt / "tool.sh", 0o755),
}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_block_transfer_preserves(conflicted, variant):
    project, fake, card, codex, ctx = conflicted
    VARIANTS[variant](ctx.worktree)
    before = state_of(ctx.worktree)
    main_before = git(project.paths.repo, "rev-parse", "main")
    result = integrate.block(ctx, "cannot merge safely")
    blocked = project.paths.blocked_worktree(card["shortLink"])
    assert result["blocked_worktree"] == str(blocked)
    assert state_of(blocked) == before
    assert git(ctx.worktree, "status", "--porcelain") == ""
    assert git(ctx.worktree, "symbolic-ref", "-q", "HEAD", check=False) == ""
    assert git(project.paths.repo, "rev-parse", "main") == main_before
    assert fake.card_list(card["id"]) == project.cfg.blocked
    assert load_state(project.paths, "codex-1").block_journal is None


@pytest.mark.parametrize("step", ["snapshot", "abort", "blocked_wt", "restore", "transition"])
def test_block_crash_at_each_step_resumes(conflicted, monkeypatch, step):
    project, fake, card, codex, ctx = conflicted
    VARIANTS["staged_and_unstaged"](ctx.worktree)
    before = state_of(ctx.worktree)
    monkeypatch.setenv("KANKAN_FAULT", step)
    with pytest.raises(faults.SimulatedCrash):
        integrate.block(ctx, "stuck")
    monkeypatch.delenv("KANKAN_FAULT")
    integrate.block(ctx, "stuck")
    blocked = project.paths.blocked_worktree(card["shortLink"])
    assert state_of(blocked) == before
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "parked"
    assert fake.card_list(card["id"]) == project.cfg.blocked
    comments = [c for c in fake.card_comments(card["id"]) if "To resolve: kankan resolve" in c]
    assert len(comments) == 1 and "@boardowner" in comments[0]


def test_block_with_merge_identical_state_and_resolve(conflicted, monkeypatch, tmux_server):
    project, fake, card, codex, ctx = conflicted
    short = card["shortLink"]
    integrate.block(ctx, "conflict in shared.txt")
    blocked = project.paths.blocked_worktree(short)
    data, message = integrate.resolve(project, short, False, tmux=tmux_server)
    assert sorted(data["conflicts"]) == ["other.txt", "shared.txt"] and "--done" in message
    assert tmux_server.has_session(f"kankan-resolve-proj-{short}")
    with pytest.raises(KankanError) as exc:
        integrate.resolve(project, short, True)
    assert exc.value.status == Status.NEEDS_ACTION
    (blocked / "shared.txt").write_text("merged\n")
    (blocked / "other.txt").write_text("merged other\n")
    git(blocked, "add", ".")
    git(blocked, "commit", "-qm", "resolve conflicts")
    integrate.resolve(project, short, True)
    assert not blocked.exists()
    assert fake.card_list(card["id"]) == project.reload_config().seats["claude-1"].implemented
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "implemented"
    task = workflow.tick(project, codex)
    assert task["card"] == card["id"] and task["kind"] == "review"
    ctx2 = agent_ctx(project, codex, monkeypatch)
    integrate.sync(ctx2)
    merge = integrate.integrate(ctx2, git(ctx2.worktree, "rev-parse", "HEAD"))["merge"]
    assert git(project.paths.repo, "rev-parse", "main") == merge


def test_block_without_merge(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    (ctx.worktree / "dirty.txt").write_text("x")
    with pytest.raises(KankanError) as exc:
        integrate.block(ctx, "x")
    assert exc.value.status == Status.NEEDS_ACTION
    os.unlink(ctx.worktree / "dirty.txt")
    result = integrate.block(ctx, "spec unclear")
    assert result["blocked_worktree"] is None
    assert fake.card_list(card["id"]) == project.cfg.blocked
    data, message = integrate.resolve(project, card["shortLink"], False)
    assert "nothing to merge" in message
    integrate.resolve(project, card["shortLink"], True)
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "implemented"
