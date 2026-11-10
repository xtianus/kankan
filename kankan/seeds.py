"""Terminal seed selection with an atomic merge into the user's adapter overrides."""
from __future__ import annotations

import sys
import termios
import tty
from pathlib import Path

from kankan import messages
from kankan.adapters import ModelEffort, _read, combinations, load_adapter
from kankan.config import atomic_write_json, file_lock, read_json
from kankan.errors import KankanError, Status

KEYS = '123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
ROLES = ('implementation', 'review')


def read_key(stream=None) -> str:
    stream = stream or sys.stdin
    fd = stream.fileno()
    attributes = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        answer = stream.read(1)
        if answer == '\x03':
            raise KeyboardInterrupt
        return answer
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, attributes)


def validate(adapters: dict, *, require_all: bool) -> None:
    for agent_type, adapter in adapters.items():
        for role in ROLES:
            value = adapter.seed.get(role)
            if value is None:
                if require_all:
                    raise KankanError(Status.NEEDS_ACTION, messages.SEEDS_MISSING.format(type=agent_type, role=role))
            elif ModelEffort(**value) not in combinations(adapter):
                raise KankanError(Status.CONFIG_ERROR, messages.SEEDS_INVALID.format(type=agent_type, role=role))


def save(paths, agent_type: str, role: str, pair: dict, stale=None, target=None) -> Path:
    """Add a seed, keeping one another process saved meanwhile; ``stale`` (dropped from the catalog) is replaced.

    ``target`` is the file holding the stale seed (it would otherwise keep winning the override merge).
    """
    target = Path(target) if target else Path.home() / '.config/kankan/agents.json'
    with file_lock(target.with_name(target.name + '.lock'), what='seed overrides lock'):
        data = _read(target)  # Strict read; never overwrite corrupt user data.
        adapter = data.setdefault(agent_type, {})
        if not isinstance(adapter, dict) or not isinstance(adapter.get('seed', {}), dict):
            raise KankanError(Status.CONFIG_ERROR, f'invalid adapter overrides in {target}: expected objects')
        seeds = adapter.setdefault('seed', {})
        if role not in seeds or (stale is not None and seeds[role] == stale):
            seeds[role] = pair
        atomic_write_json(target, data, sort_keys=False)
    return target


def ensure(paths, types: list[str], *, interactive: bool, say) -> dict:
    adapters = {agent_type: load_adapter(paths, agent_type, read_only=True) for agent_type in types}
    validate(adapters, require_all=False)
    for agent_type, adapter in list(adapters.items()):
        for role in ROLES:
            if role in adapter.seed:
                continue
            if not interactive or not sys.stdin.isatty() or not sys.stdout.isatty():
                raise KankanError(Status.NEEDS_ACTION, messages.SEEDS_MISSING.format(type=agent_type, role=role))
            pairs = combinations(adapter)
            stale = adapter.stale_seeds.get(role)
            if stale:
                say(messages.SEEDS_STALE.format(type=agent_type, role=role, model=stale['model'], effort=stale['effort']))
            while True:
                say(messages.SEEDS_QUESTION.format(type=agent_type, role=role))
                for key, pair in zip(KEYS, pairs):
                    say(f'  {key}  {pair.name}')
                answer = read_key()
                if not answer:
                    raise KankanError(Status.NEEDS_ACTION, messages.SEEDS_MISSING.format(type=agent_type, role=role))
                if answer in KEYS[:len(pairs)]:
                    pair = pairs[KEYS.index(answer)]
                    break
            target = save(paths, agent_type, role, pair.to_dict(), stale=stale,
                          target=adapter.seed_sources.get(role) if stale else None)
            adapter = adapters[agent_type] = load_adapter(paths, agent_type, read_only=True)
            if role not in adapter.seed:
                raise KankanError(Status.NEEDS_ACTION, messages.SEEDS_MISSING.format(type=agent_type, role=role))
            selected = ModelEffort(**adapter.seed[role])
            say(messages.SEEDS_SAVED.format(type=agent_type, role=role, pair=selected.name,
                                           path=adapter.seed_sources.get(role, str(target))))
    return adapters


def preflight(paths, types) -> None:
    adapters = {agent_type: load_adapter(paths, agent_type, read_only=True) for agent_type in types}
    validate(adapters, require_all=False)
    try:
        state = read_json(paths.kankan_dir / 'board-settings.json', {}) if paths else {}
        recorded = state.get('settings', {})
    except (ValueError, TypeError, AttributeError):
        recorded = {}
    for agent_type, adapter in adapters.items():
        for role in ROLES:
            value = recorded.get(role, {}).get('value', {}).get(agent_type)
            if not value and role not in adapter.seed:
                raise KankanError(Status.NEEDS_ACTION, messages.SEEDS_MISSING.format(type=agent_type, role=role))
