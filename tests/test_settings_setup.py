from datetime import datetime, timedelta, timezone

from kankan.adapters import ModelEffort
from kankan.config import save_config
from tests.helpers import add_questions_seed



def role_cards(read):
    """The setting cards of a ``kankan`` list read, without the ``Instructions`` help card."""
    return [card for card in read.cards if card.name != 'Instructions']

def test_build_and_resume_settings_by_name(kproject, fake_trello):
    from kankan import settings_setup, boardsettings
    adapters = boardsettings.adapters_for(kproject.paths)
    reports = []
    settings_setup.ensure(kproject, adapters, say=reports.append)
    cfg = kproject.reload_config()
    assert cfg.settings_list and cfg.settings_list not in cfg.workflow_lists()
    read = kproject.trello.settings_cards(cfg.settings_list)
    assert [card.name for card in role_cards(read)] == ['Implementation Agents', 'Review Agents']
    assert [card.name for card in read.cards][0] == 'Instructions'  # the help card, first: card instructions F§6
    for card in role_cards(read):
        assert card.desc
        assert [c.name for c in card.checklists] == ['claude', 'codex']
        assert all(len([i for i in c.items if i.state == 'complete']) == 1 for c in card.checklists)
    assert 'sonnet/high' in ' '.join(reports)
    settings_setup.ensure(kproject, adapters, say=reports.append)
    assert len(kproject.trello.settings_cards(cfg.settings_list).cards) == 3
    kproject.trello.archive_list(cfg.settings_list)
    settings_setup.ensure(kproject, adapters, say=reports.append)
    assert kproject.trello.settings_cards(cfg.settings_list).list_available
    assert not __import__('kankan.boardconfig', fromlist=['load']).load(kproject.paths).construction


def test_sync_never_selects_replacement_for_removed_tick(kproject, fake_trello):
    from kankan import settings_setup, boardsettings, boardconfig
    adapters = boardsettings.adapters_for(kproject.paths)
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    for card in kproject.trello.settings_cards(kproject.cfg.settings_list).cards:
        fake_trello.state['cards'][card.id]['dateLastActivity'] = (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
    boardsettings.apply_read(kproject, kproject.trello.settings_cards(kproject.cfg.settings_list), adapters)
    # Remove the selected pair; the previously accepted value remains effective.
    adapters['claude'].models = {'opus': {'efforts': ['high']}}
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    impl = next(c for c in kproject.trello.settings_cards(kproject.cfg.settings_list).cards if c.name == 'Implementation Agents')
    claude = next(c for c in impl.checklists if c.name == 'claude')
    assert [i.name for i in claude.items] == ['opus · high']
    assert not any(i.state == 'complete' for i in claude.items)
    assert boardconfig.load(kproject.paths).settings['implementation']['value']['claude'] == {'model': 'sonnet', 'effort': 'high'}
    assert kproject.cfg.label_error in kproject.trello.card(impl.id).label_ids


def test_interrupted_items_resume_selected_seed(kproject, monkeypatch):
    import pytest
    from kankan import settings_setup, boardsettings
    adapters = boardsettings.adapters_for(kproject.paths)
    original = kproject.trello.create_check_item
    calls = [0]
    def interrupt(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 3:
            raise RuntimeError('interrupted process')
        return original(*args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(kproject.trello, 'create_check_item', interrupt)
        with pytest.raises(RuntimeError):
            settings_setup.ensure(kproject, adapters, say=lambda text: None)
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    impl = next(c for c in kproject.trello.settings_cards(kproject.reload_config().settings_list).cards if c.name == 'Implementation Agents')
    selected = [i.name for c in impl.checklists if c.name == 'claude' for i in c.items if i.state == 'complete']
    assert selected == ['sonnet · high']


def test_two_processes_build_one_set_of_cards(kproject):
    import subprocess
    import sys
    script = "from kankan.project import Project; from kankan import settings_setup,boardsettings; import sys; p=Project.open(sys.argv[1]); settings_setup.ensure(p,boardsettings.adapters_for(p.paths),say=lambda text:None)"
    children = [subprocess.Popen([sys.executable, '-c', script, str(kproject.paths.repo)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        stdout, stderr = child.communicate(timeout=25)
        assert child.returncode == 0, stderr.decode()
    read = kproject.trello.settings_cards(kproject.reload_config().settings_list)
    assert sorted(c.name for c in read.cards) == ['Implementation Agents', 'Instructions', 'Review Agents']
    assert all([c.name for c in card.checklists] == ['claude', 'codex'] for card in role_cards(read))


def test_description_setting_has_value_and_help_comment(kproject, monkeypatch):
    from kankan import settings_setup, boardsettings, boardconfig
    monkeypatch.setattr(boardconfig, 'schema', lambda adapters, roles: {'mode': boardconfig.SettingSpec('description', help='allowed safe/fast', default='safe', allowed=['safe', 'fast'])})
    settings_setup.ensure(kproject, boardsettings.adapters_for(kproject.paths), say=lambda text: None)
    read = kproject.trello.settings_cards(kproject.reload_config().settings_list)
    cards = role_cards(read)
    assert len(cards) == 1 and cards[0].desc == 'safe'
    assert not cards[0].checklists
    comments = kproject.trello.comments(cards[0].id)
    assert len(comments) == 1 and comments[0].text.endswith('`kankan:help`')


def test_questions_card_exists_only_with_a_qa_seat(kproject, fake_trello):
    from kankan import settings_setup, boardsettings, seats
    adapters = boardsettings.adapters_for(kproject.paths)
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    cfg = kproject.reload_config()
    assert [c.name for c in role_cards(kproject.trello.settings_cards(cfg.settings_list))] == ['Implementation Agents', 'Review Agents']
    seats.ensure_created(kproject, 'claude-qa', 'claude')
    add_questions_seed()
    adapters = boardsettings.adapters_for(kproject.paths)
    reports = []
    settings_setup.ensure(kproject, adapters, say=reports.append)
    cards = {c.name: c for c in kproject.trello.settings_cards(cfg.settings_list).cards}
    assert sorted(cards) == ['Implementation Agents', 'Instructions', 'Q&A Agents', 'Review Agents']
    assert [c.name for c in cards['Q&A Agents'].checklists] == ['claude', 'codex']
    assert all(len([i for i in c.items if i.state == 'complete']) == 1 for c in cards['Q&A Agents'].checklists)
    assert any('Q&A Agents' in report for report in reports)
