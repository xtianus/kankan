import pytest

from kankan import integrate, procutil
from kankan.errors import KankanError, Status
from kankan.seatstate import load_state
from tests.conftest import git
from tests.flow import commit, review_setup


@pytest.fixture
def review(kproject, fake_trello, monkeypatch):
    card, codex, ctx = review_setup(kproject, fake_trello, monkeypatch, files={"shared.txt": "branch\n"})
    return kproject, fake_trello, card, codex, ctx


def advance_main(project, name, text):
    repo = project.paths.repo
    git(repo, "checkout", "-q", "--detach")
    wt = project.paths.worktree_root / "main-editor"
    git(repo, "worktree", "add", "-q", str(wt), "main")
    commit(wt, name, text)
    git(repo, "worktree", "remove", str(wt))


def test_sync_merges_main(review):
    project, fake, card, codex, ctx = review
    advance_main(project, "other.txt", "x\n")
    result = integrate.sync(ctx)
    assert (ctx.worktree / "other.txt").exists() and result["head"]


def test_sync_conflict_reports_files(review):
    project, fake, card, codex, ctx = review
    advance_main(project, "shared.txt", "main\n")
    with pytest.raises(KankanError) as exc:
        integrate.sync(ctx)
    assert exc.value.status == Status.NEEDS_ACTION and exc.value.data["conflicts"] == ["shared.txt"]
    with pytest.raises(KankanError) as exc:
        integrate.sync(ctx)
    assert "in progress" in exc.value.message


def test_verify_records_success(review):
    project, fake, card, codex, ctx = review
    ctx.project.cfg.test_command = "test -f shared.txt && echo all good"
    head = git(ctx.worktree, "rev-parse", "HEAD")
    assert integrate.verify(ctx, head[:10]) == {"tested": head, "ok": True}
    record = load_state(project.paths, "codex-1").verifications[head]
    assert record["ok"] and "all good" in record["output_tail"]


def test_verify_failure_recorded_needs_action(review):
    project, fake, card, codex, ctx = review
    ctx.project.cfg.test_command = "echo boom; exit 3"
    head = git(ctx.worktree, "rev-parse", "HEAD")
    with pytest.raises(KankanError) as exc:
        integrate.verify(ctx, head)
    assert exc.value.status == Status.NEEDS_ACTION and "boom" in exc.value.data["output_tail"]
    assert load_state(project.paths, "codex-1").verifications[head]["ok"] is False


def test_verify_wrong_tip_needs_action(review):
    project, fake, card, codex, ctx = review
    ctx.project.cfg.test_command = "true"
    old = git(ctx.worktree, "rev-parse", "HEAD")
    commit(ctx.worktree, "more.txt")
    with pytest.raises(KankanError) as exc:
        integrate.verify(ctx, old)
    assert exc.value.status == Status.NEEDS_ACTION and "tip" in exc.value.message


def test_verify_timeout_fails(review):
    project, fake, card, codex, ctx = review
    ctx.project.cfg.test_command = "sleep 30"
    ctx.project.cfg.test_timeout = 1
    head = git(ctx.worktree, "rev-parse", "HEAD")
    with pytest.raises(KankanError) as exc:
        integrate.verify(ctx, head)
    assert "timed out" in exc.value.message


def test_verify_test_mutates_worktree_not_recorded(review):
    project, fake, card, codex, ctx = review
    ctx.project.cfg.test_command = "echo junk > junk.txt"
    head = git(ctx.worktree, "rev-parse", "HEAD")
    with pytest.raises(KankanError) as exc:
        integrate.verify(ctx, head)
    assert "not recorded" in exc.value.message
    assert head not in load_state(project.paths, "codex-1").verifications


def test_verify_leaves_no_test_children(review, tmp_path):
    project, fake, card, codex, ctx = review
    pidfile = tmp_path / "child.pid"
    ctx.project.cfg.test_command = f"(sleep 300 & echo $! > {pidfile}); echo done"
    head = git(ctx.worktree, "rev-parse", "HEAD")
    integrate.verify(ctx, head)
    pid = int(pidfile.read_text())
    assert not procutil.alive(pid, None)


def test_verify_without_test_command(review):
    project, fake, card, codex, ctx = review
    head = git(ctx.worktree, "rev-parse", "HEAD")
    assert integrate.verify(ctx, head)["skipped"] is True
