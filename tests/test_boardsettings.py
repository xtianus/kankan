from datetime import datetime, timedelta, timezone

from kankan.adapters import load_adapter
from kankan.config import save_config


def prepare(project, fake):
    settings = fake.add_list(project.cfg.board_id, 'kankan')
    project.cfg.settings_list = settings['id']
    save_config(project.paths, project.cfg)
    card = fake.add_card(settings['id'], 'unknown')
    card['dateLastActivity'] = (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
    return card, {'claude': load_adapter(project.paths, 'claude', read_only=True)}


def test_journal_replays_once_and_keeps_human_error(kproject, fake_trello):
    from kankan import boardsettings, boardconfig
    card, adapters = prepare(kproject, fake_trello)
    fake_trello.user_label(card['id'], kproject.cfg.label_error)
    read = boardsettings.from_snapshot(kproject, kproject.trello.snapshot(kproject.cfg.board_id))
    result = boardsettings.apply_read(kproject, read, adapters)
    assert result.state.pending
    boardsettings.drain_actions(kproject)
    boardsettings.apply_read(kproject, read, adapters)
    boardsettings.drain_actions(kproject)
    assert len(kproject.trello.comments(card['id'])) == 1
    fake_trello.user_move(card['id'], kproject.cfg.backlog)
    boardsettings.apply_read(kproject, boardsettings.from_snapshot(kproject, kproject.trello.snapshot(kproject.cfg.board_id)), adapters)
    boardsettings.drain_actions(kproject)
    assert kproject.cfg.label_error in kproject.trello.card(card['id']).label_ids
    assert not boardconfig.load(kproject.paths).pending


def test_own_label_clears_and_pruning_preserves_people(kproject, fake_trello):
    from kankan import boardsettings
    card, adapters = prepare(kproject, fake_trello)
    for index in range(12):
        kproject.trello.add_comment(card['id'], f'old {index}', f'old-{index}')
    kproject.trello.add_comment(card['id'], 'help', 'help')
    fake_trello.user_comment(card['id'], '`kankan:copied`')
    boardsettings.apply_read(kproject, boardsettings.from_snapshot(kproject, kproject.trello.snapshot(kproject.cfg.board_id)), adapters)
    boardsettings.drain_actions(kproject)
    comments = kproject.trello.comments(card['id'])
    assert len(comments) == 12  # ten feedback + help + human
    assert any(c.text == '`kankan:copied`' for c in comments)
    assert any('kankan:help' in c.text for c in comments)
    assert kproject.cfg.label_error in kproject.trello.card(card['id']).label_ids
    fake_trello.user_move(card['id'], kproject.cfg.backlog)
    boardsettings.apply_read(kproject, boardsettings.from_snapshot(kproject, kproject.trello.snapshot(kproject.cfg.board_id)), adapters)
    boardsettings.drain_actions(kproject)
    assert kproject.cfg.label_error not in kproject.trello.card(card['id']).label_ids


def test_shared_poll_and_no_fix_are_read_only(kproject, fake_trello, monkeypatch):
    from kankan import boardsettings
    card, adapters = prepare(kproject, fake_trello)
    statefile = kproject.paths.kankan_dir / 'board-settings.json'
    before = set(kproject.paths.kankan_dir.iterdir())
    boardsettings.apply_read(kproject, boardsettings.from_snapshot(kproject, kproject.trello.snapshot(kproject.cfg.board_id)), adapters, write=False)
    assert not statefile.exists() and set(kproject.paths.kankan_dir.iterdir()) == before
    now = [0.0]
    monkeypatch.setattr(boardsettings.time, 'time', lambda: now[0])
    boardsettings.poll(kproject, adapters)
    count = sum(path.endswith('/cards') and method == 'GET' for method, path in fake_trello.requests)
    boardsettings.poll(kproject, adapters)
    now[0] = 29
    boardsettings.poll(kproject, adapters)
    assert sum(path.endswith('/cards') and method == 'GET' for method, path in fake_trello.requests) == count
    now[0] = 30
    boardsettings.poll(kproject, adapters)
    assert sum(path.endswith('/cards') and method == 'GET' for method, path in fake_trello.requests) == count+1


def test_late_model_result_does_not_replace_new_selection(kproject, fake_trello):
    from kankan import boardconfig, boardsettings, settings_setup
    from kankan.modelcheck import CheckResult
    adapters = boardsettings.adapters_for(kproject.paths)
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    def settled_read():
        for raw in fake_trello.state['cards'].values():
            raw['dateLastActivity'] = (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
        return kproject.trello.settings_cards(kproject.cfg.settings_list)
    result = boardsettings.apply_read(kproject, settled_read(), adapters)
    request = next(c for c in result.checks if c.role == 'implementation' and c.agent_type == 'claude')
    raw = next(c for c in fake_trello.state['cards'].values() if c['name'] == 'implementation')
    checklist = next(c for c in fake_trello.state['checklists'].values() if c['idCard'] == raw['id'] and c['name'] == 'claude')
    for item in checklist['checkItems']:
        item['state'] = 'complete' if item['name'] == 'opus · high' else 'incomplete'
    boardsettings.apply_read(kproject, settled_read(), adapters)
    boardsettings.record_check(kproject, request, CheckResult('failed', 'v1', 'old failure'))
    state = boardconfig.load(kproject.paths)
    assert state.settings['implementation']['value']['claude']['model'] == 'opus'
    assert not state.cards[raw['id']].get('probes')


def test_old_cli_result_cannot_restore_rejection(kproject, fake_trello):
    from kankan import boardsettings, settings_setup, modelcheck
    from kankan.adapters import ModelEffort, load_adapter
    adapters = boardsettings.adapters_for(kproject.paths)
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    for raw in fake_trello.state['cards'].values():
        raw['dateLastActivity'] = (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
    result = boardsettings.apply_read(kproject, kproject.trello.settings_cards(kproject.cfg.settings_list), adapters)
    request = next(c for c in result.checks if c.role == 'implementation' and c.agent_type == 'claude')
    modelcheck.check(adapters['claude'], request.pair)
    # A separately polled version command has established a newer CLI version.
    modelcheck._store(version_entry=(modelcheck._version_key(adapters['claude']), 'new-version'))
    boardsettings.record_check(kproject, request, modelcheck.CheckResult('failed', 'fake-v1', 'obsolete failure'))
    from kankan import boardconfig
    state = boardconfig.load(kproject.paths)
    assert not state.cards[state.settings['implementation']['card_id']].get('probes')
