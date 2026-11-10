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
