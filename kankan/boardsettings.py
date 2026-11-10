"""Durable evaluation, feedback journal and one fast settings poll per project."""
from __future__ import annotations

import dataclasses
import re
import time

from kankan import boardconfig, messages, notify
from kankan.adapters import adapter_types, load_adapter
from kankan.config import atomic_write_json, file_lock, project_lock
from kankan.errors import KankanError
from kankan.trello import SettingsRead, marker


def adapters_for(paths):
    return {name: load_adapter(paths, name, read_only=True) for name in adapter_types(paths)}


def _save(project, state):
    atomic_write_json(project.paths.kankan_dir / 'board-settings.json', state.to_dict())


def from_snapshot(project, snap) -> SettingsRead:
    ident = project.cfg.settings_list
    available = bool(ident and ident in snap.lists and not snap.lists[ident].closed)
    return SettingsRead([c for c in snap.cards.values() if c.list_id == ident], snap.response_date, available)


def apply_read(project, read: SettingsRead, adapters: dict, *, write: bool = True):
    def evaluate():
        state = boardconfig.load(project.paths)
        previous = boardconfig.activity(state.last_date) if state.last_date else None
        trusted = read.list_available and read.response_date is not None and (previous is None or read.response_date >= previous)
        if trusted:
            for card in read.cards:
                entry = state.cards.setdefault(card.id, {})
                entry['observed_error'] = project.cfg.label_error in card.label_ids
        result = boardconfig.evaluate(read, state, boardconfig.schema(adapters))
        if write:
            _save(project, result.state)
        return result
    if not write:
        return evaluate()
    with project_lock(project.paths):
        return evaluate()


def _prune(project, card_id):
    comments = project.trello.comments(card_id)
    me = notify.kankan_member_id(project)
    eligible = [c for c in comments if c.member_id == me and re.search(r'`kankan:(?!help`)[^`]+`\s*$', c.text)]
    for comment in eligible[:-10]:
        project.trello.delete_comment(card_id, comment.id)


def drain_actions(project) -> None:
    """Serialize writers, ack each completed intent; one failed action never blocks the others."""
    with file_lock(project.paths.kankan_dir / 'settings-feedback.lock', what='settings feedback lock'):
        with project_lock(project.paths):
            ids = [a['id'] for a in boardconfig.load(project.paths).pending]
        for ident in ids:
            try:
                with project_lock(project.paths):
                    state = boardconfig.load(project.paths)
                    action = next((a for a in state.pending if a['id'] == ident), None)
                    if action is None:
                        continue
                    card_id, kind = action['card_id'], action['kind']
                    record = state.cards.get(card_id, {})
                    card = project.trello.card(card_id)
                    if card:
                        if kind == 'add_error' and record.get('invalid'):
                            if project.cfg.label_error in card.label_ids and not (record.get('error_confirmed') or record.get('error_write_started')):
                                # The label appeared after evaluation, before our first write: it belongs to a person.
                                record['owned_error'] = False
                            else:
                                record['error_write_started'] = True
                                _save(project, state)
                                project.trello.add_label(card_id, project.cfg.label_error)
                                record['error_confirmed'] = True
                            record['observed_error'] = True
                        elif kind == 'remove_error' and not record.get('invalid') and record.get('owned_error'):
                            if record.get('error_confirmed') or record.get('error_write_started'):
                                project.trello.remove_label(card_id, project.cfg.label_error)
                                record['observed_error'] = False
                            record['owned_error'] = False
                            record['error_confirmed'] = False
                            record['error_write_started'] = False
                        elif kind == 'comment':
                            me = notify.kankan_member_id(project)
                            existing = next((c for c in project.trello.comments(card_id)
                                             if c.member_id == me and c.text.rstrip().endswith(marker(ident))), None)
                            if not existing:
                                mention = notify.mention(project, card_id) if record.get('invalid') else ''
                                project.trello.add_comment(card_id, action['text'] + ('\n' + mention if mention else ''), ident,
                                                           member_id=me)
                            prune_id = 'prune-' + ident
                            state.pending.append(dataclasses.asdict(boardconfig.SettingsAction(prune_id, 'prune', card_id, action['revision'])))
                        elif kind == 'prune':
                            _prune(project, card_id)
                    state.pending = [a for a in state.pending if a['id'] != ident]
                    _save(project, state)
            except KankanError as err:
                project.log('settings_feedback_failed', action=ident, error=err.message)
        # Prune actions produced above are independent and retried on the next drain on failure.
        with project_lock(project.paths):
            state = boardconfig.load(project.paths)
            pruning = [a for a in state.pending if a['kind'] == 'prune']
        for action in pruning:
            try:
                _prune(project, action['card_id'])
                with project_lock(project.paths):
                    state = boardconfig.load(project.paths)
                    state.pending = [a for a in state.pending if a['id'] != action['id']]
                    _save(project, state)
            except KankanError as err:
                project.log('settings_prune_failed', card=action['card_id'], error=err.message)


def poll(project, adapters: dict) -> list:
    if project.cfg.settings_list:
        # Lock-free pre-check: launchers call this every second, the reservation below is rarely due.
        last = boardconfig.load(project.paths).last_fast_read
        if last is not None and time.time() - last < project.cfg.settings_poll_seconds:
            return []
    cfg = project.reload_config()
    if not cfg.settings_list:
        return []
    now = time.time()
    with project_lock(project.paths):
        state = boardconfig.load(project.paths)
        if state.last_fast_read is not None and now - state.last_fast_read < cfg.settings_poll_seconds:
            return []
        state.last_fast_read = now
        _save(project, state)
    try:
        result = apply_read(project, project.trello.settings_cards(cfg.settings_list), adapters)
        drain_actions(project)
        return result.checks
    except KankanError as err:
        project.log('settings_read_failed', error=err.message)
        return []


def record_check(project, request, result) -> None:
    """A probe belongs to its original pair/revision; stale completions only populate the user cache."""
    with project_lock(project.paths):
        state = boardconfig.load(project.paths)
        role = state.settings.get(request.role, {})
        from kankan import modelcheck
        adapters = adapters_for(project.paths)
        if request.agent_type not in adapters:
            return
        effective = boardconfig._values(state, boardconfig.schema(adapters)).get(request.role, {}).get(request.agent_type)
        version = modelcheck._version(adapters[request.agent_type])
        if role.get('revision', 0) != request.revision or effective != request.pair or (version and result.version != version):
            return
        card_id = role.get('card_id')
        if not card_id:
            return
        record = state.cards.get(card_id, {})
        probes = record.setdefault('probes', {})
        before = probes.get(request.agent_type)
        if result.status == 'passed':
            probes.pop(request.agent_type, None)
        elif result.status == 'failed':
            probes[request.agent_type] = {'pair': request.pair.to_dict(), 'version': result.version, 'message': result.message}
        elif before and (before.get('version') != result.version or before.get('pair') != request.pair.to_dict()):
            probes.pop(request.agent_type, None)
        else:
            return  # An unchecked/timed-out pair blocks its role, but is not a rejected setting.
        record['invalid'] = bool(record.get('structural') or probes)
        revision = role.get('revision', 0)
        if result.status == 'failed' and before != probes.get(request.agent_type):
            text = messages.SETTINGS_PROBE_FAILED.format(type=request.agent_type, pair=request.pair.name, message=result.message)
            ident = boardconfig.fingerprint([card_id, request.agent_type, request.pair.to_dict(), result.version, 'failed'])
            state.pending.append(dataclasses.asdict(boardconfig.SettingsAction(ident, 'comment', card_id, revision, text)))
        if record['invalid'] and not record.get('observed_error'):
            record['owned_error'] = True
            kind = 'add_error'
        elif not record['invalid'] and record.get('owned_error'):
            kind = 'remove_error'
        else:
            kind = None
        if kind:
            state.pending = [a for a in state.pending if a['card_id'] != card_id or a['kind'] not in ('add_error', 'remove_error')]
            ident = boardconfig.fingerprint([card_id, revision, kind, probes])
            state.pending.append(dataclasses.asdict(boardconfig.SettingsAction(ident, kind, card_id, revision)))
        _save(project, state)


def role_block(project, agent_type, role, pair):
    from kankan import modelcheck
    adapter = load_adapter(project.paths, agent_type, read_only=True)
    result = modelcheck.cached(adapter, pair)
    if result and result.status == 'passed':
        return None
    return 'model_check_failed' if result and result.status == 'failed' else 'model_check_unverified'
