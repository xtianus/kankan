"""Ordered, versioned CLI model catalogs. Preparation is explicit and outside project locks."""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path

from kankan.config import atomic_write_json, file_lock


def cache_path(agent_type: str) -> Path:
    return Path.home() / '.cache' / 'kankan' / f'{agent_type}-models.json'


def parse_catalog(data: object) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get('models'), list):
        raise ValueError('missing models array')
    ordered = []
    for entry in data['models']:
        if not isinstance(entry, dict) or not all(key in entry for key in ('slug', 'visibility', 'priority', 'supported_reasoning_levels')):
            raise ValueError('malformed catalog entry')
        slug, priority, levels = entry['slug'], entry['priority'], entry['supported_reasoning_levels']
        if not isinstance(slug, str) or not slug.strip() or any(c in slug for c in '\r\n\x00'):
            raise ValueError('invalid model name')
        if isinstance(priority, bool) or not isinstance(priority, (int, float)) or not math.isfinite(priority):
            raise ValueError('invalid priority')
        if not isinstance(levels, list) or not levels or not isinstance(entry['visibility'], str):
            raise ValueError('invalid reasoning levels')
        efforts = [level.get('effort') if isinstance(level, dict) else level for level in levels]
        if not all(isinstance(e, str) and e.strip() and not any(c in e for c in '\r\n\x00') for e in efforts) or len(set(efforts)) != len(efforts):
            raise ValueError('invalid effort')
        if entry['visibility'] == 'list':
            ordered.append((priority, slug, list(reversed(efforts))))
    result = {slug: {'efforts': efforts} for _, slug, efforts in sorted(ordered, key=lambda row: row[0])}
    if not result or len(result) != len(ordered) or sum(len(v['efforts']) for v in result.values()) > 61:
        raise ValueError('empty, duplicate or oversized catalog')
    return result


def cached(agent_type: str):
    try:
        data = json.loads(cache_path(agent_type).read_text())
        # Validate cached normalized models through the same strict input parser.
        parse_catalog({'models': [{'slug': model, 'visibility': 'list', 'priority': index,
                                   'supported_reasoning_levels': list(reversed(value['efforts']))}
                                  for index, (model, value) in enumerate(data['models'].items())]})
        observed = data.get('observed_at', 0)
        return data if isinstance(data['version'], str) and data.get('source') in ('command', 'file', 'shipped') and type(observed) is int and observed >= 0 else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def refresh(paths, agent_type: str, *, force: bool = False) -> dict:
    observed_at = time.time_ns()
    from kankan.adapters import _read, _sources, load_adapter
    adapter = load_adapter(paths, agent_type, read_only=True)
    if not adapter.models_from or any('models' in _read(path).get(agent_type, {}) for path in _sources(paths)):
        return {'version': '', 'source': 'override', 'models': adapter.models}
    try:
        proc = subprocess.run(adapter.version_command, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=adapter.model_check_timeout_seconds)
        version = proc.stdout.strip() if proc.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        version = ''
    old = cached(agent_type)
    if old and version and old['version'] == version and not force:
        return old
    # No project or user lock is held while running discovery.
    models, source = None, 'shipped'
    try:
        proc = subprocess.run(adapter.models_from['command'], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=adapter.model_check_timeout_seconds)
        if proc.returncode == 0:
            models, source = parse_catalog(json.loads(proc.stdout)), 'command'
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    if models is None:
        try:
            location = adapter.models_from['file'].format(CODEX_HOME=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')), HOME=str(Path.home()))
            models, source = parse_catalog(json.loads(Path(location).read_text())), 'file'
        except (OSError, ValueError):
            from importlib import resources
            models = json.loads(resources.files('kankan').joinpath('adapters_default.json').read_text())[agent_type]['models']
    return _save(agent_type, {'version': version, 'source': source, 'models': models, 'observed_at': observed_at})


def _save(agent_type, entry):
    target = cache_path(agent_type)
    with file_lock(target.with_suffix('.lock'), what='model catalog lock'):
        old = cached(agent_type)
        if old and old.get('observed_at', 0) > entry['observed_at']:
            return old
        atomic_write_json(target, entry, sort_keys=False)
        return entry


class PendingCatalog:
    """Polled catalog discovery carrying the ordering of its CLI-version observation."""
    def __init__(self, paths, agent_type, version):
        from kankan import modelcheck
        from kankan.adapters import load_adapter
        self.adapter = load_adapter(paths, agent_type, read_only=True)
        self.agent_type, self.version = agent_type, version
        data = modelcheck._read()
        version_key = modelcheck._version_key(self.adapter)
        self.observed_at = data.get('version_observed', {}).get(version_key, time.time_ns())
        self.child = modelcheck.PolledCommand(self.adapter.models_from['command'], self.adapter.model_check_timeout_seconds)
        self.process, self.output, self.deadline = self.child.process, self.child.output, self.child.deadline
        self.done = False

    def poll(self):
        if self.done:
            return True
        self.child.deadline = self.deadline
        finished, code = self.child.poll_exit()
        if not finished:
            return False
        models, source = None, 'shipped'
        if code == 0 and not self.child.timed_out:
            try:
                self.output.seek(0)
                models, source = parse_catalog(json.load(self.output)), 'command'
            except (ValueError, OSError):
                pass
        if models is None:
            try:
                location = self.adapter.models_from['file'].format(CODEX_HOME=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')), HOME=str(Path.home()))
                models, source = parse_catalog(json.loads(Path(location).read_text())), 'file'
            except (OSError, ValueError):
                from importlib import resources
                models = json.loads(resources.files('kankan').joinpath('adapters_default.json').read_text())[self.agent_type]['models']
        _save(self.agent_type, {'version': self.version, 'source': source, 'models': models, 'observed_at': self.observed_at})
        self.output.close()
        self.done = True
        return True

    def cancel(self):
        self.child.cancel_child()
        self.done = True


def start_refresh(paths, agent_type, version):
    from kankan.adapters import _read, _sources, load_adapter
    adapter = load_adapter(paths, agent_type, read_only=True)
    if not adapter.models_from or any('models' in _read(p).get(agent_type, {}) for p in _sources(paths)):
        return None
    from kankan import modelcheck
    current_version = modelcheck._version(adapter)
    if current_version and current_version != version:
        return None
    old = cached(agent_type)
    if old and old['version'] == version:
        return None
    return PendingCatalog(paths, agent_type, version)
