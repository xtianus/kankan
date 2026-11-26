import os
import threading
from datetime import timedelta

import pytest

from kankan import attachments, auditlog, clock, diag, labels, seats, workflow
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


def _old_error_cause(project, card_id):
    old = clock.iso(clock.now() - timedelta(hours=30))
    update_state(project.paths, "claude-1", lambda s: s.label_causes.update(
        {card_id: {project.cfg.label_error: {"cause": "fail", "at": old}}}))


def test_old_label(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    fake.state["cards"][card["id"]]["idLabels"] = [project.cfg.label_error]
    _old_error_cause(project, card["id"])
    assert find(diag.reconcile(project), "old_label")[0].subject == card["id"]


def test_old_label_removed_by_the_user_not_reported(board):
    """The seat state still records why kankan set the label, but the card no longer has it."""
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    _old_error_cause(project, card["id"])
    assert not find(diag.reconcile(project), "old_label")


def test_old_label_of_deleted_card_not_reported(board):
    project, fake, lease = board
    _old_error_cause(project, "b" * 24)
    assert not find(diag.reconcile(project), "old_label")


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


def _make_dir(project, name):
    path = attachments.root(project.paths) / name / "x"
    path.mkdir(parents=True)
    (path / "f").write_text("data")
    return path.parent


def test_reconcile_reports_and_fixes_orphan_attachment_dirs(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    workflow.tick(project, lease)
    orphan = _make_dir(project, "Ab12Cd34")
    other = _make_dir(project, "not-a-short")
    mine = _make_dir(project, card["shortLink"])
    report = find(diag.reconcile(project), "attachment_dir_without_ref")
    assert sorted(i.subject for i in report) == ["Ab12Cd34", "not-a-short"]
    fixed = find(diag.reconcile(project, fix=True), "attachment_dir_deleted")
    assert [(i.subject, i.fixed) for i in fixed] == [("Ab12Cd34", True)]
    assert not orphan.exists() and other.exists() and mine.exists()


@pytest.mark.parametrize("blob", [
    lambda project: project.refs.write_blob({"schema": 1, "kind": "card", "card": "a" * 24}),
    lambda project: project.refs.write_blob({"schema": 1, "card": "a" * 24, "phase": "parked"}),
], ids=["no-short", "other-shape"])
def test_reconcile_fix_skips_attachment_dirs_with_malformed_ref(board, blob):
    project, fake, lease = board
    orphan = _make_dir(project, "Ab12Cd34")
    project.refs.create(card_ref("a" * 24), blob(project))
    report = diag.reconcile(project, fix=True)
    assert find(report, "attachment_dir_without_ref")[0].subject == "Ab12Cd34"
    assert not find(report, "attachment_dir_deleted")
    assert orphan.exists()


def test_reconcile_fix_unparsable_blob_skips_attachment_dirs(board):
    project, fake, lease = board
    orphan = _make_dir(project, "Ab12Cd34")
    git(project.paths.repo, "update-ref", card_ref("a" * 24), git(project.paths.repo, "rev-parse", "HEAD"))
    report = diag.reconcile(project, fix=True)
    assert find(report, "malformed_blob")
    assert not find(report, "attachment_dir_deleted")
    assert orphan.exists()


def test_reconcile_fix_integrated_ref_also_discards(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    from kankan import integrate
    integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    fake.user_move(card["id"], project.cfg.done)
    directory = _make_dir(project, card["shortLink"])
    issues = diag.reconcile(project, fix=True)
    assert "integrated_ref_deleted" in {i.kind for i in issues if i.fixed}
    assert project.refs.read(card_ref(card["id"])) is None
    assert not directory.exists()


def test_qa_seat_ignored_by_list_checks(board):
    from tests.helpers import add_qa_list
    project, fake, lease = board
    add_qa_list(project, fake)
    qa = seats.acquire_qa(project, "claude", None)
    seats.release(project, qa)
    assert "seat_without_lists" not in kinds(diag.reconcile(project))
    assert not [i for i in diag.reconcile(project) if i.kind == "card_without_ref"]


def test_seat_remove_of_qa_seat_archives_no_list(board):
    from tests.helpers import add_qa_list
    project, fake, lease = board
    add_qa_list(project, fake)
    seats.release(project, seats.acquire_qa(project, "claude", None))
    open_before = {i for i, l in fake.state["lists"].items() if not l["closed"]}
    remove_seat(project, "claude-qa")
    assert "claude-qa" not in project.reload_config().seats
    assert {i for i, l in fake.state["lists"].items() if not l["closed"]} == open_before
    assert not project.paths.seat_worktree("claude-qa").exists()


# ---------------------------------------------------------------------- Q&A (questions T§2, T§3)
from kankan import qa  # noqa: E402
from kankan.config import project_lock  # noqa: E402
from tests.helpers import add_qa_list, put_card_ref, remove_seat  # noqa: E402

CONV = "0123456789abcdef"


def _record(project, *card_ids):
    with project_lock(project.paths):
        for card_id in card_ids:
            qa.record_add(project.paths, card_id)


def test_done_without_merge_exempts_qa_cards_until_implementation_claim(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.done, "answered question")
    _record(project, card["id"])
    assert not find(diag.reconcile(project), "done_without_merge")
    fake.user_move(card["id"], project.cfg.backlog)
    workflow.tick(project, lease)
    value = project.refs.read(card_ref(card["id"]))
    assert value.meta["phase"] == "impl"
    project.refs.delete(card_ref(card["id"]), value.oid)
    fake.user_move(card["id"], project.cfg.done)
    assert find(diag.reconcile(project), "done_without_merge")[0].subject == card["id"]


@pytest.mark.parametrize("where", ["done", "archived", "deleted"])
def test_qa_conversation_ended_reported_and_fixed(board, where):
    project, fake, lease = board
    qa_list = add_qa_list(project, fake)
    card = fake.add_card(qa_list, "question")
    put_card_ref(project, card["id"], card["shortLink"], phase="qa", owner="claude-qa", target_list=qa_list,
                 conversation=CONV)
    qa.conversation_dir(project.paths, CONV).mkdir(parents=True)
    assert not kinds(diag.reconcile(project)) & {"qa_conversation_ended", "qa_conversation_dir_without_ref"}
    getattr(fake, {"done": "user_move", "archived": "user_archive", "deleted": "user_delete"}[where])(
        *((card["id"], project.cfg.done) if where == "done" else (card["id"],)))
    for _ in range(2):  # a list mismatch is reported only on a second run: make sure it never is
        issues = diag.reconcile(project)
    assert find(issues, "qa_conversation_ended")[0].subject == card["id"]
    assert not kinds(issues) & {"phase_list_mismatch", "ref_of_deleted_card", "ref_of_archived_card"}
    fixed = diag.reconcile(project, fix=True)
    assert find(fixed, "qa_conversation_ref_deleted")[0].fixed
    assert project.refs.read(card_ref(card["id"])) is None
    assert find(fixed, "qa_conversation_dir_deleted")[0].subject == CONV
    assert not qa.conversation_dir(project.paths, CONV).exists()


def test_qa_conversation_of_alive_seat_not_reported(board):
    project, fake, lease = board
    qa_list = add_qa_list(project, fake)
    qa_lease = seats.acquire_qa(project, "claude", None)
    card = fake.add_card(qa_list, "question")
    put_card_ref(project, card["id"], card["shortLink"], phase="qa", owner=qa_lease.seat, target_list=qa_list,
                 conversation=CONV)
    fake.user_move(card["id"], project.cfg.done)
    assert not find(diag.reconcile(project), "qa_conversation_ended")
    diag.reconcile(project, fix=True)
    assert project.refs.read(card_ref(card["id"])) is not None


def test_fix_removes_stale_qa_record_entries(board):
    project, fake, lease = board
    qa_list = add_qa_list(project, fake)
    held = {}
    for phase in ("impl", "implemented", "review", "integrated", "parked"):
        card = fake.add_card(project.cfg.backlog, phase)
        put_card_ref(project, card["id"], card["shortLink"], phase=phase, owner=None, target_list=project.cfg.backlog)
        held[phase] = card["id"]
    done = fake.add_card(project.cfg.done, "answered")["id"]
    asking = fake.add_card(qa_list, "asking")["id"]
    deleted = fake.add_card(qa_list, "deleted")["id"]
    archived = fake.add_card(qa_list, "archived")["id"]
    fake.user_delete(deleted)
    fake.user_archive(archived)
    _record(project, *held.values(), done, asking, deleted, archived)
    fixed = diag.reconcile(project, fix=True)
    assert qa.recorded(project.paths) == {held["parked"], done, asking}
    assert {i.subject for i in find(fixed, "qa_record_entry_removed")} == {
        held["impl"], held["implemented"], held["review"], held["integrated"], deleted, archived}


def test_fix_reads_archived_recorded_cards_outside_the_project_lock_and_prunes_them(board, monkeypatch):
    import time
    from kankan import config
    from kankan.trello import TrelloClient
    project, fake, lease = board
    qa_list = add_qa_list(project, fake)
    archived = [fake.add_card(qa_list, f"old {i}")["id"] for i in range(3)]
    for card_id in archived:
        fake.user_archive(card_id)
    _record(project, *archived)
    lock = str(project.paths.kankan_dir / "project.lock")
    reads = []
    real = TrelloClient.read_card  # every card read goes through it (board reader)

    def card(self, card_id, *args, **kwargs):
        if card_id in archived:
            entry = config._LOCK_DEPTH.get(lock)
            reads.append(entry is not None and entry[1] == threading.get_ident())
            time.sleep(0.2)  # a slow Trello
        return real(self, card_id, *args, **kwargs)
    monkeypatch.setattr(TrelloClient, "read_card", card)
    fixed = diag.reconcile(project, fix=True)
    assert reads == [False, False, False]  # read without the project lock
    assert qa.recorded(project.paths) == set()
    assert {i.subject for i in find(fixed, "qa_record_entry_removed")} == set(archived)
    reads.clear()
    diag.reconcile(project, fix=True)
    assert reads == []  # pruned: never read again


def test_fix_keeps_a_recorded_card_whose_ref_changed_during_the_reads(board, monkeypatch):
    from kankan.trello import TrelloClient
    project, fake, lease = board
    qa_list = add_qa_list(project, fake)
    card_id = fake.add_card(qa_list, "old")["id"]
    fake.user_archive(card_id)
    _record(project, card_id)
    real = TrelloClient.read_card  # every card read goes through it (board reader)

    def card(self, ident, *args, **kwargs):
        result = real(self, ident, *args, **kwargs)
        if ident == card_id and project.refs.read(card_ref(card_id)) is None:
            put_card_ref(project, card_id, "abcdefgh", phase="parked", owner=None, target_list=qa_list)
        return result
    monkeypatch.setattr(TrelloClient, "read_card", card)
    diag.reconcile(project, fix=True)
    assert qa.recorded(project.paths) == {card_id}


def test_fix_removes_conversation_dirs_no_ref_names(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "handed off")
    named, orphan = "1" * 16, "2" * 16
    put_card_ref(project, card["id"], card["shortLink"], phase="parked", cause="handoff",
                 target_list=project.cfg.backlog, handoff=["h1"], conversation=named)
    for name in (named, orphan):
        qa.conversation_dir(project.paths, name).mkdir(parents=True)
    assert [i.subject for i in find(diag.reconcile(project), "qa_conversation_dir_without_ref")] == [orphan]
    fixed = diag.reconcile(project, fix=True)
    assert [i.subject for i in find(fixed, "qa_conversation_dir_deleted")] == [orphan]
    assert qa.conversations(project.paths) == [named]


def test_old_sections_error_reported(board):
    from kankan import cardsections_feedback as feedback
    project, fake, lease = board
    old = clock.iso(clock.now() - timedelta(hours=30))
    feedback.save(project.paths, {"cards": {"c" * 24: {"fingerprint": "f", "report": "r", "at": old, "state": "reported",
                                                       "owned_error": True, "error_write_started": True,
                                                       "error_confirmed": True, "pending": []}}})
    issues = [i for i in diag.reconcile(project) if i.kind == "old_label"]
    assert any(i.subject == "c" * 24 and "sections" in i.detail for i in issues), issues


def test_old_sections_error_not_written_not_reported(board):
    from kankan import cardsections_feedback as feedback
    project, fake, lease = board
    old = clock.iso(clock.now() - timedelta(hours=30))
    feedback.save(project.paths, {"cards": {"c" * 24: {"fingerprint": "f", "report": "r", "at": old, "state": "reported",
                                                       "owned_error": True, "error_write_started": False,
                                                       "error_confirmed": False, "pending": []}}})
    issues = [i for i in diag.reconcile(project) if i.kind == "old_label"]
    assert not any(i.subject == "c" * 24 for i in issues), issues
