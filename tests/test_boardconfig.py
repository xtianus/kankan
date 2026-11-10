from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kankan.adapters import ModelEffort, load_adapter
from kankan.trello import Card

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def card(name='mode', desc='fast', age=10, ident='card'):
    return Card(ident, 'short', 'board', name, desc, '', 'settings', 1, [], [], False,
                (NOW - timedelta(seconds=age)).isoformat())


def description_schema():
    from kankan.boardconfig import SettingSpec
    return {'mode': SettingSpec(kind='description', default='safe', allowed=['safe', 'fast'], help='mode')}


def evaluate(cards, state=None, schema=None, date=NOW, available=True):
    from kankan.boardconfig import SettingsState, evaluate as run
    from kankan.trello import SettingsRead
    return run(SettingsRead(cards, date, available), state or SettingsState(), schema or description_schema())


def comments(result):
    return [a.text for a in result.actions if a.kind == 'comment']


def test_description_transitions_and_quiet_boundary():
    result = evaluate([card(age=9.999)])
    assert result.values['mode'] == 'safe' and not comments(result)
    result = evaluate([card(desc=' FAST ')], result.state)
    assert result.values['mode'] == 'fast'
    assert comments(result) == ['applied: `safe` → `fast`, from the next task']
    same = evaluate([card(desc='fast', age=11)], result.state)
    assert not comments(same)
    invalid = evaluate([card(desc='typo')], same.state)
    assert invalid.values['mode'] == 'fast' and invalid.state.settings['mode']['invalid']
    assert not comments(evaluate([card(desc='typo')], invalid.state))
    fixed = evaluate([card(desc='fast')], invalid.state)
    assert comments(fixed) == ['fixed; still using `fast`']
    reset = evaluate([], fixed.state)
    assert reset.values['mode'] == 'safe' and comments(reset) == ['applied: reset to default `safe`']
    assert not comments(evaluate([], reset.state))
    missing = evaluate([], fixed.state, available=False)
    assert missing.values['mode'] == 'fast'


@pytest.mark.parametrize('date,activity', [(None, None), (NOW, ''), (NOW, 'bad'), (NOW, (NOW+timedelta(seconds=1)).isoformat())])
def test_untrustworthy_times_never_mark_edit_handled(date, activity):
    c = card()
    if activity is not None:
        c.last_activity = activity
    result = evaluate([c], date=date)
    assert result.values['mode'] == 'safe' and not result.state.settings.get('mode', {}).get('fingerprint')


def role_card(adapters, *, two=False):
    from kankan.trello import Checklist, CheckItem
    c = card('implementation', '')
    c.checklists = [Checklist('check', c.id, 'claude', 1, [CheckItem('item', 'sonnet·high', 'complete', 1)])]
    if two:
        c.checklists[0].items.append(CheckItem('other', 'opus · high', 'complete', 2))
    return c


def test_role_atomic_validation_and_schema_change():
    from kankan.boardconfig import schema
    adapters = {'claude': load_adapter(None, 'claude', read_only=True)}
    specs = schema(adapters)
    good = role_card(adapters)
    result = evaluate([good], schema=specs)
    assert result.values['implementation']['claude'] == ModelEffort('sonnet', 'high')
    bad = evaluate([role_card(adapters, two=True)], result.state, specs)
    assert bad.values['implementation']['claude'] == ModelEffort('sonnet', 'high')
    assert bad.state.settings['implementation']['invalid']
    assert 'two' in ' '.join(comments(bad))
    adapters['codex'] = load_adapter(None, 'codex')
    added = evaluate([good], result.state, schema(adapters))
    assert added.values['implementation']['claude'] == ModelEffort('sonnet', 'high')
    assert added.values['implementation']['codex'] == ModelEffort('gpt-6.1-sol', 'high')
    assert added.state.settings['implementation']['invalid']
    assert evaluate([], result.state, specs).values['implementation']['claude'] == ModelEffort('sonnet', 'high')


def test_duplicates_recent_rename_and_old_read():
    initial = evaluate([card()])
    dup = evaluate([card(), card(ident='second')], initial.state)
    assert dup.values['mode'] == 'fast'
    assert len(comments(dup)) == 2 and all('duplicate' in m for m in comments(dup))
    fixed = evaluate([card()], dup.state)
    assert not fixed.state.settings['mode']['invalid']
    newer = evaluate([card(desc='safe')], fixed.state, date=NOW+timedelta(seconds=20))
    stale = evaluate([card(desc='fast')], newer.state)
    assert stale.values['mode'] == 'safe'
    # A rename still being edited must not reset its old setting.
    recent = evaluate([card('typo', age=1)], initial.state)
    assert recent.values['mode'] == 'fast' and not comments(recent)


def test_corrupt_nested_state_is_rebuilt(git_repo):
    from kankan import boardconfig
    from kankan.config import Paths, atomic_write_json
    paths = Paths.discover(git_repo)
    atomic_write_json(paths.kankan_dir/'board-settings.json', {'settings': {'implementation': {'value': {'claude': {'model': 'broken'}}}}})
    assert boardconfig.effective(paths, 'implementation')['claude'] == ModelEffort('sonnet', 'high')
