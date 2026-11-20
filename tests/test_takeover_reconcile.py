"""Stranded cards: reported by reconcile and moved back to Backlog by reconcile --fix (takeover F§3)."""

from kankan import diag, seats
from tests.conftest import git


def kinds(issues):
    return {(i.kind, i.subject) for i in issues}


def test_stranded_cards_are_reported_and_moved(kproject, fake_trello):
    project, fake = kproject, fake_trello
    seat_list = fake.add_list(project.cfg.board_id, "claude-7 · Implemented")  # an unregistered seat's list
    stranded = fake.add_card(seat_list["id"], "half done")
    blocked = fake.add_card(project.cfg.blocked, "blocked elsewhere")
    first = fake.add_card(project.cfg.backlog, "first")
    found = kinds(diag.reconcile(project))
    assert ("card_without_ref", stranded["id"]) in found and ("blocked_without_ref", blocked["id"]) in found
    fixed = [i for i in diag.reconcile(project, fix=True) if i.kind == "stranded_card_moved"]
    assert {i.subject for i in fixed} == {stranded["id"], blocked["id"]}
    for card in (stranded, blocked):
        assert fake.card_list(card["id"]) == project.cfg.backlog
        assert any("moved back to Backlog" in text for text in fake.card_comments(card["id"]))
    backlog = sorted((c for c in fake.state["cards"].values() if c["idList"] == project.cfg.backlog),
                     key=lambda c: c["pos"])
    assert backlog[-1]["id"] == first["id"]  # stranded cards go to the top
    assert not [i for i in diag.reconcile(project) if i.kind in ("card_without_ref", "blocked_without_ref")]


def test_alive_seat_and_blocked_worktree_are_left_alone(kproject, fake_trello):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    in_progress = project.reload_config().seats[lease.seat].in_progress
    card = fake.add_card(in_progress, "being claimed")
    blocked = fake.add_card(project.cfg.blocked, "blocked here")
    path = project.paths.worktree_root / f"blocked-{blocked['shortLink']}"
    git(project.paths.repo, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    diag.reconcile(project, fix=True)
    assert fake.card_list(card["id"]) == in_progress
    assert fake.card_list(blocked["id"]) == project.cfg.blocked
    seats.release(project, lease)
