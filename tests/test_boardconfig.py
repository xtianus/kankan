from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers import add_questions_seed

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
    c = card('Implementation Agents', '')
    c.checklists = [Checklist('check', c.id, 'claude', 1, [CheckItem('item', 'sonnet·high', 'complete', 1)])]
    if two:
        c.checklists[0].items.append(CheckItem('other', 'opus · high', 'complete', 2))
    return c


def test_role_atomic_validation_and_schema_change():
    from kankan.boardconfig import schema
    adapters = {'claude': load_adapter(None, 'claude', read_only=True)}
    specs = schema(adapters, ('implementation', 'review'))
    good = role_card(adapters)
    result = evaluate([good], schema=specs)
    assert result.values['implementation']['claude'] == ModelEffort('sonnet', 'high')
    bad = evaluate([role_card(adapters, two=True)], result.state, specs)
    assert bad.values['implementation']['claude'] == ModelEffort('sonnet', 'high')
    assert bad.state.settings['implementation']['invalid']
    assert 'two' in ' '.join(comments(bad))
    adapters['codex'] = load_adapter(None, 'codex')
    added = evaluate([good], result.state, schema(adapters, ('implementation', 'review')))
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


def test_roles_follow_the_registered_qa_seat(kproject):
    from kankan import boardconfig, seats
    assert boardconfig.roles(kproject.paths) == ('implementation', 'review')
    seats.ensure_created(kproject, 'claude-qa', 'claude')
    assert boardconfig.roles(kproject.paths) == ('implementation', 'review', 'questions')
    add_questions_seed()
    assert boardconfig.effective(kproject.paths, 'questions')['claude'] == ModelEffort('sonnet', 'high')


def test_questions_is_evaluated_without_a_qa_seat_but_not_active(kproject):
    from kankan import boardconfig
    assert boardconfig.effective(kproject.paths, 'questions') == {}
    assert 'questions' not in boardconfig.roles(kproject.paths)


def test_questions_card_is_no_unknown_setting_once_the_qa_seat_is_removed(kproject):
    from kankan import boardsettings
    from kankan.trello import SettingsRead
    adapters = {'claude': load_adapter(kproject.paths, 'claude', read_only=True)}
    questions = role_card(adapters)
    questions.name, questions.id = 'Q&A Agents', 'questions-card'
    result = boardsettings.apply_read(kproject, SettingsRead([questions], NOW, True), adapters)
    assert not result.state.cards['questions-card'].get('invalid')
    assert not [a for a in result.actions if a.kind == 'add_error']
    assert not [a.text for a in result.actions if 'unknown setting' in a.text]


def test_roles_logs_an_unreadable_configuration(kproject):
    from kankan import auditlog, boardconfig
    (kproject.paths.kankan_dir / 'config.json').write_text('{broken')
    assert boardconfig.roles(kproject.paths) == ('implementation', 'review')
    assert any(e['event'] == 'roles_config_unreadable' for e in auditlog.read(kproject.paths))


def test_invalid_questions_card_never_changes_the_work_roles():
    from kankan.boardconfig import schema
    adapters = {'claude': load_adapter(None, 'claude', read_only=True)}
    specs = schema(adapters, ('implementation', 'review', 'questions'))
    good = role_card(adapters)
    broken = role_card(adapters, two=True)
    broken.name, broken.id = 'Q&A Agents', 'questions-card'
    result = evaluate([good, broken], schema=specs)
    assert result.values['implementation']['claude'] == ModelEffort('sonnet', 'high')
    assert result.state.settings['questions']['invalid'] and not result.state.settings['implementation']['invalid']
    missing = evaluate([good], schema=specs)
    assert missing.values['implementation']['claude'] == ModelEffort('sonnet', 'high')


def test_setting_cards_are_recognized_by_title_not_by_key():
    from kankan.boardconfig import schema, title
    adapters = {'claude': load_adapter(None, 'claude', read_only=True)}
    specs = schema(adapters, ('implementation', 'review', 'questions'))
    assert [title(k, s) for k, s in specs.items()] == ['Implementation Agents', 'Review Agents', 'Q&A Agents']
    bare = role_card(adapters)
    bare.name, bare.id = 'implementation', 'bare'
    result = evaluate([role_card(adapters), bare], schema=specs)
    assert result.values['implementation']['claude'] == ModelEffort('sonnet', 'high')
    assert result.state.cards['bare']['structural'] == ['unknown setting `implementation`']
    assert not result.state.settings['implementation'].get('invalid')
    duplicate = evaluate([role_card(adapters), replace(role_card(adapters), id='other')], schema=specs)
    assert duplicate.state.cards['other']['structural'] == ['duplicate setting `Implementation Agents`']


def test_instructions_card_is_not_unknown():
    result = evaluate([card('Instructions', desc='help', ident='help')])
    assert not result.state.cards.get('help', {}).get('invalid')
    assert not [a for a in result.actions if a.kind == 'add_error']
    assert not [t for t in comments(result) if 'unknown setting' in t]


def test_instructions_duplicate_cleared():
    dup = evaluate([card('Instructions', ident='a'), card('Instructions', ident='b')])
    assert {a.card_id for a in dup.actions if a.kind == 'add_error'} == {'a', 'b'}
    assert all('duplicate' in t for t in comments(dup)) and len(comments(dup)) == 2
    for record in dup.state.cards.values():
        record['observed_error'] = True
    single = evaluate([card('Instructions', ident='a')], dup.state, date=NOW + timedelta(seconds=20))
    assert 'a' in {a.card_id for a in single.actions if a.kind == 'remove_error'}


def test_instructions_card_migrates_from_unknown():
    unknown = evaluate([card('Notes', ident='x')])
    assert unknown.state.cards['x']['invalid'] and unknown.state.cards['x']['owned_error']
    unknown.state.cards['x']['probes'] = {'claude': {'pair': 'p'}}
    renamed = evaluate([card('Instructions', ident='x')], unknown.state, date=NOW + timedelta(seconds=20))
    assert not renamed.state.cards['x']['invalid']
    assert [a.card_id for a in renamed.actions if a.kind == 'remove_error'] == ['x']


def test_instructions_renamed_away_and_back():
    first = evaluate([card('Instructions', ident='x')])
    away = evaluate([card('Notes', ident='x')], first.state, date=NOW + timedelta(seconds=20))
    assert away.state.cards['x']['invalid']
    away.state.cards['x']['observed_error'] = True
    back = evaluate([card('Instructions', ident='x')], away.state, date=NOW + timedelta(seconds=40))
    assert not back.state.cards['x']['invalid']
    assert 'x' in {a.card_id for a in back.actions if a.kind == 'remove_error'}
