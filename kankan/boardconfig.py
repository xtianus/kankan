"""Pure board-settings evaluator and read-only effective-value lookup."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Union

from kankan import messages
from kankan.adapters import Adapter, ModelEffort, adapter_types, combinations, load_adapter
from kankan.trello import SettingsRead

SettingValue = Union[str, int, dict[str, ModelEffort]]


@dataclass
class SettingSpec:
    kind: str
    help: str = ''
    default: object = None
    allowed: list = field(default_factory=list)
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    adapters: dict[str, Adapter] = field(default_factory=dict)


@dataclass
class SettingsAction:
    id: str
    kind: str
    card_id: str
    revision: int
    text: str = ''


@dataclass(frozen=True)
class CheckRequest:
    role: str
    agent_type: str
    pair: ModelEffort
    revision: int


@dataclass
class SettingsState:
    settings: dict = field(default_factory=dict)
    cards: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    last_date: Optional[str] = None
    last_fast_read: Optional[float] = None
    construction: dict = field(default_factory=dict)
    list_available: bool = True

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('settings state must be an object')
        state = cls(**{k: v for k, v in value.items() if k in {f.name for f in dataclasses.fields(cls)}})
        if not all(isinstance(getattr(state, key), dict) for key in ('settings', 'cards', 'construction')) or not isinstance(state.pending, list):
            raise ValueError('invalid settings state')
        if not all(isinstance(entry, dict) for entry in state.settings.values()) or not all(isinstance(entry, dict) for entry in state.cards.values()):
            raise ValueError('invalid settings records')
        for key, record in state.settings.items():
            value = record.get('value')
            if key in ('implementation', 'review') and value is not None and not isinstance(value, dict):
                raise ValueError('role values must be model/effort maps')
            if isinstance(value, dict):
                for name, pair in value.items():
                    if not isinstance(name, str) or not isinstance(pair, dict) or set(pair) != {'model', 'effort'} or not all(isinstance(v, str) and v.strip() and not any(c in v for c in '\r\n\x00') for v in pair.values()):
                        raise ValueError('invalid saved model/effort')
            elif value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
                raise ValueError('invalid saved value')
            if 'revision' in record and (type(record['revision']) is not int or record['revision'] < 0):
                raise ValueError('invalid revision')
        for record in state.cards.values():
            if not isinstance(record.get('probes', {}), dict) or not isinstance(record.get('structural', []), list):
                raise ValueError('invalid card error causes')
            for probe in record.get('probes', {}).values():
                if not isinstance(probe, dict) or not isinstance(probe.get('pair'), dict) or set(probe['pair']) != {'model', 'effort'} or not all(isinstance(v, str) for v in probe['pair'].values()) or not isinstance(probe.get('version'), str) or not isinstance(probe.get('message'), str):
                    raise ValueError('invalid probe cause')
        for intent in state.construction.values():
            if not isinstance(intent, dict) or (intent.get('card_id') is not None and not isinstance(intent['card_id'], str)) or type(intent.get('creating', False)) is not bool:
                raise ValueError('invalid construction intent')
        for action in state.pending:
            if not isinstance(action, dict) or not all(isinstance(action.get(k), str) for k in ('id', 'card_id', 'kind')) or type(action.get('revision')) is not int:
                raise ValueError('invalid pending action')
        if state.last_fast_read is not None and (type(state.last_fast_read) not in (int, float) or not math.isfinite(state.last_fast_read)):
            raise ValueError('invalid poll reservation')
        if state.last_date is not None and activity(state.last_date) is None:
            raise ValueError('invalid response date')
        return state


@dataclass
class Evaluation:
    values: dict[str, SettingValue]
    state: SettingsState
    actions: list[SettingsAction]
    checks: list[CheckRequest]


def schema(adapters: dict[str, Adapter]) -> dict[str, SettingSpec]:
    return {role: SettingSpec('checklist', help=messages.SETTINGS_HELP, adapters=adapters)
            for role in ('implementation', 'review')}


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':')).encode()).hexdigest()[:24]


def activity(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def encode(value):
    if isinstance(value, ModelEffort):
        return value.to_dict()
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    return value


def display(value) -> str:
    if isinstance(value, dict):
        return ', '.join(f'{agent_type} {pair.model}/{pair.effort}' for agent_type, pair in value.items())
    return str(value)


def _values(state, specs):
    values = {}
    for key, spec in specs.items():
        stored = state.settings.get(key, {}).get('value')
        if spec.kind == 'description':
            values[key] = spec.default if stored is None else stored
        else:
            # A newly added adapter uses its seed without rewriting already recorded values.
            values[key] = {name: ModelEffort(**value) for name, value in (stored or {}).items()}
            for name, adapter in spec.adapters.items():
                if name not in values[key] and key in adapter.seed:
                    values[key][name] = ModelEffort(**adapter.seed[key])
    return values


def _inputs(card, kind=None):
    return [card.id, card.name, card.desc if kind != 'checklist' else '', [[c.name, c.pos, [[i.name, i.state, i.pos] for i in sorted(c.items, key=lambda i: i.pos)]]
                                          for c in sorted(card.checklists, key=lambda c: c.pos)]]


def _schema(spec):
    return [spec.kind, spec.default, spec.allowed, spec.minimum, spec.maximum,
            [[name, [pair.name for pair in combinations(adapter)]] for name, adapter in spec.adapters.items()]]


def item_name(value: str) -> str:
    return re.sub(r'\s+', '', value)


def _validate(card, spec):
    if spec.kind == 'description':
        text = card.desc.strip()
        if not text:
            return spec.default, [], True
        if spec.allowed:
            for allowed in spec.allowed:
                if str(allowed).casefold() == text.casefold():
                    return allowed, [], False
            return None, [f'`{text}` is not valid (allowed: {", ".join(map(str, spec.allowed))})'], False
        try:
            value = int(text)
            if spec.minimum is not None and value < spec.minimum or spec.maximum is not None and value > spec.maximum:
                raise ValueError
            return value, [], False
        except ValueError:
            return None, [f'`{text}` is not a valid bounded integer'], False
    problems, value = [], {}
    groups = {}
    for checklist in card.checklists:
        groups.setdefault(checklist.name, []).append(checklist)
    for name in groups:
        if name not in spec.adapters:
            problems.append(f'unknown checklist `{name}`')
    for name, adapter in spec.adapters.items():
        matches = groups.get(name, [])
        if len(matches) != 1:
            problems.append(f'{name}: {"missing" if not matches else "duplicate"} checklist')
            continue
        selected = [item for item in matches[0].items if item.state == 'complete']
        if len(selected) != 1:
            problems.append(f'{name}: {"no models ticked" if not selected else "two or more models ticked"}')
            continue
        allowed = {item_name(pair.name): pair for pair in combinations(adapter)}
        pair = allowed.get(item_name(selected[0].name))
        if pair is None:
            problems.append(f'{name}: unknown ticked combination `{selected[0].name}`')
        else:
            value[name] = pair
    return value, problems, False


def evaluate(cards: SettingsRead, state: SettingsState, schema: dict[str, SettingSpec]) -> Evaluation:
    """Evaluate only settled edits; no I/O or local clock is consulted."""
    state = copy.deepcopy(state)
    values, actions = _values(state, schema), []

    def result():
        checks = [CheckRequest(key, name, pair, state.settings.get(key, {}).get('revision', 0))
                  for key, value in values.items() if isinstance(value, dict) for name, pair in value.items() if name in schema[key].adapters]
        return Evaluation(values, state, actions, checks)

    date = cards.response_date
    state.list_available = cards.list_available
    if not cards.list_available or date is None or date.tzinfo is None:
        return result()
    previous = activity(state.last_date) if state.last_date else None
    if previous and date < previous:
        return result()
    state.last_date = date.isoformat()
    groups, unsettled = {}, set()
    for card in cards.cards:
        if card.closed:
            continue
        groups.setdefault(card.name, []).append(card)
        timestamp = activity(card.last_activity)
        if timestamp is None or (date-timestamp).total_seconds() < 10 or card.id in state.construction or card.name in state.construction:
            unsettled.add(card.name)
            old_key = state.cards.get(card.id, {}).get('key')
            if old_key:
                unsettled.add(old_key)

    def action(kind, card_id, record, text=''):
        revision = record.get('revision', 0)
        ident = fingerprint([card_id, record.get('fingerprint'), revision, kind, text])
        entry = SettingsAction(ident, kind, card_id, revision, text)
        if not any(p.get('id') == ident for p in state.pending):
            state.pending.append(dataclasses.asdict(entry))
            actions.append(entry)

    def card_error(card, key, problems, fp, revision, feedback=None):
        record = state.cards.setdefault(card.id, {})
        was_invalid = record.get('invalid', False)
        old_fp = record.get('fingerprint')
        record.update(key=key, structural=problems, fingerprint=fp, revision=revision)
        invalid = bool(problems or record.get('probes'))
        record['invalid'] = invalid
        # Supersede label actions while preserving ordered, deduplicated feedback.
        state.pending = [a for a in state.pending if a.get('card_id') != card.id or a.get('kind') not in ('add_error', 'remove_error')]
        if invalid and not record.get('observed_error'):
            record['owned_error'] = True
            action('add_error', card.id, record)
        elif not invalid and record.get('owned_error'):
            action('remove_error', card.id, record)
        if feedback and (old_fp != fp or was_invalid != invalid):
            action('comment', card.id, record, feedback)
        elif problems and old_fp != fp:
            action('comment', card.id, record, '; '.join(problems))

    for key, spec in schema.items():
        if key in unsettled:
            continue
        matches = groups.get(key, [])
        record = state.settings.setdefault(key, {})
        fp = fingerprint([[_inputs(card, spec.kind) for card in sorted(matches, key=lambda c: c.id)], _schema(spec)])
        if record.get('fingerprint') == fp:
            continue
        was_invalid = record.get('invalid', False)
        revision = record.get('revision', 0) + 1
        old = values[key]
        record.update(fingerprint=fp, revision=revision, card_id=matches[0].id if len(matches) == 1 else None)
        if not matches:
            record['invalid'] = False
            if spec.kind == 'description':
                values[key] = spec.default
                record['value'] = spec.default
                old_id = next((ident for ident, c in state.cards.items() if c.get('key') == key), None)
                if old != spec.default and old_id:
                    action('comment', old_id, record, messages.SETTINGS_RESET.format(default=spec.default))
            continue
        if len(matches) != 1:
            record['invalid'] = True
            for card in matches:
                card_error(card, key, [messages.SETTINGS_DUPLICATE.format(name=key)], fp, revision)
            continue
        card = matches[0]
        new, problems, reset = _validate(card, spec)
        record['invalid'] = bool(problems)
        if not problems:
            values[key] = new
            record['value'] = encode(new)
        feedback = None
        if problems:
            feedback = messages.SETTINGS_INVALID.format(problem='; '.join(problems), current=display(old))
        elif old != new:
            feedback = messages.SETTINGS_RESET.format(default=new) if reset else messages.SETTINGS_APPLIED.format(old=display(old), new=display(new))
        elif was_invalid:
            feedback = messages.SETTINGS_FIXED.format(current=display(new))
        previous_probes = state.cards.get(card.id, {}).get('probes', {})
        if not problems and isinstance(new, dict):
            state.cards.setdefault(card.id, {})['probes'] = {name: probe for name, probe in previous_probes.items() if name in new and probe.get('pair') == encode(new[name])}
        card_error(card, key, problems, fp, revision, feedback)
    for key, matches in groups.items():
        if key in schema or key in unsettled:
            continue
        for card in matches:
            record = state.cards.get(card.id, {})
            fp = fingerprint(_inputs(card))
            card_error(card, key, [messages.SETTINGS_UNKNOWN.format(name=key)], fp,
                       record.get('revision', 0) + int(record.get('fingerprint') != fp))
    present = {c.id for c in cards.cards if not c.closed}
    for ident, record in state.cards.items():
        if ident not in present and record.get('key') not in unsettled:
            record['structural'] = []
            record['probes'] = {}
            record['invalid'] = False
            if record.get('owned_error'):
                action('remove_error', ident, record)
    return result()


def load(paths) -> SettingsState:
    from kankan.config import read_json
    try:
        return SettingsState.from_dict(read_json(paths.kankan_dir / 'board-settings.json', {}))
    except (OSError, ValueError, TypeError, KeyError):
        return SettingsState()


def effective(paths, key: str) -> SettingValue:
    adapters = {name: load_adapter(paths, name, read_only=True) for name in adapter_types(paths)}
    return _values(load(paths), schema(adapters))[key]
