"""Recoverable settings-list/card construction, serialized separately from workflow transactions."""
from __future__ import annotations

from kankan import boardconfig, boardsettings, messages
from kankan.adapters import combinations
from kankan.config import file_lock, load_config, project_lock, save_config
from kankan.errors import KankanError


def ensure(project, adapters: dict, *, say) -> list[str]:
    reports = []
    with file_lock(project.paths.kankan_dir / 'settings-construction.lock', what='settings construction lock'):
        cfg = project.reload_config()
        snap = project.trello.snapshot(cfg.board_id)
        from kankan.config import check_registry
        check_registry(cfg, snap.desc)
        existing = snap.lists.get(cfg.settings_list)
        if existing and existing.closed:
            project.trello.unarchive_list(existing.id)
        elif existing is None:
            # An interrupted create is adopted by name before making another object.
            matches = [lst for lst in snap.lists.values() if lst.name == 'kankan' and not lst.closed]
            ident = matches[0].id if matches else project.trello.create_list(cfg.board_id, 'kankan').id
            with project_lock(project.paths):
                fresh = load_config(project.paths)
                fresh.settings_list = ident
                save_config(project.paths, fresh)
            project.reload_config()
        read = project.trello.settings_cards(project.cfg.settings_list)
        boardsettings.apply_read(project, read, adapters)
        boardsettings.drain_actions(project)
        specs = boardconfig.schema(adapters)
        for key, spec in specs.items():
            cards = [card for card in read.cards if card.name == key]
            if len(cards) > 1:
                continue  # Invalid user choices are not repaired by guessing.
            created = not cards
            with project_lock(project.paths):
                state = boardconfig.load(project.paths)
                creating = not cards or bool(state.construction.get(key, {}).get('creating'))
                state.construction[key] = {'card_id': cards[0].id if cards else None, 'creating': creating}
                boardsettings._save(project, state)
                values = boardconfig._values(state, specs)[key]
                recorded = state.settings.get(key, {}).get('value')
            try:
                card = cards[0] if cards else project.trello._write_once(
                    lambda: project.trello.create_card(project.cfg.settings_list, key, spec.help if spec.kind == 'checklist' else str(values)),
                    lambda: next((c for c in project.trello.settings_cards(project.cfg.settings_list).cards if c.name == key), None))
                with project_lock(project.paths):
                    state = boardconfig.load(project.paths)
                    state.construction[key] = {'card_id': card.id, 'creating': creating}
                    boardsettings._save(project, state)
                if spec.kind == 'description':
                    project.trello.comment_once(card.id, spec.help, 'help')
                for agent_type, adapter in (adapters.items() if spec.kind == 'checklist' else []):
                    checklists = [c for c in card.checklists if c.name == agent_type]
                    if len(checklists) > 1:
                        continue
                    new_checklist = not checklists
                    checklist = checklists[0] if checklists else project.trello.create_checklist(card.id, agent_type)
                    allowed = {boardconfig.item_name(pair.name): pair for pair in combinations(adapter)}
                    for item in checklist.items:
                        if boardconfig.item_name(item.name) not in allowed:
                            project.trello.delete_check_item(checklist.id, item.id)
                    for index, pair in enumerate(combinations(adapter), 1):
                        matches = [i for i in checklist.items if boardconfig.item_name(i.name) == boardconfig.item_name(pair.name)]
                        if not matches:
                            # Only a new checklist gets a seed/current tick. Existing invalid choices stay invalid.
                            project.trello.create_check_item(checklist.id, pair.name,
                                bool((new_checklist or creating) and values.get(agent_type) == pair), index * 1024)
                        else:
                            for item in matches:
                                if item.pos != index * 1024:
                                    project.trello.move_check_item(card.id, item.id, index * 1024)
                if created:
                    source = 'last valid board value' if recorded else 'seeds from ' + ', '.join(dict.fromkeys(
                        adapter.seed_sources.get(key, 'agents.json') for adapter in adapters.values()))
                    report = messages.SETTINGS_CREATED.format(role=key, values=boardconfig.display(values), source=source)
                    reports.append(report)
                    say(report)
            except KankanError:
                # Keep the construction intent for resumption by name under the next construction lock.
                raise
            else:
                with project_lock(project.paths):
                    state = boardconfig.load(project.paths)
                    state.construction.pop(key, None)
                    boardsettings._save(project, state)
        result = boardsettings.apply_read(project, project.trello.settings_cards(project.cfg.settings_list), adapters)
        boardsettings.drain_actions(project)
    return reports
