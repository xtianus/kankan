import json
from datetime import datetime, timezone

import pytest

from kankan import cli, clock, diag, seats, workflow


@pytest.fixture
def board(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc))
    working = fake.add_card(project.cfg.backlog, "Working card", pos=1)
    held = fake.add_card(project.cfg.backlog, "Held card", pos=2)
    fake.user_label(held["id"], project.cfg.label_hold)
    lease = seats.acquire(project, "claude")
    workflow.tick(project, lease)
    monkeypatch.chdir(project.paths.repo)
    return project, fake, lease, working, held


def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_status_golden(board, capsys):
    project, fake, lease, working, held = board
    code, out = run(capsys, "status")
    assert code == 0
    assert out.startswith("Seats:\n"
                   f"  claude-1   alive       stopped       impl {working['shortLink']}\n"
                   "Waiting cards:\n"
                   f"  {held['shortLink']}  hold             Held card\n")
    assert "Settings:" in out and "sonnet/high" in out
    code, out = run(capsys, "status", "--json")
    data = json.loads(out)["data"]
    assert data["seats"][0]["seat"] == "claude-1" and data["seats"][0]["liveness"] == "alive"
    assert data["waiting"] == [{"card": held["id"], "short": held["shortLink"], "title": "Held card", "reason": "hold"}]


def test_claims_golden(board, capsys):
    project, fake, lease, working, held = board
    code, out = run(capsys, "claims")
    assert out == f"{working['shortLink']}  impl         claude-1         0s  26kankan-{working['shortLink']}\n"
    code, out = run(capsys, "claims", "--json")
    rows = json.loads(out)["data"]
    assert rows == [{"card": working["id"], "short": working["shortLink"], "phase": "impl", "owner": "claude-1",
                     "age_s": 0, "branch": f"26kankan-{working['shortLink']}"}]


def test_status_reports_settings_without_writes(kproject, fake_trello):
    from kankan import boardconfig
    from kankan.config import atomic_write_json
    from kankan.seatstate import update_state
    lease = seats.acquire(kproject, 'claude')
    update_state(kproject.paths, lease.seat, lambda state: setattr(state, 'running_selection', {'model': 'sonnet', 'effort': 'high'}))
    state = boardconfig.SettingsState(settings={'implementation': {'value': {'claude': {'model': 'opus', 'effort': 'max'}}, 'invalid': True, 'card_id': 'settings-card'}})
    atomic_write_json(kproject.paths.kankan_dir / 'board-settings.json', state.to_dict())
    before = {str(p): p.read_bytes() for p in kproject.paths.kankan_dir.rglob('*') if p.is_file()}
    data = diag.status(kproject)
    assert data['settings']['implementation']['value']['claude'] == {'model': 'opus', 'effort': 'max'}
    assert data['settings']['implementation']['structural_error']
    assert data['seats'][0]['running_selection'] == {'model': 'sonnet', 'effort': 'high'}
    assert data['settings_warning']
    after = {str(p): p.read_bytes() for p in kproject.paths.kankan_dir.rglob('*') if p.is_file()}
    assert before == after


def test_status_sorts_qa_seat_and_warns_only_when_relevant(kproject, fake_trello):
    from kankan import messages
    from tests.helpers import add_qa_list
    held = [seats.acquire(kproject, "claude"), seats.acquire(kproject, "claude")]
    assert diag.status(kproject)["qa"] is None  # no Q&A seat, no qa_list
    seats.ensure_created(kproject, "claude-qa", "claude")
    data = diag.status(kproject)
    assert [row["seat"] for row in data["seats"]] == ["claude-1", "claude-2", "claude-qa"]
    assert data["qa"]["warning"] == messages.QA_LIST_MISSING
    assert messages.QA_LIST_MISSING in diag.status_text(data)
    ident = add_qa_list(kproject, fake_trello)
    assert diag.status(kproject)["qa"]["warning"] is None
    fake_trello.state["lists"][ident]["closed"] = True
    assert diag.status(kproject)["qa"]["warning"] == messages.QA_LIST_MISSING


def test_status_warns_for_configured_missing_qa_list_without_seat(kproject, fake_trello):
    from kankan import messages
    from tests.helpers import add_qa_list
    ident = add_qa_list(kproject, fake_trello)
    assert diag.status(kproject)["qa"]["warning"] is None
    fake_trello.state["lists"][ident]["closed"] = True
    assert diag.status(kproject)["qa"]["warning"] == messages.QA_LIST_MISSING


def test_status_shows_the_qa_seat_its_active_card_and_the_queue(kproject, fake_trello, capsys, monkeypatch):
    from kankan import messages, qa
    from kankan.adapters import ModelEffort
    from tests.helpers import add_qa_list
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "claude", None)
    active = fake.add_card(qa_list, "Active question")
    task = qa.claim(project, lease, project.trello.card(active["id"]), ModelEffort("sonnet", "high"))
    turn = qa.start_turn(project, lease, task, qa.Inputs([], []))
    fake.add_card(qa_list, "Second question")
    fake.add_card(qa_list, "Third question")
    data = diag.status(project)
    assert data["qa"] == {"seat": "claude-qa", "liveness": "alive", "launcher_state": "stopped", "card": active["id"],
                          "short": active["shortLink"], "turn": turn, "queue": 2, "warning": None}
    text = diag.status_text(data)
    assert messages.STATUS_QA.format(seat="claude-qa", liveness="alive", state="stopped", card=active["shortLink"],
                                     queue=2) in text
    monkeypatch.chdir(project.paths.repo)
    code, out = run(capsys, "status", "--json")
    assert json.loads(out)["data"]["qa"]["card"] == active["id"]
    # the seat stops: its conversation is kept, and the cards of Q&A wait
    seats.release(project, lease)
    data = diag.status(project)
    assert data["qa"]["liveness"] == "free" and data["qa"]["card"] == active["id"]
    assert data["qa"]["warning"] == messages.QA_CARDS_WAIT
    assert messages.QA_CARDS_WAIT in diag.status_text(data)
    # the Q&A list archived: the missing-list warning prevails
    fake.state["lists"][qa_list]["closed"] = True
    assert diag.status(project)["qa"]["warning"] == messages.QA_LIST_MISSING


def test_status_without_a_qa_seat_warns_that_qa_cards_wait(kproject, fake_trello):
    from kankan import messages
    from tests.helpers import add_qa_list
    project, fake = kproject, fake_trello
    assert diag.status(project)["qa"] is None  # the feature is not configured
    qa_list = add_qa_list(project, fake)
    data = diag.status(project)
    assert data["qa"]["seat"] is None and data["qa"]["queue"] == 0 and data["qa"]["warning"] is None
    fake.add_card(qa_list, "question")
    data = diag.status(project)
    assert data["qa"]["queue"] == 1 and data["qa"]["warning"] == messages.QA_CARDS_WAIT
    assert messages.STATUS_QA_NONE.format(queue=1) in diag.status_text(data)


def test_status_with_only_a_qa_seat_alive_keeps_the_work_reasons(kproject, fake_trello):
    from tests.flow import implement
    from tests.helpers import add_qa_list
    project, fake = kproject, fake_trello
    add_qa_list(project, fake)
    claude = seats.acquire(project, "claude")
    card = implement(project, fake, claude)
    assert {row["card"]: row["reason"] for row in diag.status(project)["waiting"]}[card["id"]] == "no_reviewer_type"
    seats.release(project, claude)
    qa_lease = seats.acquire_qa(project, "codex", None)  # a codex Q&A seat is no codex reviewer
    try:
        reasons = {row["card"]: row["reason"] for row in diag.status(project)["waiting"]}
    finally:
        seats.release(project, qa_lease)
    assert reasons[card["id"]] == "no_reviewer_type"


def test_status_shows_invalid_headings(board, capsys):
    project, fake, lease, working, held = board
    bad = fake.add_card(project.cfg.backlog, "Bad headings", desc="spec\n## agent: cluade\nx\n", pos=3)
    fake.state["cards"][held["id"]]["desc"] = "spec\n## agent: cluade\nx\n"
    rows = {row["card"]: row for row in diag.status(project)["waiting"]}
    assert rows[bad["id"]]["reason"] == "invalid_headings" and rows[bad["id"]]["problems"].startswith("line 2: ")
    assert rows[held["id"]]["reason"] == "hold"
    assert "line 2: `cluade` is not an agent type" in diag.status_text(diag.status(project))
    fake.state["cards"][bad["id"]]["desc"] = "fixed\n"
    assert bad["id"] not in {row["card"] for row in diag.status(project)["waiting"]}
