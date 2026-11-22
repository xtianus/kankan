"""Q&A conversation policy (questions T§3–T§5): candidates and `new`, eligibility, claim, take-over, turns, end."""

import contextlib
import threading
from dataclasses import replace

import pytest

from kankan import attachments, faults, gitutil, qa, seats, worktrees
from kankan.adapters import ModelEffort
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref, seat_ref
from kankan.seatstate import load_state, update_state
from kankan.trello import Comment
from tests.conftest import git
from tests.helpers import remove_seat, add_qa_list, put_card_ref

PAIR = ModelEffort("sonnet", "high")
OLD = "fedcba9876543210"


def ident(number: int) -> str:
    return f"{number:024x}"


@pytest.fixture
def qa_env(kproject, fake_trello):
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "claude", None)
    return project, fake, lease, qa_list


def _top(project, fake, qa_list, name="question"):
    card = fake.add_card(qa_list, name)
    return project.trello.card(card["id"])


# ---------------------------------------------------------------------- candidates and delivery

def test_candidates_keep_board_members_only_and_drop_marked_comments(qa_env):
    project, fake, lease, qa_list = qa_env
    card = fake.add_card(qa_list, "q")
    outsider = fake.add_member("outsider")
    by_owner = fake.user_comment(card["id"], "a question")
    fake.user_comment(card["id"], "kankan notice", member_id=fake.me["id"])
    fake.user_comment(card["id"], "from outside", member_id=outsider["id"])
    fake.user_comment(card["id"], "pasted `kankan:qa-0123456789abcdef-t-1`")
    att_owner = fake.add_attachment(card["id"], "https://example.com/a")
    fake.add_attachment(card["id"], "https://example.com/b", member_id=outsider["id"])
    fake.add_attachment(card["id"], "https://example.com/c", member_id=fake.me["id"])
    fresh = project.trello.card(card["id"])
    cands = qa.candidates(project, fresh, project.trello.comments(card["id"]))
    assert cands == qa.Inputs(comments=[by_owner["id"]], attachments=[att_owner["id"]])


def test_to_deliver_triggers_only_on_items_neither_covered_nor_delivered():
    cands = qa.Inputs([ident(1), ident(2)], [ident(10)])
    assert qa.to_deliver(cands, None, qa.Inputs([], [])) == cands
    covered = {"comment": ident(2), "attachments": [ident(10)]}
    assert qa.to_deliver(cands, covered, qa.Inputs([], [])) is None
    assert qa.to_deliver(cands, {"comment": ident(1), "attachments": []}, qa.Inputs([ident(2)], [ident(10)])) is None
    assert qa.to_deliver(qa.Inputs([], []), None, qa.Inputs([], [])) is None


def test_to_deliver_includes_the_failed_turns_inputs():
    # the turn that delivered c2 failed: c2 stays uncovered and comes again with the follow-up c3
    cands = qa.Inputs([ident(1), ident(2), ident(3)], [ident(10), ident(11)])
    covered = {"comment": ident(1), "attachments": [ident(10)]}
    delivered = qa.Inputs([ident(2)], [ident(11)])
    assert qa.to_deliver(cands, covered, delivered) == qa.Inputs([ident(2), ident(3)], [ident(11)])
    assert qa.to_deliver(qa.Inputs([ident(1), ident(2)], [ident(10), ident(11)]), covered, delivered) is None


def test_coverage_includes_context_so_to_deliver_ignores_it():
    inputs, context = qa.Inputs([ident(5)], [ident(12)]), qa.Inputs([ident(1), ident(3)], [ident(10)])
    covered = qa.coverage(None, inputs, context)
    assert covered == {"comment": ident(5), "attachments": [ident(10), ident(12)]}
    cands = qa.Inputs([ident(1), ident(3), ident(5)], [ident(10), ident(12)])
    assert qa.to_deliver(cands, covered, qa.Inputs([], [])) is None
    # a newer previous comment is kept; attachments accumulate
    assert qa.coverage({"comment": ident(9), "attachments": [ident(20)]}, qa.Inputs([ident(6)], []), None) == \
        {"comment": ident(9), "attachments": [ident(20)]}


def test_first_turn_marks_items_after_the_latest_answer_by_trello_dates(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    owner, me = fake.owner["id"], fake.me["id"]

    def comment(number, member, date, text="text"):
        return Comment(id=ident(number), member_id=member, username="u", date=date, text=text)

    answer = "the answer\n\n`kankan:qa-0123456789abcdef-0a1b2c3d-1`"
    comments = [
        comment(1, owner, "2026-11-17T10:00:00.000Z"),
        comment(2, me, "2026-11-17T10:05:00.000Z", answer),
        comment(3, owner, "2026-11-17T10:04:00.000Z"),  # newer ID but dated before the answer: context
        comment(4, owner, "2026-11-17T10:06:00.000Z"),
    ]
    fake.add_attachment(card.id, "https://example.com/old")
    fake.add_attachment(card.id, "https://example.com/new")
    fresh = project.trello.card(card.id)
    first, second = fresh.attachment_items
    fresh.attachment_items = [replace(first, date="2026-11-17T10:01:00.000Z"),
                              replace(second, date="2026-11-17T10:07:00.000Z")]
    inputs, context, _ = qa.first_turn(project, fresh, comments)
    assert inputs == qa.Inputs([ident(4)], [second.id])
    assert context == qa.Inputs([ident(3)], [first.id])  # the newest context comment covers the older ones
    # never answered: everything is new
    inputs, context, _ = qa.first_turn(project, fresh, [c for c in comments if c.id != ident(2)])
    assert inputs == qa.Inputs([ident(1), ident(3), ident(4)], [first.id, second.id])
    assert context == qa.Inputs([], [])


# ---------------------------------------------------------------------- eligibility and claim

def test_eligible_takes_the_top_free_qa_card_and_ignores_dependencies(qa_env):
    project, fake, lease, qa_list = qa_env
    held = fake.add_card(qa_list, "held")
    fake.user_label(held["id"], project.cfg.label_hold)
    implementing = fake.add_card(qa_list, "impl ref")
    put_card_ref(project, implementing["id"], implementing["shortLink"], phase="impl", owner="claude-1")
    unfinished = fake.add_card(qa_list, "unfinished handoff")
    put_card_ref(project, unfinished["id"], unfinished["shortLink"], phase="parked", cause="handoff",
                 target_list=project.cfg.backlog, from_list=qa_list)
    dependent = fake.add_card(qa_list, "parked, with a dependency")
    put_card_ref(project, dependent["id"], dependent["shortLink"], phase="parked", cause="yield",
                 target_list=project.cfg.backlog)
    blocker = fake.add_card(project.cfg.backlog, "not done")
    fake.add_attachment(dependent["id"], f"https://trello.com/c/{blocker['shortLink']}")
    fake.add_card(qa_list, "below")
    fake.add_card(project.cfg.backlog, "backlog card")
    from kankan.workflow import card_refs
    snap = project.trello.snapshot(project.cfg.board_id)
    found = qa.eligible(project, snap, card_refs(project))
    assert found is not None and found.id == dependent["id"]


def test_eligible_skips_a_card_with_a_malformed_ref(qa_env):
    project, fake, lease, qa_list = qa_env
    broken = fake.add_card(qa_list, "malformed ref")
    good = fake.add_card(qa_list, "good")
    oid = gitutil.out(project.paths.repo, ["hash-object", "-w", "--stdin"], input="not json")
    project.refs.create(card_ref(broken["id"]), oid)
    from kankan.workflow import card_refs
    snap = project.trello.snapshot(project.cfg.board_id)
    refs = card_refs(project)
    assert broken["id"] not in refs
    assert qa.eligible(project, snap, refs).id == good["id"]
    raw = dict(refs, **{broken["id"]: KankanError(Status.NEEDS_RECONCILIATION, "malformed")})
    assert qa.eligible(project, snap, raw).id == good["id"]


def test_eligible_without_qa_list(kproject, fake_trello):
    fake_trello.add_card(kproject.cfg.backlog, "x")
    assert qa.eligible(kproject, kproject.trello.snapshot(kproject.cfg.board_id), {}) is None


def test_claim_creates_the_qa_ref_and_the_seat_task(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    task = qa.claim(project, lease, card, PAIR)
    meta = project.refs.read(card_ref(card.id)).meta
    assert meta["phase"] == "qa" and meta["owner"] == lease.seat and meta["target_list"] == qa_list
    assert "branch" not in meta and "covered" not in meta and qa.valid_conversation(meta["conversation"])
    assert task["kind"] == "qa" and task["card"] == card.id and task["short"] == card.short
    assert task["conversation"] == meta["conversation"] and task["turn"] is None and task["ended"] is False
    assert (task["role"], task["model"], task["effort"]) == ("questions", "sonnet", "high")
    assert task["base"] == gitutil.rev(project.paths.repo, "main")
    assert task["reflog"] == worktrees.reflog_length(project.paths.seat_worktree(lease.seat))
    assert load_state(project.paths, lease.seat).task == task
    assert card.id in qa.recorded(project.paths)
    assert qa.conversation_dir(project.paths, meta["conversation"]).is_dir()
    assert fake.card_list(card.id) == qa_list


def test_claim_lock_hold_is_one_card_read_and_one_transaction(qa_env, monkeypatch):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    real_lock, real_tx = qa.project_lock, project.refs.transaction
    seen = {"requests": [], "tx": 0, "inside": False, "recorded_at_tx": None}

    class Counting:
        def __init__(self, paths):
            self.inner = real_lock(paths)

        def __enter__(self):
            self.inner.__enter__()
            seen["inside"] = True
            seen["start"] = len(fake.requests)

        def __exit__(self, *exc):
            seen["inside"] = False
            seen["requests"] += fake.requests[seen["start"]:]
            return self.inner.__exit__(*exc)

    def tx(ops):
        if seen["inside"]:
            seen["tx"] += 1
            seen["recorded_at_tx"] = card.id in qa.recorded(project.paths)
        return real_tx(ops)

    monkeypatch.setattr(qa, "project_lock", Counting)
    monkeypatch.setattr(project.refs, "transaction", tx)
    assert qa.claim(project, lease, card, PAIR) is not None
    assert [method for method, _ in seen["requests"]] == ["GET"]
    assert seen["requests"][0][1].startswith(f"/1/cards/{card.id}")
    assert seen["tx"] == 1 and seen["recorded_at_tx"] is True


def test_claim_from_parked_drops_branch_and_old_conversation(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    branch = f"26kankan-{card.short}"
    git(project.paths.repo, "branch", branch, "main")
    tip = gitutil.rev(project.paths.repo, branch)
    put_card_ref(project, card.id, card.short, phase="parked", cause="handoff", target_list=project.cfg.backlog,
                 branch=branch, conversation=OLD, handoff=["h1"])
    old_dir = qa.conversation_dir(project.paths, OLD)
    old_dir.mkdir(parents=True)
    (old_dir / "turn.json").write_text("{}")
    task = qa.claim(project, lease, card, PAIR)
    meta = project.refs.read(card_ref(card.id)).meta
    assert task is not None and meta["phase"] == "qa"
    assert "branch" not in meta and "handoff" not in meta and meta["conversation"] != OLD
    assert gitutil.rev(project.paths.repo, branch) == tip
    assert not old_dir.exists()


@pytest.mark.parametrize("change", ["moved", "hold", "archived", "claimed"])
def test_claim_refuses_a_card_changed_since_the_board_read(qa_env, change):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    if change == "moved":
        fake.user_move(card.id, project.cfg.backlog)
    elif change == "hold":
        fake.user_label(card.id, project.cfg.label_hold)
    elif change == "archived":
        fake.user_archive(card.id)
    else:
        put_card_ref(project, card.id, card.short, phase="impl", owner="claude-1")
    before = project.refs.read(card_ref(card.id))
    assert qa.claim(project, lease, card, PAIR) is None
    after = project.refs.read(card_ref(card.id))
    assert (after.oid if after else None) == (before.oid if before else None)
    assert load_state(project.paths, lease.seat).task is None
    assert qa.conversations(project.paths) == []


def test_claim_refuses_a_lost_seat(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    stale = replace(lease, seat_oid="0" * 39 + "1")
    with pytest.raises(KankanError) as err:
        qa.claim(project, stale, card, PAIR)
    assert err.value.status == Status.NOT_OWNER
    assert project.refs.read(card_ref(card.id)) is None


def test_claim_resets_the_worktree_discarding_a_stray_commit_and_untracked_files(qa_env):
    project, fake, lease, qa_list = qa_env
    worktree = project.paths.seat_worktree(lease.seat)
    (worktree / "stray.txt").write_text("x")
    git(worktree, "add", "stray.txt")
    git(worktree, "commit", "-q", "-m", "stray")
    (worktree / "README.md").write_text("changed\n")
    (worktree / "untracked.txt").write_text("u")
    (worktree / ".gitignore").write_text("ignored.txt\n")
    (worktree / "ignored.txt").write_text("i")
    (project.paths.repo / "new.txt").write_text("n")
    git(project.paths.repo, "add", "new.txt")
    git(project.paths.repo, "commit", "-q", "-m", "main moved")
    card = _top(project, fake, qa_list)
    task = qa.claim(project, lease, card, PAIR)
    assert gitutil.rev(worktree, "HEAD") == gitutil.rev(project.paths.repo, "main") == task["base"]
    assert worktrees.current_branch(worktree) is None
    assert worktrees.dirty_files(worktree) == []
    assert not (worktree / "stray.txt").exists() and not (worktree / "ignored.txt").exists()
    assert (worktree / "README.md").read_text() == "hello\n"


def test_reset_detached_removes_an_untracked_nested_repository(qa_env):
    project, fake, lease, qa_list = qa_env
    worktree = project.paths.seat_worktree(lease.seat)
    git(worktree, "init", "-q", "nested")  # e.g. a clone an earlier conversation left behind
    (worktree / "nested" / "f.txt").write_text("x")
    worktrees.reset_detached(worktree, gitutil.rev(project.paths.repo, "main"))
    assert not (worktree / "nested").exists()
    assert worktrees.dirty_files(worktree) == []


def test_reset_detached_and_reflog_length(qa_env):
    project, fake, lease, qa_list = qa_env
    worktree = project.paths.seat_worktree(lease.seat)
    before = worktrees.reflog_length(worktree)
    git(worktree, "commit", "-q", "--allow-empty", "-m", "agent")
    assert worktrees.reflog_length(worktree) == before + 1
    main = gitutil.rev(project.paths.repo, "main")
    assert worktrees.reset_detached(worktree, main) == (main, worktrees.reflog_length(worktree))
    assert gitutil.rev(worktree, "HEAD") == main


# ---------------------------------------------------------------------- take-over

def test_take_over_on_restart_keeps_base_and_gets_a_new_task_id(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    task = qa.claim(project, lease, card, PAIR)
    git(project.paths.seat_worktree(lease.seat), "commit", "-q", "--allow-empty", "-m", "agent")
    value = project.refs.read(card_ref(card.id))
    taken = qa.take_over(project, lease, value, ModelEffort("opus", "low"))
    assert taken["id"] != task["id"] and taken["conversation"] == task["conversation"]
    assert (taken["base"], taken["reflog"]) == (task["base"], task["reflog"])
    assert (taken["model"], taken["effort"], taken["ended"]) == ("opus", "low", False)
    assert load_state(project.paths, lease.seat).task == taken


def test_take_over_without_a_base_resets_its_own_worktree(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    qa.claim(project, lease, card, PAIR)
    update_state(project.paths, lease.seat, lambda state: setattr(state, "task", None))
    worktree = project.paths.seat_worktree(lease.seat)
    git(worktree, "commit", "-q", "--allow-empty", "-m", "stray")
    taken = qa.take_over(project, lease, project.refs.read(card_ref(card.id)), PAIR)
    assert taken["base"] == gitutil.rev(worktree, "HEAD") == gitutil.rev(project.paths.repo, "main")
    assert taken["reflog"] == worktrees.reflog_length(worktree)


def test_take_over_refuses_a_ref_it_does_not_own(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    value = put_card_ref(project, card.id, card.short, phase="qa", owner="codex-qa", target_list=qa_list,
                         conversation=OLD)
    with pytest.raises(KankanError) as err:
        qa.take_over(project, lease, value, PAIR)
    assert err.value.status == Status.NOT_OWNER


def test_take_over_refuses_a_seat_no_longer_held_before_touching_the_worktree(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    qa.claim(project, lease, card, PAIR)
    update_state(project.paths, lease.seat, lambda state: setattr(state, "task", None))
    worktree = project.paths.seat_worktree(lease.seat)
    git(worktree, "commit", "-q", "--allow-empty", "-m", "stray")
    stray = gitutil.rev(worktree, "HEAD")
    stale = replace(lease, seat_oid="0" * 39 + "1")
    with pytest.raises(KankanError) as err:
        qa.take_over(project, stale, project.refs.read(card_ref(card.id)), PAIR)
    assert err.value.status == Status.NOT_OWNER
    assert gitutil.rev(worktree, "HEAD") == stray
    assert load_state(project.paths, lease.seat).task is None


# ---------------------------------------------------------------------- turns

def test_start_turn_saves_turn_json_then_the_seat_task(qa_env):
    project, fake, lease, qa_list = qa_env
    task = qa.claim(project, lease, _top(project, fake, qa_list), PAIR)
    inputs, context = qa.Inputs([ident(3)], [ident(10)]), qa.Inputs([ident(1)], [ident(9)])
    token = qa.start_turn(project, lease, task, inputs, context)
    saved = qa.read_turn(project.paths, task["conversation"])
    assert saved == {"token": token, "inputs": {"comments": [ident(3)], "attachments": [ident(10)]},
                     "context": {"comments": [ident(1)], "attachments": [ident(9)]}, "run": task["id"]}
    assert load_state(project.paths, lease.seat).task["turn"] == token
    second = qa.start_turn(project, lease, dict(task, turn=token), qa.Inputs([ident(4)], []))
    saved = qa.read_turn(project.paths, task["conversation"])
    assert second != token and saved["context"] == {"comments": [ident(1)], "attachments": [ident(9)]}
    assert load_state(project.paths, lease.seat).task["turn"] == second
    # once an answer covered something, the first turn's context is no longer carried
    value = project.refs.read(card_ref(task["card"]))
    covered = {k: v for k, v in dict(value.meta, covered=qa.coverage(None, inputs, context)).items() if k != "nonce"}
    assert project.refs.update(card_ref(task["card"]), project.refs.write_blob(covered), value.oid)
    qa.start_turn(project, lease, task, qa.Inputs([ident(6)], []))
    assert "context" not in qa.read_turn(project.paths, task["conversation"])


def _retaken(project, fake, qa_list, lease, follow_up=True):
    """A card answered in an earlier conversation, claimed again; returns (task, old, new comment IDs, turn)."""
    card = fake.add_card(qa_list, "re-taken")
    old = fake.user_comment(card["id"], "old question")["id"]
    fake.user_comment(card["id"], "old answer\n\n`kankan:qa-fedcba9876543210-0a1b2c3d-1`", member_id=fake.me["id"])
    new = [fake.user_comment(card["id"], "new question")["id"]] if follow_up else []
    fresh = project.trello.card(card["id"])
    task = qa.claim(project, lease, fresh, PAIR)
    inputs, context, _ = qa.first_turn(project, fresh, project.trello.comments(card["id"]))
    assert inputs == qa.Inputs(new, []) and context == qa.Inputs([old], [])
    qa.start_turn(project, lease, task, inputs, context)
    return task, old, new, fresh


def _deliverable(project, task, delivered):
    card = project.trello.card(task["card"])
    cands = qa.candidates(project, card, project.trello.comments(task["card"]))
    covered = qa.effective_covered(project.refs.read(card_ref(task["card"])).meta.get("covered"),
                                   qa.read_turn(project.paths, task["conversation"]))
    return qa.to_deliver(cands, covered, delivered)


def test_failed_first_turn_of_a_retaken_card_never_resends_the_old_thread(qa_env):
    project, fake, lease, qa_list = qa_env
    task, old, new, card = _retaken(project, fake, qa_list, lease)
    # the first turn failed (no answer, no covered); the user follows up
    follow = fake.user_comment(card.id, "follow-up")["id"]
    delivery = _deliverable(project, task, qa.Inputs(new, []))
    assert delivery == qa.Inputs(new + [follow], [])
    qa.start_turn(project, lease, task, delivery)
    assert qa.read_turn(project.paths, task["conversation"])["context"] == {"comments": [old], "attachments": []}
    assert _deliverable(project, task, qa.Inputs(new + [follow], [])) is None


def test_restart_before_the_first_answer_is_not_triggered_by_the_old_thread(qa_env):
    project, fake, lease, qa_list = qa_env
    task, old, new, card = _retaken(project, fake, qa_list, lease, follow_up=False)
    assert _deliverable(project, task, qa.Inputs()) is None  # a new launcher run: nothing delivered yet
    assert qa.effective_covered(None, None) is None
    assert qa.effective_covered({"comment": "x", "attachments": []}, {"context": {"comments": [old]}}) == \
        {"comment": "x", "attachments": []}


def test_restart_before_the_first_answer_delivers_only_the_new_inputs(qa_env):
    project, fake, lease, qa_list = qa_env
    task, old, new, card = _retaken(project, fake, qa_list, lease)
    assert _deliverable(project, task, qa.Inputs()) == qa.Inputs(new, [])


def test_start_turn_crash_after_saving_turn_json(qa_env, monkeypatch):
    project, fake, lease, qa_list = qa_env
    task = qa.claim(project, lease, _top(project, fake, qa_list), PAIR)
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_turn_saved")
    with pytest.raises(faults.SimulatedCrash):
        qa.start_turn(project, lease, task, qa.Inputs([ident(3)], []))
    assert qa.read_turn(project.paths, task["conversation"])["inputs"]["comments"] == [ident(3)]
    assert load_state(project.paths, lease.seat).task["turn"] is None


def test_start_turn_after_the_end_is_not_owner(qa_env):
    project, fake, lease, qa_list = qa_env
    task = qa.claim(project, lease, _top(project, fake, qa_list), PAIR)
    assert qa.end(project, lease, task) is True
    with pytest.raises(KankanError) as err:
        qa.start_turn(project, lease, task, qa.Inputs([], []))
    assert err.value.status == Status.NOT_OWNER
    assert qa.conversations(project.paths) == []


# ---------------------------------------------------------------------- card state and end

def test_card_state_and_active_ended(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    cfg = project.cfg
    assert qa.active_ended(cfg, qa.card_state(project, card.id)) is False
    fake.user_label(card.id, cfg.label_hold)
    assert qa.active_ended(cfg, qa.card_state(project, card.id)) is True
    fake.user_label(card.id, cfg.label_hold, add=False)
    fake.user_move(card.id, cfg.done)
    assert qa.active_ended(cfg, qa.card_state(project, card.id)) is True
    fake.user_move(card.id, qa_list)
    fake.user_archive(card.id)
    assert qa.active_ended(cfg, qa.card_state(project, card.id)) is True
    assert qa.active_ended(cfg, None) is True
    fake.user_delete(card.id)
    assert qa.card_state(project, card.id) is None


def _attachment_dir(project, card):
    directory = attachments.card_dir(project.paths, card.short)
    directory.mkdir(parents=True)
    (directory / "file.txt").write_text("x")
    return directory


def test_end_deletes_ref_and_directories_and_marks_the_task_ended(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    task = qa.claim(project, lease, card, PAIR)
    qa.start_turn(project, lease, task, qa.Inputs([], []))
    att_dir = _attachment_dir(project, card)
    fake.user_move(card.id, project.cfg.done)
    assert qa.end(project, lease, task) is True
    assert project.refs.read(card_ref(card.id)) is None
    state = load_state(project.paths, lease.seat).task
    assert state["id"] == task["id"] and state["ended"] is True
    assert not att_dir.exists() and not qa.conversation_dir(project.paths, task["conversation"]).exists()
    assert qa.end(project, lease, task) is False  # idempotent
    assert card.id in qa.recorded(project.paths)  # reconcile exempts it in Done


def test_end_after_handoff_keeps_ref_and_directories(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    task = qa.claim(project, lease, card, PAIR)
    qa.start_turn(project, lease, task, qa.Inputs([], []))
    att_dir = _attachment_dir(project, card)
    value = project.refs.read(card_ref(card.id))
    parked = dict(value.meta, phase="parked", cause="handoff", owner=None, target_list=project.cfg.backlog,
                  handoff=["h1"])
    parked = {key: item for key, item in parked.items() if item is not None and key != "nonce"}
    assert project.refs.update(card_ref(card.id), project.refs.write_blob(parked), value.oid)
    assert qa.end(project, lease, task, handed_off=True) is True
    assert project.refs.read(card_ref(card.id)).meta["phase"] == "parked"
    assert load_state(project.paths, lease.seat).task["ended"] is True
    assert att_dir.exists() and qa.conversation_dir(project.paths, task["conversation"]).exists()


def test_end_leaves_a_conversation_taken_over_by_another_seat(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    task = qa.claim(project, lease, card, PAIR)
    value = project.refs.read(card_ref(card.id))
    moved = {key: item for key, item in dict(value.meta, owner="codex-qa").items() if key != "nonce"}
    assert project.refs.update(card_ref(card.id), project.refs.write_blob(moved), value.oid)
    assert qa.end(project, lease, task) is False
    assert project.refs.read(card_ref(card.id)).meta["owner"] == "codex-qa"
    assert qa.conversation_dir(project.paths, task["conversation"]).exists()


def test_end_admin_ends_the_conversation_of_a_free_seat(qa_env):
    project, fake, lease, qa_list = qa_env
    card = _top(project, fake, qa_list)
    task = qa.claim(project, lease, card, PAIR)
    att_dir = _attachment_dir(project, card)
    seats.release(project, lease)
    assert qa.end_admin(project, lease.seat) is True
    assert project.refs.read(card_ref(card.id)) is None
    assert not att_dir.exists() and not qa.conversation_dir(project.paths, task["conversation"]).exists()
    assert load_state(project.paths, lease.seat).task["ended"] is True
    assert qa.end_admin(project, lease.seat) is False


# ---------------------------------------------------------------------- conversation lock

def test_conversation_lock_survives_the_deletion_of_its_file(kproject, monkeypatch):
    """A waiter on a deleted ``op.lock`` never shares the lock with a holder of the re-created file."""
    import fcntl
    paths = kproject.paths
    conv = "0123456789abcdef"
    real_flock = fcntl.flock
    waiting = threading.Event()

    def flock(fd, op):
        try:
            return real_flock(fd, op)
        except BlockingIOError:
            if threading.current_thread().name == "waiter":
                waiting.set()
            raise

    monkeypatch.setattr(fcntl, "flock", flock)
    holder_in, discard_go, discarded, release_go = (threading.Event() for _ in range(4))
    other_in, other_out = threading.Event(), threading.Event()
    outcome = {}

    def holder():
        with qa.conversation_lock(paths, conv, 1):
            holder_in.set()
            discard_go.wait(5)
            outcome["discarded"] = qa.discard_conversation(paths, conv)  # reentrant; deletes op.lock
            discarded.set()
            release_go.wait(5)

    def waiter():
        with qa.conversation_lock(paths, conv, 5):
            outcome["overlap"] = other_in.is_set() and not other_out.is_set()

    def other():
        with qa.conversation_lock(paths, conv, 1):  # re-creates op.lock while the holder still holds the old one
            other_in.set()
            other_out.wait(5)

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter, name="waiter"),
               threading.Thread(target=other)]
    threads[0].start()
    assert holder_in.wait(5)
    threads[1].start()
    assert waiting.wait(5)  # the waiter opened the old op.lock and is blocked on it
    discard_go.set()
    assert discarded.wait(5) and outcome["discarded"] is True
    threads[2].start()
    assert other_in.wait(5)
    release_go.set()
    threads[0].join()
    import time
    time.sleep(0.3)  # the waiter now gets the deleted file's lock: it must not count as the conversation lock
    other_out.set()
    for thread in threads[1:]:
        thread.join()
    assert outcome["overlap"] is False
    with qa.conversation_lock(paths, conv, 1):
        pass


def test_conversation_lock_gives_up_when_its_directory_keeps_disappearing(kproject, monkeypatch):
    real = qa.os.open
    calls = []

    def vanishing(path, *args, **kwargs):
        if str(path).endswith("op.lock"):
            calls.append(1)
            raise FileNotFoundError(path)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(qa.os, "open", vanishing)
    outcome = {}

    def take():
        try:
            with qa.conversation_lock(kproject.paths, "0123456789abcdef", 0.3):
                outcome["taken"] = True
        except KankanError as err:
            outcome["error"] = err
    thread = threading.Thread(target=take, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and isinstance(outcome.get("error"), qa.LockBusy)
    assert len(calls) < 50  # it waits between attempts instead of spinning


# ---------------------------------------------------------------------- seat replacement (T§2, F§8.13)

@pytest.fixture
def old_seat(kproject, fake_trello):
    """``codex-qa`` holds a conversation on card A; card B is on top of ``Q&A``; the seat is still running."""
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "codex", None)
    first = _top(project, fake, qa_list, "A")
    task = qa.claim(project, lease, first, PAIR)
    top = fake.add_card(qa_list, "B", pos=0)
    return project, fake, lease, first, task, top


def _dead(project, lease):
    """The launcher died: its flock is gone, its seat ref stays."""
    seats._release_fd(lease.fd)


def _assert_replaced(project, card, task, top, new="claude-qa", old="codex-qa"):
    value = project.refs.read(card_ref(card.id))
    assert value.meta["phase"] == "qa" and value.meta["owner"] == new
    assert value.meta["conversation"] == task["conversation"]
    assert project.refs.read(card_ref(top["id"])) is None
    cfg = project.reload_config()
    assert cfg.qa_seat() == new and old not in cfg.seats
    assert project.refs.read(seat_ref(old)) is None
    assert not project.paths.seat_worktree(old).exists()
    assert not list(project.paths.seats_dir.glob(f"{old}.*"))
    assert qa.conversation_dir(project.paths, task["conversation"]).exists()


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "dead"])
def test_replace_seat_takes_over_the_conversation_of_a_stopped_or_dead_seat(old_seat, stopped):
    project, fake, lease, card, task, top = old_seat
    if stopped:
        seats.release(project, lease)
        assert project.refs.read(seat_ref("codex-qa")) is None
    else:
        _dead(project, lease)
        assert seats.liveness(project, "codex-qa") == "dead"
    assert qa.replace_seat(project, "claude") == "codex-qa"
    _assert_replaced(project, card, task, top)
    assert qa.replace_seat(project, "claude") is None  # idempotent


@pytest.mark.parametrize("state", ["alive", "unavailable"])
def test_replace_seat_refuses_a_running_or_unavailable_seat(old_seat, state):
    import os
    from kankan import procutil
    from kankan.config import atomic_write_json
    project, fake, lease, card, task, top = old_seat
    if state == "unavailable":
        proc = seats.read_proc(project, "codex-qa") or {}
        proc["unavailable"] = [{"pid": os.getpid(), "start": procutil.start_time(os.getpid())}]
        atomic_write_json(project.paths.seats_dir / "codex-qa.proc.json", proc)
        _dead(project, lease)
    assert seats.liveness(project, "codex-qa") == state
    with pytest.raises(KankanError) as err:
        qa.replace_seat(project, "claude")
    assert err.value.status == Status.NEEDS_ACTION and "codex-qa" in err.value.message
    assert project.reload_config().qa_seat() == "codex-qa"
    assert project.refs.read(card_ref(card.id)).meta["owner"] == "codex-qa"


def test_replace_seat_without_a_registered_qa_seat_does_nothing(kproject, fake_trello):
    add_qa_list(kproject, fake_trello)
    assert qa.replace_seat(kproject, "claude") is None
    assert kproject.reload_config().qa_seat() is None


def _launcher_completes(project):
    from kankan.adapters import load_adapter
    from kankan.qa_launcher import QaLauncher
    launcher = QaLauncher(project, load_adapter(project.paths, "claude"), None, "claude", "claude-qa")
    lease = launcher.acquire({})
    seats.release(project, lease)


@pytest.mark.parametrize("fault", ["qa_replace_registered", "qa_replace_ref", "qa_replace_seatref"])
@pytest.mark.parametrize("completion", ["rerun", "launcher"])
def test_a_crash_after_each_replacement_stage_is_completed(old_seat, monkeypatch, fault, completion):
    project, fake, lease, card, task, top = old_seat
    _dead(project, lease)
    monkeypatch.setenv("KANKAN_FAULT", fault)
    with pytest.raises(faults.SimulatedCrash):
        qa.replace_seat(project, "claude")
    monkeypatch.delenv("KANKAN_FAULT")
    assert project.reload_config().qa_seat() == "claude-qa"  # stage 2 is done in every case
    if completion == "rerun":
        assert qa.replace_seat(project, "claude") == "codex-qa"
    else:
        _launcher_completes(project)
    _assert_replaced(project, card, task, top)


def test_the_new_launcher_takes_up_the_replaced_conversation_whatever_is_on_top(old_seat):
    from kankan.adapters import load_adapter
    from kankan.qa_launcher import QaLauncher
    project, fake, lease, card, task, top = old_seat
    _dead(project, lease)
    qa.replace_seat(project, "claude")
    launcher = QaLauncher(project, load_adapter(project.paths, "claude"), None, "claude", "claude-qa")
    launcher.lease = launcher.acquire({})
    try:
        held = launcher.held_conversation()
        assert held is not None and held.meta["card"] == card.id
        taken = qa.take_over(project, launcher.lease, held, PAIR)
        assert taken["card"] == card.id and taken["conversation"] == task["conversation"]
    finally:
        seats.release(project, launcher.lease)


def test_a_launcher_of_the_replaced_seat_never_completes_a_replacement_back(old_seat):
    project, fake, lease, card, task, top = old_seat
    _dead(project, lease)
    qa.replace_seat(project, "claude")
    assert qa.replace_seat(project, "codex", initiate=False) is None
    assert project.reload_config().qa_seat() == "claude-qa"
    with pytest.raises(KankanError) as err:
        seats.acquire_qa(project, "codex", None)
    assert err.value.status == Status.CONFIG_ERROR


def test_an_old_supervisor_relaunching_the_replaced_seat_exits_fatally(old_seat, monkeypatch):
    import os
    import subprocess
    import sys
    from kankan.launcher import FATAL_EXIT
    from tests.conftest import ROOT
    project, fake, lease, card, task, top = old_seat
    _dead(project, lease)
    qa.replace_seat(project, "claude")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run([sys.executable, "-m", "kankan", "launcher", "--type=codex", "--seat=codex-qa"],
                          cwd=project.paths.repo, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == FATAL_EXIT, proc.stdout + proc.stderr
    assert project.reload_config().qa_seat() == "claude-qa" and "codex-qa" not in project.cfg.seats
    assert project.refs.read(card_ref(card.id)).meta["owner"] == "claude-qa"
    assert project.refs.read(seat_ref("codex-qa")) is None


def test_an_old_answer_paused_mid_answer_gets_not_owner_after_the_replacement(old_seat, monkeypatch):
    from tests.flow import agent_ctx
    from tests.test_qa_answer import long_text
    project, fake, lease, card, task, top = old_seat
    token = qa.start_turn(project, lease, task, qa.Inputs([], []))
    ctx = agent_ctx(project, lease, monkeypatch)
    _dead(project, lease)
    real = faults.point
    calls = []

    def point(name):
        if name == "qa_after_chunk" and not calls:
            calls.append(name)
            assert qa.replace_seat(project, "claude") == "codex-qa"
        real(name)
    monkeypatch.setattr(faults, "point", point)
    from kankan import qa_answer
    with pytest.raises(KankanError) as err:
        qa_answer.answer(ctx, long_text(), token)
    assert err.value.status == Status.NOT_OWNER
    posted = [text for text in fake.card_comments(card.id) if "`kankan:qa-" in text]
    assert len(posted) == 1
    assert project.refs.read(card_ref(card.id)).meta["owner"] == "claude-qa"


def test_end_admin_holds_the_project_lock_once_per_conversation(qa_env, monkeypatch):
    from kankan import config
    project, fake, lease, qa_list = qa_env
    first = qa.claim(project, lease, _top(project, fake, qa_list, "one"), PAIR)
    second_card = _top(project, fake, qa_list, "two")
    second = put_card_ref(project, second_card.id, second_card.short, phase="qa", owner=lease.seat,
                          target_list=qa_list, conversation="0123456789abcdef")
    seats.release(project, lease)
    holds = []
    real = config.project_lock

    @contextlib.contextmanager
    def counting(paths, *args, **kwargs):
        with real(paths, *args, **kwargs):
            holds.append(1)
            yield
    transactions = []
    real_tx = project.refs.transaction

    def tx(ops):
        transactions.append(len(holds))
        return real_tx(ops)
    monkeypatch.setattr(qa, "project_lock", counting)
    monkeypatch.setattr(project.refs, "transaction", tx)
    assert qa.end_admin(project, lease.seat) is True
    assert project.refs.read(card_ref(first["card"])) is None and project.refs.read(card_ref(second_card.id)) is None
    assert len(transactions) == 2 and transactions == sorted(set(transactions))  # one transaction per lock hold


def test_end_admin_ends_nothing_while_the_seat_ref_exists(qa_env):
    project, fake, lease, qa_list = qa_env
    task = qa.claim(project, lease, _top(project, fake, qa_list), PAIR)
    seats._release_fd(lease.fd)  # dead: the seat ref stays
    assert qa.end_admin(project, lease.seat) is False
    assert project.refs.read(card_ref(task["card"])).meta["phase"] == "qa"


def test_opposite_concurrent_replacements_never_leave_the_conversation_to_an_unregistered_seat(old_seat, monkeypatch):
    """A swaps codex-qa for claude-qa and pauses; B swaps back. The lifecycle lock serializes them, so the
    conversation always ends with the registered seat and the registered seat's worktree is never removed by A."""
    project, fake, lease, card, task, top = old_seat
    _dead(project, lease)
    paused, resume = threading.Event(), threading.Event()
    real = faults.point
    outcome = {}

    def point(name):
        if name == "qa_replace_registered" and threading.current_thread().name == "A":
            paused.set()
            resume.wait(10)
        real(name)
    monkeypatch.setattr(faults, "point", point)

    def run(name, agent_type):
        try:
            outcome[name] = qa.replace_seat(project, agent_type)
        except KankanError as err:
            outcome[name] = err
    first = threading.Thread(target=run, args=("A", "claude"), name="A", daemon=True)
    first.start()
    assert paused.wait(10)
    second = threading.Thread(target=run, args=("B", "codex"), name="B", daemon=True)
    second.start()
    second.join(1.0)  # B runs now unless the lifecycle lock keeps it waiting
    resume.set()
    first.join(30)
    second.join(30)
    registered = project.reload_config().qa_seat()
    assert project.refs.read(card_ref(card.id)).meta["owner"] == registered
    assert registered == "codex-qa" and outcome == {"A": "codex-qa", "B": "claude-qa"}


def test_replace_seat_checks_its_destination_is_still_registered_in_every_stage(old_seat, monkeypatch):
    from kankan.config import SeatConfig, save_config
    project, fake, lease, card, task, top = old_seat
    _dead(project, lease)
    real = faults.point

    def point(name):
        if name == "qa_replace_registered":  # another writer registers codex-qa again behind the replacement's back
            cfg = project.reload_config()
            del cfg.seats["claude-qa"]
            cfg.seats["codex-qa"] = SeatConfig(type="codex", role="qa")
            save_config(project.paths, cfg)
        real(name)
    monkeypatch.setattr(faults, "point", point)
    with pytest.raises(KankanError) as err:
        qa.replace_seat(project, "claude")
    assert err.value.status == Status.INELIGIBLE
    assert project.refs.read(card_ref(card.id)).meta["owner"] == "codex-qa"
    assert project.paths.seat_worktree("codex-qa").exists()


def test_a_crash_after_registration_then_seat_remove_of_the_new_seat_is_completed_later(old_seat, monkeypatch):
    from kankan import diag
    project, fake, lease, card, task, top = old_seat
    seats.release(project, lease)
    monkeypatch.setenv("KANKAN_FAULT", "qa_replace_registered")
    with pytest.raises(faults.SimulatedCrash):
        qa.replace_seat(project, "claude")
    monkeypatch.delenv("KANKAN_FAULT")
    remove_seat(project, "claude-qa")
    assert project.reload_config().qa_seat() is None
    issues = diag.reconcile(project)
    assert any(i.kind == "qa_owner_unregistered" and i.subject == card.id for i in issues)
    assert qa.replace_seat(project, "claude") == "codex-qa"
    _assert_replaced(project, card, task, top)
    assert not any(i.kind == "qa_owner_unregistered" for i in diag.reconcile(project))


def test_a_plain_seat_remove_is_not_taken_for_a_replacement(kproject, fake_trello):
    from kankan import auditlog, diag
    project, fake = kproject, fake_trello
    add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "codex", None)
    seats.release(project, lease)
    remove_seat(project, "codex-qa")
    assert list(project.paths.seats_dir.glob("codex-qa.*"))  # the seat files of a removed seat stay
    seats.release(project, seats.acquire_qa(project, "claude", None))  # `agent --qa claude` registers claude-qa
    assert qa.replace_seat(project, "claude", initiate=False) is None  # what its launcher runs before occupying
    assert not any(e["event"] == "qa_seat_replaced" for e in auditlog.read(project.paths))


def test_replacement_carries_the_old_seats_label_causes_for_the_moved_card(old_seat, monkeypatch):
    from kankan import labels
    project, fake, lease, card, task, top = old_seat
    labels.set_label(project, card.id, project.cfg.label_error, "fail", "codex-qa", comment="needs help", op_id="f1")
    _dead(project, lease)
    qa.replace_seat(project, "claude")
    causes = load_state(project.paths, "claude-qa").label_causes
    assert causes[card.id][project.cfg.label_error]["cause"] == "fail"
    labels.clear_label(project, card.id, project.cfg.label_error, "fail", "claude-qa")
    assert project.cfg.label_error not in fake.state["cards"][card.id]["idLabels"]


def test_a_replacement_record_that_can_no_longer_complete_is_removed(old_seat, monkeypatch):
    """Crash after stage 2 (codex → claude), `seat remove claude-qa`, then codex starts again: the record naming
    codex → claude goes, so a later plain `seat remove codex-qa` is never taken for a replacement."""
    from kankan import auditlog, diag
    project, fake, lease, card, task, top = old_seat
    seats.release(project, lease)
    monkeypatch.setenv("KANKAN_FAULT", "qa_replace_registered")
    with pytest.raises(faults.SimulatedCrash):
        qa.replace_seat(project, "claude")
    monkeypatch.delenv("KANKAN_FAULT")
    remove_seat(project, "claude-qa")
    assert qa.replace_seat(project, "codex", initiate=False) is None  # the codex launcher starts again
    seats.release(project, seats.acquire_qa(project, "codex", None))
    assert not qa._replacing_path(project.paths).exists()
    remove_seat(project, "codex-qa")
    assert qa.replace_seat(project, "claude", initiate=False) is None
    seats.release(project, seats.acquire_qa(project, "claude", None))
    assert not any(e["event"] == "qa_seat_replaced" for e in auditlog.read(project.paths))
