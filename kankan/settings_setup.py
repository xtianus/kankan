"""Recoverable settings-list/card construction, serialized separately from workflow transactions."""
from __future__ import annotations

from typing import Optional

from kankan import boardconfig, boardsettings, cardsections, messages
from kankan.adapters import adapter_types, combinations
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
        specs = boardconfig.schema(adapters, boardconfig.roles(project.paths))
        for key, spec in specs.items():
            name = boardconfig.title(key, spec)
            cards = [card for card in read.cards if card.name == name]
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
                    lambda: project.trello.create_card(project.cfg.settings_list, name, spec.help if spec.kind == 'checklist' else str(values)),
                    lambda: next((c for c in project.trello.settings_cards(project.cfg.settings_list).cards if c.name == name), None))
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
                    report = messages.SETTINGS_CREATED.format(role=name, values=boardconfig.display(values), source=source)
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
        report = ensure_instructions_card(project, adapter_types(project.paths), write=True)
        if report:
            reports.append(report)
            say(report)
        result = boardsettings.apply_read(project, project.trello.settings_cards(project.cfg.settings_list), adapters)
        boardsettings.drain_actions(project)
    return reports


def ensure_instructions_card(project, agent_types, *, write: bool) -> Optional[str]:
    """Create or update the `Instructions` card and keep it first in the list (card instructions T§6); with ``write``
    false, only report.

    Two or more such cards are left alone: the settings evaluation reports the duplicate."""
    list_id = project.cfg.settings_list
    if not list_id:
        return None
    read = project.trello.settings_cards(list_id)
    if not read.list_available:
        return None
    cards = [card for card in read.cards if card.name == cardsections.INSTRUCTIONS_TITLE and not card.closed]
    text = cardsections.help_text(agent_types)
    if len(cards) > 1:
        return None
    if not cards:
        if not write:
            return messages.INSTRUCTIONS_CARD_MISSING
        project.trello._write_once(
            lambda: project.trello.create_card(list_id, cardsections.INSTRUCTIONS_TITLE, text, pos="top"),
            lambda: next((c for c in project.trello.settings_cards(list_id).cards
                          if c.name == cardsections.INSTRUCTIONS_TITLE), None))
        return messages.INSTRUCTIONS_CARD_CREATED
    card = cards[0]
    outdated = cardsections.normalize_help(card.desc) != cardsections.normalize_help(text)
    misplaced = any(other.pos < card.pos for other in read.cards if other.id != card.id and not other.closed)
    if not write:
        return "; ".join([message for flag, message in ((outdated, messages.INSTRUCTIONS_CARD_OUTDATED),
                          (misplaced, messages.INSTRUCTIONS_CARD_NOT_FIRST)) if flag]) or None
    reports = []
    if outdated:
        project.trello.set_card_desc(card.id, text)
        reports.append(messages.INSTRUCTIONS_CARD_UPDATED.format(types=", ".join(sorted(set(agent_types)))))
    if misplaced:
        project.trello.move_card(card.id, list_id, "top")
        reports.append(messages.INSTRUCTIONS_CARD_MOVED)
    return "; ".join(reports) or None
