import pytest

from kankan import deps, seats, workflow
from kankan.errors import KankanError, Status


@pytest.fixture
def board(kproject, fake_trello):
    return kproject, fake_trello


def link(fake, card, dep):
    fake.add_attachment(card["id"], f"https://trello.com/c/{dep['shortLink']}/12-some-title")


def status(project, card):
    snap = project.trello.snapshot(project.cfg.board_id)
    return deps.dep_status(project, snap, snap.cards[card["id"]])


def test_attachment_short():
    assert deps.attachment_short("https://trello.com/c/Ab12Cd34/5-x") == "Ab12Cd34"
    assert deps.attachment_short("https://trello.com/c/Ab12Cd34") == "Ab12Cd34"
    assert deps.attachment_short("https://example.com/c/Ab12Cd34") is None
    assert deps.attachment_short("https://trello.com/b/Ab12Cd34/board") is None


def test_dependency_waits_until_done(board):
    project, fake = board
    dep = fake.add_card(project.cfg.backlog, "dep", pos=50)
    card = fake.add_card(project.cfg.backlog, "card", pos=1)
    link(fake, card, dep)
    assert status(project, card).waiting == [dep["shortLink"]]
    lease = seats.acquire(project, "claude")
    assert workflow.tick(project, lease)["card"] == dep["id"]
    fake.user_move(dep["id"], project.cfg.done)
    other = seats.acquire(project, "claude")
    assert workflow.tick(project, other)["card"] == card["id"]


def test_missing_other_board_cycle_reported(board):
    project, fake = board
    card = fake.add_card(project.cfg.backlog, "card")
    fake.add_attachment(card["id"], "https://trello.com/c/Zz99Zz99/1-gone")
    other_board = fake.add_board("other")
    foreign = fake.add_card(fake.add_list(other_board["id"], "L")["id"], "foreign")
    link(fake, card, foreign)
    a = fake.add_card(project.cfg.backlog, "a")
    b = fake.add_card(project.cfg.backlog, "b")
    link(fake, a, b)
    link(fake, b, a)
    problems = dict((r, s) for s, r in status(project, card).problems)
    assert problems == {"missing": "Zz99Zz99", "other_board": foreign["shortLink"]}
    assert ("cycle" in [r for _, r in status(project, a).problems])
    snap = project.trello.snapshot(project.cfg.board_id)
    reasons = workflow.waiting_reasons(project, snap, {}, types=set())
    assert reasons[card["id"]] == "deps" and reasons[a["id"]] == "deps"


def test_non_card_attachments_ignored(board):
    project, fake = board
    card = fake.add_card(project.cfg.backlog, "card")
    fake.add_attachment(card["id"], "https://example.com/spec.pdf")
    assert status(project, card).ok


def test_archived_dep_in_done_satisfied(board):
    project, fake = board
    dep = fake.add_card(project.cfg.done, "dep")
    archived_elsewhere = fake.add_card(project.cfg.backlog, "arch")
    card = fake.add_card(project.cfg.backlog, "card")
    link(fake, card, dep)
    fake.user_archive(dep["id"])
    assert status(project, card).ok
    link(fake, card, archived_elsewhere)
    fake.user_archive(archived_elsewhere["id"])
    assert status(project, card).problems == [(archived_elsewhere["shortLink"], "archived_not_done")]


def test_unreadable_dep_network_error_not_eligible(board, virtual_time):
    project, fake = board
    card = fake.add_card(project.cfg.backlog, "card")
    fake.add_attachment(card["id"], "https://trello.com/c/Qq11Qq11/1-x")
    snap = project.trello.snapshot(project.cfg.board_id)
    fake.inject("GET", "^/1/cards/Qq11Qq11", "500", times=100)
    result = deps.dep_status(project, snap, snap.cards[card["id"]])
    assert not result.ok and "unreadable" in result.problems[0][1]
    assert not workflow.deps_satisfied(project, snap, snap.cards[card["id"]])
