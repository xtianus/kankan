"""Regressions for the ultrareview findings on the board-settings branch."""
import json
import subprocess

import pytest

from kankan import auditlog, diag, model_catalog, modelcheck, seats, seeds, workflow
from kankan.adapters import Classification, ModelEffort, load_adapter
from kankan.config import atomic_write_json, load_config
from kankan.errors import KankanError, Status
from kankan.launcher import Launcher
from kankan.refstore import card_ref
from kankan.seatstate import load_state, update_state
from tests.test_ask import FakeTmux
from tests.test_trello import board, client, fake, virtual_time  # noqa: F401 - fixtures


def make_launcher(project, lease):
    tmux = FakeTmux()
    launcher = Launcher(project, load_adapter(project.paths, 'claude'), tmux, 'claude')
    launcher.lease, launcher.pane, launcher.session = lease, '%1', 'session'
    launcher.running_pair = ModelEffort('sonnet', 'high')
    return launcher, tmux


def events(project, name):
    return [e for e in auditlog.read(project.paths) if e['event'] == name]


def test_malformed_config_json_is_a_config_error(kproject):
    (kproject.paths.kankan_dir / 'config.json').write_text('{not json')
    with pytest.raises(KankanError) as exc:
        load_config(kproject.paths)
    assert exc.value.status == Status.CONFIG_ERROR


def test_config_error_during_a_task_never_ends_it(kproject):
    lease = seats.acquire(kproject, 'claude')
    launcher, _ = make_launcher(kproject, lease)
    launcher.state, launcher.task = 'working', {'id': 't1', 'card': 'c'}
    before = kproject.cfg
    (kproject.paths.kankan_dir / 'config.json').write_text('{not json')
    launcher.refresh_settings()
    launcher.refresh_settings()
    assert launcher.stop_after_task and kproject.cfg is before
    assert len(events(kproject, 'settings_refresh_failed')) == 1  # logged once, not every second


def claimed(kproject, fake_trello, lease):
    card = fake_trello.add_card(kproject.cfg.backlog, 'work')
    assert modelcheck.check(load_adapter(kproject.paths, 'claude'), ModelEffort('sonnet', 'high')).status == 'passed'
    prepared = workflow.tick(kproject, lease, defer_assignment=True)
    assert prepared and prepared['card'] == card['id']
    return card, prepared


def test_transient_activation_failure_is_retried(kproject, fake_trello, monkeypatch):
    lease = seats.acquire(kproject, 'claude')
    card, prepared = claimed(kproject, fake_trello, lease)
    launcher, tmux = make_launcher(kproject, lease)
    launcher.prepared = prepared
    real = workflow.activate_task
    monkeypatch.setattr(workflow, 'activate_task', lambda *a: (_ for _ in ()).throw(KankanError(Status.TRELLO_ERROR, 'blip')))
    assert launcher.idle(Classification('ready')) is False
    assert launcher.prepared is prepared and launcher.next_activation > 0
    monkeypatch.setattr(workflow, 'activate_task', real)
    launcher.next_activation = 0.0
    assert launcher.idle(Classification('ready')) is False
    assert launcher.prepared is None and load_state(kproject.paths, lease.seat).task['card'] == card['id']
    assert tmux.pastes


def test_lost_seat_at_activation_exits_cleanly(kproject, fake_trello, monkeypatch):
    lease = seats.acquire(kproject, 'claude')
    _, prepared = claimed(kproject, fake_trello, lease)
    launcher, tmux = make_launcher(kproject, lease)
    launcher.prepared = prepared
    monkeypatch.setattr(workflow, 'activate_task', lambda *a: (_ for _ in ()).throw(KankanError(Status.NOT_OWNER, 'recovered')))
    assert launcher.idle(Classification('ready')) is True
    assert launcher.step() is True and tmux.pastes == []


def test_stop_before_activation_gives_fresh_claim_back(kproject, fake_trello):
    lease = seats.acquire(kproject, 'claude')
    card, prepared = claimed(kproject, fake_trello, lease)
    launcher, tmux = make_launcher(kproject, lease)
    launcher.prepared = prepared
    update_state(kproject.paths, lease.seat, lambda state: setattr(state, 'stop_requested', True))
    assert launcher.idle(Classification('ready')) is True
    assert tmux.pastes == [] and fake_trello.card_list(card['id']) == kproject.cfg.backlog
    assert kproject.refs.read(card_ref(card['id'])).meta.get('owner') is None


def test_stop_before_prompt_gives_fresh_claim_back(kproject, fake_trello):
    from kankan import messages
    lease = seats.acquire(kproject, 'claude')
    card = fake_trello.add_card(kproject.cfg.backlog, 'work')
    task = workflow.tick(kproject, lease)
    launcher, tmux = make_launcher(kproject, lease)
    launcher.task, launcher.pending_writes = task, [messages.PROMPT]
    update_state(kproject.paths, lease.seat, lambda state: setattr(state, 'stop_requested', True))
    launcher.flush_assignment(Classification('ready'))
    assert tmux.pastes == [] and fake_trello.card_list(card['id']) == kproject.cfg.backlog
    assert load_state(kproject.paths, lease.seat).task is None


def test_stop_keeps_resumed_work(kproject, fake_trello):
    lease = seats.acquire(kproject, 'claude')
    card, prepared = claimed(kproject, fake_trello, lease)
    held = kproject.trello.card(card['id']).list_id
    assert workflow.release_unsent(kproject, lease, dict(prepared, resumed=True)) is False
    assert fake_trello.card_list(card['id']) == held


def test_failed_recheck_is_paced(kproject, fake_trello, monkeypatch):
    from kankan import messages
    lease = seats.acquire(kproject, 'claude')
    fake_trello.add_card(kproject.cfg.backlog, 'work')
    task = workflow.tick(kproject, lease)
    launcher, tmux = make_launcher(kproject, lease)
    launcher.task, launcher.pending_writes = task, [messages.PROMPT]
    calls = []
    def fail(*args):
        calls.append(1)
        raise KankanError(Status.NETWORK_ERROR, 'down')
    monkeypatch.setattr(workflow, 'refresh_task', fail)
    for _ in range(3):
        launcher.flush_assignment(Classification('ready'))
    assert len(calls) == 1 and launcher.pending_writes and tmux.pastes == []


def write_catalog(models):
    path = model_catalog.cache_path('codex')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 'codex 9', 'source': 'command', 'observed_at': 1, 'models': models}))


def test_catalog_dropping_a_seeded_pair_makes_it_a_missing_seed(kproject, monkeypatch):
    write_catalog({'gpt-new': {'efforts': ['high', 'low']}})
    user = kproject.paths.kankan_dir / 'agents.json'
    stale = {'model': 'gpt-retired', 'effort': 'high'}
    atomic_write_json(user, {'codex': {'seed': {'implementation': stale, 'review': {'model': 'gpt-new', 'effort': 'low'}}}})
    adapter = load_adapter(kproject.paths, 'codex')
    assert 'implementation' not in adapter.seed and adapter.stale_seeds == {'implementation': stale}
    assert adapter.seed['review'] == {'model': 'gpt-new', 'effort': 'low'}
    with pytest.raises(KankanError) as exc:
        seeds.ensure(kproject.paths, ['codex'], interactive=False, say=lambda text: None)
    assert exc.value.status == Status.NEEDS_ACTION


def test_explicit_models_still_reject_an_invalid_seed(kproject):
    write_catalog({'gpt-new': {'efforts': ['high']}})
    atomic_write_json(kproject.paths.kankan_dir / 'agents.json', {'codex': {
        'models': {'m': {'efforts': ['high']}}, 'seed': {'implementation': {'model': 'x', 'effort': 'high'}}}})
    with pytest.raises(KankanError) as exc:
        load_adapter(kproject.paths, 'codex')
    assert exc.value.status == Status.CONFIG_ERROR


def test_seed_answer_replaces_a_stale_seed(isolated_home):
    target = isolated_home / '.config/kankan/agents.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    stale = {'model': 'old', 'effort': 'high'}
    target.write_text(json.dumps({'codex': {'seed': {'implementation': stale}}}))
    seeds.save(None, 'codex', 'implementation', {'model': 'new', 'effort': 'high'}, stale=stale)
    assert json.loads(target.read_text())['codex']['seed']['implementation'] == {'model': 'new', 'effort': 'high'}
    seeds.save(None, 'codex', 'implementation', {'model': 'other', 'effort': 'high'})
    assert json.loads(target.read_text())['codex']['seed']['implementation']['model'] == 'new'


def test_unavailable_entry_without_start_time_is_never_signalled():
    child = subprocess.Popen(['sleep', '30'])
    try:
        proc = {'unavailable': [{'pid': child.pid, 'start': None}]}
        assert seats._recorded_alive(proc) == []
    finally:
        child.kill()
        child.wait(5)


def test_survivor_gone_before_its_start_time_is_not_recorded(kproject):
    lease = seats.acquire(kproject, 'claude')
    launcher, _ = make_launcher(kproject, lease)
    child = subprocess.Popen(['true'])
    child.wait(5)
    launcher.mark_unavailable([child.pid])
    assert not launcher.unsafe and 'unavailable' not in (seats.read_proc(kproject, lease.seat) or {})


def test_declined_list_is_never_adopted_after_a_failed_create(fake, board, virtual_time):  # noqa: F811
    b = board[0]
    existing = fake.add_list(b['id'], 'kankan')
    fake.inject('POST', '^/1/lists$', 'timeout')
    created = client(fake).create_list(b['id'], 'kankan', exclude={existing['id']})
    assert created.id != existing['id'] and len(fake.list_by_name(b['id'], 'kankan')) == 2


def test_status_shows_running_selection_only_for_live_seats(kproject):
    lease = seats.acquire(kproject, 'claude')
    update_state(kproject.paths, lease.seat, lambda state: setattr(state, 'running_selection', {'model': 'opus', 'effort': 'high'}))
    launcher, _ = make_launcher(kproject, lease)
    launcher.shutdown()
    assert load_state(kproject.paths, lease.seat).running_selection is None
    update_state(kproject.paths, lease.seat, lambda state: setattr(state, 'running_selection', {'model': 'opus', 'effort': 'high'}))
    row = next(r for r in diag.status(kproject)['seats'] if r['seat'] == lease.seat)
    assert row['liveness'] != 'alive' and row['running_selection'] is None


def test_seed_outside_shipped_list_without_catalog_is_missing(kproject):
    model_catalog.cache_path('codex').unlink(missing_ok=True)
    atomic_write_json(kproject.paths.kankan_dir / 'agents.json', {'codex': {'seed': {
        'implementation': {'model': 'gpt-from-an-old-catalog', 'effort': 'high'}}}})
    adapter = load_adapter(kproject.paths, 'codex')
    assert 'implementation' not in adapter.seed and 'implementation' in adapter.stale_seeds


def test_stale_project_seed_is_replaced_in_its_own_file(kproject, monkeypatch):
    write_catalog({'gpt-new': {'efforts': ['high', 'low']}})
    project_file = kproject.paths.kankan_dir / 'agents.json'
    atomic_write_json(project_file, {'codex': {'seed': {
        'implementation': {'model': 'gpt-retired', 'effort': 'high'}, 'review': {'model': 'gpt-new', 'effort': 'low'}}}})
    monkeypatch.setattr(seeds.sys.stdin, 'isatty', lambda: True, raising=False)
    monkeypatch.setattr(seeds.sys.stdout, 'isatty', lambda: True, raising=False)
    monkeypatch.setattr(seeds, 'read_key', lambda: '1')
    said = []
    adapters = seeds.ensure(kproject.paths, ['codex'], interactive=True, say=said.append)
    assert adapters['codex'].seed['implementation'] == {'model': 'gpt-new', 'effort': 'high'}
    assert json.loads(project_file.read_text())['codex']['seed']['implementation']['model'] == 'gpt-new'
    assert any('no longer' in text for text in said)


def test_probe_that_cannot_start_is_unverified_not_rejected(isolated_home):
    adapter = load_adapter(None, 'claude')
    adapter.model_check = ['/nonexistent/kankan-probe', '{model}', '{effort}']
    adapter._check_version = 'claude 1'
    result = modelcheck.PendingCheck(adapter, ModelEffort('sonnet', 'high'), 'claude 1', None).poll()
    assert result.status == 'timeout' and result.retry_at
    assert modelcheck.cached(adapter, ModelEffort('sonnet', 'high')).status != 'failed'
