import pytest

from kankan import worktrees
from kankan.errors import KankanError, Status
from tests.conftest import git


def test_branch_name():
    assert worktrees.branch_name("Ab12Cd34", 2026) == "26kankan-Ab12Cd34"
    assert worktrees.branch_name("Ab12Cd34", 2105) == "05kankan-Ab12Cd34"


def test_branch_reuse_after_year_change(git_repo):
    git(git_repo, "branch", "25kankan-Ab12Cd34")
    assert worktrees.prepare_branch(git_repo, "Ab12Cd34", "main", year=2026) == "25kankan-Ab12Cd34"
    assert worktrees.prepare_branch(git_repo, "Zz12Zz12", "main", year=2026) == "26kankan-Zz12Zz12"
    git(git_repo, "branch", "26kankan-Ab12Cd34")
    with pytest.raises(KankanError) as exc:
        worktrees.prepare_branch(git_repo, "Ab12Cd34", "main", year=2026)
    assert exc.value.status == Status.NEEDS_RECONCILIATION
    assert exc.value.data["branches"] == ["25kankan-Ab12Cd34", "26kankan-Ab12Cd34"]


def test_dirty_files_ignores_gitignored(git_repo):
    (git_repo / ".gitignore").write_text("build/\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-qm", "ignore")
    (git_repo / "build").mkdir()
    (git_repo / "build" / "out.o").write_text("x")
    assert worktrees.dirty_files(git_repo) == []
    (git_repo / "a.txt").write_text("x")
    (git_repo / "README.md").write_text("changed\n")
    assert sorted(worktrees.dirty_files(git_repo)) == ["README.md", "a.txt"]


def test_where_checked_out_and_detach(git_repo, tmp_path):
    wt = tmp_path / "wt"
    git(git_repo, "worktree", "add", "-q", "--detach", str(wt))
    git(git_repo, "branch", "feature")
    worktrees.checkout(wt, "feature")
    assert worktrees.where_checked_out(git_repo, "feature") == wt
    assert worktrees.current_branch(wt) == "feature"
    worktrees.detach(wt)
    assert worktrees.where_checked_out(git_repo, "feature") is None
    assert worktrees.main_checked_out(git_repo, "main") == [git_repo]
    assert not worktrees.merge_in_progress(wt)
