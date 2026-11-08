import os
from datetime import timedelta

import pytest

from kankan import auditlog, clock, diag, labels, seats, workflow
from kankan.config import write_registry_block
from kankan.refstore import card_ref
from kankan.seatstate import update_state
from tests.conftest import git
from tests.flow import commit, review_setup


@pytest.fixture
def board(kproject, fake_trello):
    lease = seats.acquire(kproject, "claude")
    kproject.reload_config()
    return kproject, fake_trello, lease


def kinds(issues):
    return {i.kind for i in issues}


def find(issues, kind):
    return [i for i in issues if i.kind == kind]


def test_clean_board_only_main_checked_out(board):
    project, fake, lease = board
    git(project.paths.repo, "checkout", "-q", "--detach")
    assert diag.reconcile(project) == []


def test_main_checked_out_and_registry(board):
    project, fake, lease = board
    fake.state["boards"][project.cfg.board_id]["desc"] = write_registry_block("", {"registry-id": "x"})
    assert {"main_checked_out", "registry_invalid"} <= kinds(diag.reconcile(project))


def test_phase_list_mismatch_needs_two_runs(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    workflow.tick(project, lease)
    fake.user_move(card["id"], project.cfg.done)
    assert not find(diag.reconcile(project), "phase_list_mismatch")
    assert find(diag.reconcile(project), "phase_list_mismatch")[0].subject == card["id"]


def test_card_without_ref_and_deleted_archived(board):
    project, fake, lease = board
    orphan = fake.add_card(project.cfg.seats["claude-1"].in_progress, "orphan")
    gone = fake.add_card(project.cfg.backlog, "gone")
    workflow.tick(project, lease)
    fake.user_delete(gone["id"])
    issues = diag.reconcile(project)
    assert find(issues, "card_without_ref")[0].subject == orphan["id"]
    assert find(issues, "ref_of_deleted_card")[0].subject == gone["id"]


def test_archived_card_ref_listed(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    workflow.tick(project, lease)
    fake.user_archive(card["id"])
    assert find(diag.reconcile(project), "ref_of_archived_card")[0].subject == card["id"]


def test_malformed_blob(board):
    project, fake, lease = board
    head = git(project.paths.repo, "rev-parse", "HEAD")
    git(project.paths.repo, "update-ref", card_ref("a" * 24), head)
    assert find(diag.reconcile(project), "malformed_blob")[0].subject == "a" * 24


def test_done_without_merge(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.done, "done by hand")
    assert find(diag.reconcile(project), "done_without_merge")[0].subject == card["id"]


def test_dead_seat_and_lists(board):
    project, fake, lease = board
    os.close(lease.fd)
    fake.state["lists"][project.cfg.seats["claude-1"].review]["closed"] = True
    issues = diag.reconcile(project)
    assert find(issues, "dead_seat") and find(issues, "seat_without_lists")


def test_unsatisfiable_dependency_and_branches(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    fake.add_attachment(card["id"], "https://trello.com/c/Zz99Zz99/1-gone")
    git(project.paths.repo, "branch", f"25kankan-{card['shortLink']}")
    git(project.paths.repo, "branch", f"26kankan-{card['shortLink']}")
    issues = diag.reconcile(project)
    assert "missing" in find(issues, "unsatisfiable_dependency")[0].detail
    assert find(issues, "multiple_branches")[0].subject == card["shortLink"]


def test_stale_blocked_worktree(board):
    project, fake, lease = board
    git(project.paths.repo, "worktree", "add", "-q", "--detach", str(project.paths.blocked_worktree("Ab12Cd34")))
    assert find(diag.reconcile(project), "stale_blocked_worktree")


def test_old_label(board):
    project, fake, lease = board
    old = clock.iso(clock.now() - timedelta(hours=30))
    update_state(project.paths, "claude-1", lambda s: s.label_causes.update(
        {"b" * 24: {project.cfg.label_error: {"cause": "fail", "at": old}}}))
    assert find(diag.reconcile(project), "old_label")


def test_push_failed(board):
    project, fake, lease = board
    project.cfg.push_main = True
    from kankan.config import save_config
    save_config(project.paths, project.cfg)
    auditlog.append(project.paths, "push_failed", merge=git(project.paths.repo, "rev-parse", "HEAD"))
    assert find(diag.reconcile(project), "push_failed")


def test_fix_only_certain_cases(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    from kankan import integrate
    integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    fake.user_move(card["id"], project.cfg.done)
    fake.user_label(card["id"], project.cfg.label_error)
    fake.user_label(card["id"], project.cfg.label_hold)
    resume = fake.add_label(project.cfg.board_id, labels.resume_label_name(clock.now() - timedelta(hours=3)))
    report = diag.reconcile(project)
    assert all(not i.fixed for i in report)
    assert project.refs.read(card_ref(card["id"])) is not None
    issues = diag.reconcile(project, fix=True)
    fixed = {i.kind for i in issues if i.fixed}
    assert fixed == {"integrated_ref_deleted", "resume_label_deleted", "done_label_removed"}
    assert project.refs.read(card_ref(card["id"])) is None
    assert fake.state["cards"][card["id"]]["idLabels"] == [project.cfg.label_hold]
    assert resume["id"] not in fake.state["labels"]
