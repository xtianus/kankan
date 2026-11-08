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
    assert out == ("Seats:\n"
                   f"  claude-1   alive       stopped       impl {working['shortLink']}\n"
                   "Waiting cards:\n"
                   f"  {held['shortLink']}  hold             Held card\n")
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
