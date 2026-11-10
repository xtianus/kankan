from types import SimpleNamespace

from kankan import boardconfig, boardsettings, modelcheck, seats, workflow
from kankan.adapters import ModelEffort, load_adapter
from kankan.config import atomic_write_json
from kankan.refstore import card_ref
from kankan.seatstate import load_state, update_state


def test_deferred_activation_keeps_claim_selection(kproject, fake_trello):
    lease = seats.acquire(kproject, 'claude')
    card = fake_trello.add_card(kproject.cfg.backlog, 'work')
    pair = ModelEffort('sonnet', 'high')
    assert modelcheck.check(load_adapter(kproject.paths, 'claude'), pair).status == 'passed'
    prepared = workflow.tick(kproject, lease, defer_assignment=True)
    assert prepared and 'id' not in prepared and prepared['role'] == 'implementation'
    assert prepared['model'] == 'sonnet' and load_state(kproject.paths, lease.seat).task is None
    state = boardconfig.load(kproject.paths)
    state.settings['implementation'] = {'value': {'claude': {'model': 'opus', 'effort': 'high'}}, 'revision': 2}
    atomic_write_json(kproject.paths.kankan_dir / 'board-settings.json', state.to_dict())
    active = workflow.activate_task(kproject, lease, prepared)
    assert active['model'] == 'sonnet' and active['effort'] == 'high'
    assert 'sonnet/high' in ' '.join(fake_trello.card_comments(card['id']))


def test_failed_role_is_not_claimed(kproject, fake_trello):
    adapter = load_adapter(kproject.paths, 'claude')
    adapter.model_check = [__import__('sys').executable, '-c', 'raise SystemExit(5)', '{model}', '{effort}']
    assert modelcheck.check(adapter, ModelEffort('sonnet', 'high')).status == 'failed'
    lease = seats.acquire(kproject, 'claude')
    card = fake_trello.add_card(kproject.cfg.backlog, 'work')
    assert workflow.tick(kproject, lease, defer_assignment=True) is None
    assert kproject.refs.read(card_ref(card['id'])) is None
    codex = seats.acquire(kproject, 'codex')
    assert workflow.tick(kproject, codex)['card'] == card['id']


def test_activation_rechecks_stop_and_hold(kproject, fake_trello):
    lease = seats.acquire(kproject, 'claude')
    card = fake_trello.add_card(kproject.cfg.backlog, 'work')
    modelcheck.check(load_adapter(kproject.paths, 'claude'), ModelEffort('sonnet', 'high'))
    prepared = workflow.tick(kproject, lease, defer_assignment=True)
    update_state(kproject.paths, lease.seat, lambda state: setattr(state, 'stop_requested', True))
    assert workflow.activate_task(kproject, lease, prepared) is None
    assert load_state(kproject.paths, lease.seat).task is None
    update_state(kproject.paths, lease.seat, lambda state: setattr(state, 'stop_requested', False))
    fake_trello.user_label(card['id'], kproject.cfg.label_hold)
    assert workflow.activate_task(kproject, lease, prepared) is None
    assert kproject.trello.card(card['id']).list_id == kproject.cfg.backlog


def test_hold_wip_keeps_phase_role():
    c = SimpleNamespace(kind='hold_wip', ref=SimpleNamespace(meta={'phase': 'review'}))
    assert workflow.task_role(c) == 'review'
    c.ref.meta['phase'] = 'impl'
    assert workflow.task_role(c) == 'implementation'


import pytest


@pytest.mark.parametrize('event', ['stop', 'hold', 'move', 'takeover', 'recovery'])
def test_pending_prompt_rechecks_human_intent(kproject, fake_trello, event):
    from kankan.launcher import Launcher
    from kankan import messages
    from kankan.adapters import Classification
    from kankan.refstore import seat_ref
    from tests.test_ask import FakeTmux
    lease = seats.acquire(kproject, 'claude')
    card = fake_trello.add_card(kproject.cfg.backlog, 'pending prompt')
    task = workflow.tick(kproject, lease)
    tmux = FakeTmux()
    launcher = Launcher(kproject, load_adapter(kproject.paths, 'claude'), tmux, 'claude')
    launcher.lease, launcher.pane, launcher.session = lease, '%1', 'session'
    launcher.task, launcher.pending_writes = task, [messages.PROMPT]
    if event == 'stop':
        update_state(kproject.paths, lease.seat, lambda state: setattr(state, 'stop_requested', True))
    elif event == 'hold':
        fake_trello.user_label(card['id'], kproject.cfg.label_hold)
    elif event == 'move':
        fake_trello.user_move(card['id'], kproject.cfg.blocked)
    elif event == 'takeover':
        fake_trello.state['boards'][kproject.cfg.board_id]['desc'] = 'registry removed'
    else:
        kproject.refs.delete(seat_ref(lease.seat), lease.seat_oid)
    launcher.flush_assignment(Classification('ready'))
    assert tmux.pastes == []
    assert load_state(kproject.paths, lease.seat).task is None
