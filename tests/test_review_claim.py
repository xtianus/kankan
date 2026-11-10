import pytest

from kankan import seats, workflow, worktrees
from kankan.cards import CardMeta, target_list_for, transition
from kankan.errors import KankanError
from kankan.refstore import RefValue, card_ref
from tests.conftest import git


@pytest.fixture
def board(kproject, fake_trello):
    claude = seats.acquire(kproject, "claude")
    codex = seats.acquire(kproject, "codex")
    kproject.reload_config()
    return kproject, fake_trello, claude, codex


def implement(project, fake, lease, name="feature"):
    """Claim a Backlog card with ``lease``, commit on its branch and close it as implemented."""
    card = fake.add_card(project.cfg.backlog, name, pos=0)
    task = workflow.tick(project, lease)
    assert task["card"] == card["id"]
    wt = project.paths.seat_worktree(lease.seat)
    (wt / f"{name}.txt").write_text(name)
    git(wt, "add", ".")
    git(wt, "commit", "-qm", name)
    worktrees.detach(wt)
    value = project.refs.read(card_ref(card["id"]))
    new = CardMeta.from_dict(value.meta).evolve(phase="implemented", owner=None, session=None,
                                                 target_list=target_list_for(project.cfg, "implemented", None, lease.seat))
    transition(project, lease, project.trello.card(card["id"]), value, new, None)
    return card


def test_review_claim_end_to_end(board):
    project, fake, claude, codex = board
    card = implement(project, fake, claude)
    fake.add_card(project.cfg.backlog, "later", pos=99)
    task = workflow.tick(project, codex)
    assert task["kind"] == "review" and task["card"] == card["id"]
    assert fake.card_list(card["id"]) == project.cfg.seats["codex-1"].review
    assert worktrees.current_branch(project.paths.seat_worktree("codex-1")) == task["branch"]
    meta = project.refs.read(card_ref(card["id"])).meta
    assert meta["phase"] == "review" and meta["owner"] == "codex-1" and meta["implementer"] == "claude-1"


def ref(meta):
    return RefValue("0" * 40, meta)


def test_priority_own_review_impl(board):
    project, fake, claude, codex = board
    cfg = project.cfg
    own_impl = fake.add_card(cfg.seats["codex-1"].in_progress, "own impl")
    own_review = fake.add_card(cfg.seats["codex-1"].review, "own review")
    to_review = fake.add_card(cfg.seats["claude-1"].implemented, "to review")
    backlog = fake.add_card(cfg.backlog, "backlog")
    refs = {
        own_impl["id"]: ref({"phase": "impl", "owner": "codex-1", "target_list": cfg.seats["codex-1"].in_progress}),
        own_review["id"]: ref({"phase": "review", "owner": "codex-1", "target_list": cfg.seats["codex-1"].review}),
        to_review["id"]: ref({"phase": "implemented", "implementer": "claude-1", "implementer_type": "claude"}),
    }
    snap = project.trello.snapshot(cfg.board_id)
    order = []
    skip = set()
    while True:
        choice = workflow.choose(project, codex, snap, refs, skip)
        if choice is None:
            break
        order.append((choice.kind, choice.card.id))
        skip.add(choice.card.id)
    assert order == [("resume_review", own_review["id"]), ("resume_impl", own_impl["id"]),
                     ("review", to_review["id"]), ("impl", backlog["id"])]


def test_cross_type_from_ref_not_list(board):
    project, fake, claude, codex = board
    cfg = project.cfg
    card = fake.add_card(cfg.seats["claude-1"].implemented, "moved by hand")
    refs = {card["id"]: ref({"phase": "implemented", "implementer": "codex-1", "implementer_type": "codex"})}
    snap = project.trello.snapshot(cfg.board_id)
    assert [c.card.id for c in workflow.review_candidates(project, claude, snap, refs)] == [card["id"]]
    assert workflow.review_candidates(project, codex, snap, refs) == []


def test_single_type_leaves_cards_waiting(board):
    project, fake, claude, codex = board
    card = fake.add_card(project.cfg.seats["claude-1"].implemented, "c")
    refs = {card["id"]: ref({"phase": "implemented", "implementer": "claude-1", "implementer_type": "claude"})}
    snap = project.trello.snapshot(project.cfg.board_id)
    assert workflow.waiting_reasons(project, snap, refs, types={"claude"}) == {card["id"]: "no_reviewer_type"}
    from kankan.adapters import load_adapter, ModelEffort
    from kankan.modelcheck import check
    check(load_adapter(project.paths, 'codex'), ModelEffort('gpt-6.1-sol', 'high'))
    assert workflow.waiting_reasons(project, snap, refs, types={"claude", "codex"}) == {}


def test_clean_blocked_worktree_removed(board):
    project, fake, claude, codex = board
    card = implement(project, fake, claude)
    branch = project.refs.read(card_ref(card["id"])).meta["branch"]
    blocked = project.paths.blocked_worktree(card["shortLink"])
    git(project.paths.repo, "worktree", "add", "-q", str(blocked), branch)
    task = workflow.tick(project, codex)
    assert task["card"] == card["id"] and not blocked.exists()


def test_unfinished_merge_not_assigned(board):
    project, fake, claude, codex = board
    card = implement(project, fake, claude)
    branch = project.refs.read(card_ref(card["id"])).meta["branch"]
    blocked = project.paths.blocked_worktree(card["shortLink"])
    git(project.paths.repo, "worktree", "add", "-q", str(blocked), branch)
    (blocked / "wip.txt").write_text("x")
    assert workflow.tick(project, codex) is None
    assert workflow.tick(project, codex) is None
    comments = [c for c in fake.card_comments(card["id"]) if "merge not completed" in c]
    assert len(comments) == 1


def test_branch_elsewhere_error(board, tmp_path):
    project, fake, claude, codex = board
    card = implement(project, fake, claude)
    branch = project.refs.read(card_ref(card["id"])).meta["branch"]
    git(project.paths.repo, "worktree", "add", "-q", str(tmp_path / "elsewhere"), branch)
    assert workflow.tick(project, codex) is None
    assert project.cfg.label_error in fake.state["cards"][card["id"]]["idLabels"]
