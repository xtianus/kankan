import pytest

from kankan import seats, workflow, worktrees
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref
from kankan.seatstate import load_state
from tests.conftest import git


@pytest.fixture
def board(kproject, fake_trello):
    return kproject, fake_trello


def add(fake, project, name, list_id=None, **kw):
    return fake.add_card(list_id or project.cfg.backlog, name, **kw)


def test_tick_claims_top_backlog_card(board):
    project, fake = board
    first = add(fake, project, "first", pos=10)
    add(fake, project, "second", pos=20)
    lease = seats.acquire(project, "claude")
    task = workflow.tick(project, lease)
    assert task["card"] == first["id"] and task["kind"] == "impl" and task["resumed"] is False
    assert fake.card_list(first["id"]) == project.cfg.seats["claude-1"].in_progress
    assert load_state(project.paths, "claude-1").task == task
    wt = project.paths.seat_worktree("claude-1")
    assert worktrees.current_branch(wt) == task["branch"]
    assert task["branch"].endswith("kankan-" + first["shortLink"])
    assert any("started implementing" in c for c in fake.card_comments(first["id"]))


def test_two_seats_parallel_distinct_cards(board):
    project, fake = board
    add(fake, project, "a")
    add(fake, project, "b")
    one, two = seats.acquire(project, "claude"), seats.acquire(project, "codex")
    t1, t2 = workflow.tick(project, one), workflow.tick(project, two)
    assert t1["card"] != t2["card"]


def test_stale_snapshot_only_one_claims(board):
    project, fake = board
    add(fake, project, "a")
    one, two = seats.acquire(project, "claude"), seats.acquire(project, "claude")
    project.reload_config()
    snap = project.trello.snapshot(project.cfg.board_id)
    c1 = workflow.choose(project, one, snap, {})
    c2 = workflow.choose(project, two, snap, {})
    assert c1.card.id == c2.card.id
    workflow.claim_impl(project, one, c1)
    with pytest.raises(KankanError) as exc:
        workflow.claim_impl(project, two, c2)
    assert exc.value.status == Status.ALREADY_CLAIMED


def test_order_own_then_backlog_by_position(board):
    project, fake = board
    low = add(fake, project, "low", pos=50)
    lease = seats.acquire(project, "claude")
    first = workflow.tick(project, lease)
    assert first["card"] == low["id"]
    top = add(fake, project, "top", pos=1)
    again = workflow.tick(project, lease)
    assert again["card"] == low["id"] and again["kind"] == "impl" and again["resumed"] is True
    assert again["id"] != first["id"]
    assert top["id"] != again["card"]


def test_hold_card_not_chosen(board):
    project, fake = board
    held = add(fake, project, "held", pos=1)
    fake.user_label(held["id"], project.cfg.label_hold)
    free = add(fake, project, "free", pos=2)
    lease = seats.acquire(project, "claude")
    assert workflow.tick(project, lease)["card"] == free["id"]


def test_branch_conflict_sets_error_and_skips(board):
    project, fake = board
    bad = add(fake, project, "bad", pos=1)
    good = add(fake, project, "good", pos=2)
    git(project.paths.repo, "branch", f"25kankan-{bad['shortLink']}")
    git(project.paths.repo, "branch", f"26kankan-{bad['shortLink']}")
    lease = seats.acquire(project, "claude")
    assert workflow.tick(project, lease)["card"] == good["id"]
    assert project.cfg.label_error in fake.state["cards"][bad["id"]]["idLabels"]
    assert project.refs.read(card_ref(bad["id"])) is None


def test_existing_branch_reused(board):
    project, fake = board
    card = add(fake, project, "c")
    git(project.paths.repo, "branch", f"25kankan-{card['shortLink']}")
    lease = seats.acquire(project, "claude")
    assert workflow.tick(project, lease)["branch"] == f"25kankan-{card['shortLink']}"


def test_unknown_lists_ignored_and_renamed_lists_work(board):
    project, fake = board
    foreign = fake.add_list(project.cfg.board_id, "Ideas")
    fake.add_card(foreign["id"], "not mine", pos=0)
    fake.state["lists"][project.cfg.backlog]["name"] = "Da fare"
    card = add(fake, project, "real")
    lease = seats.acquire(project, "claude")
    assert workflow.tick(project, lease)["card"] == card["id"]


def test_archived_workflow_list_config_error(board):
    project, fake = board
    lease = seats.acquire(project, "claude")
    fake.state["lists"][project.cfg.done]["closed"] = True
    with pytest.raises(KankanError) as exc:
        workflow.tick(project, lease)
    assert exc.value.status == Status.CONFIG_ERROR


def test_no_work_returns_none(board):
    project, _ = board
    lease = seats.acquire(project, "claude")
    assert workflow.tick(project, lease) is None


def test_waiting_reasons_hold(board):
    project, fake = board
    held = add(fake, project, "held")
    fake.user_label(held["id"], project.cfg.label_hold)
    snap = project.trello.snapshot(project.cfg.board_id)
    assert workflow.waiting_reasons(project, snap, {}, types=set()) == {held["id"]: "hold"}
