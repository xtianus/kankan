import json
import sys
from pathlib import Path

import pytest

from kankan import model_catalog
from kankan.adapters import load_adapter
from kankan.config import Paths, atomic_write_json


def test_catalog_filters_orders_and_reverses_levels():
    data = {'models': [
        {'slug': 'small', 'visibility': 'list', 'priority': 9, 'supported_reasoning_levels': ['low', {'effort': 'high'}]},
        {'slug': 'hidden', 'visibility': 'hide', 'priority': 0, 'supported_reasoning_levels': ['low']},
        {'slug': 'big', 'visibility': 'list', 'priority': 1, 'supported_reasoning_levels': ['medium', 'max'], 'extra': True},
    ]}
    assert model_catalog.parse_catalog(data) == {'big': {'efforts': ['max', 'medium']}, 'small': {'efforts': ['high', 'low']}}
    for bad in [{}, {'models': []}, {'models': [{'slug': 'x', 'visibility': 'list', 'priority': '1', 'supported_reasoning_levels': ['low']}]}]:
        with pytest.raises(ValueError):
            model_catalog.parse_catalog(bad)


def test_catalog_version_cache_and_read_only(git_repo, tmp_path, monkeypatch):
    paths = Paths.discover(git_repo)
    (Path.home() / '.config/kankan/agents.json').unlink(missing_ok=True)
    script = tmp_path / 'catalog.py'
    calls = tmp_path / 'calls'
    script.write_text("import json, pathlib\np=pathlib.Path(" + repr(str(calls)) + ")\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nprint(json.dumps({'models':[{'slug':'test','visibility':'list','priority':1,'supported_reasoning_levels':['low','high']}]}))\n")
    atomic_write_json(paths.kankan_dir / 'agents.json', {'codex': {
        'models_from': {'command': [sys.executable, str(script)], 'file': str(tmp_path/'absent')},
        'version_command': [sys.executable, '-c', "print('v1')"]}})
    assert 'test' not in load_adapter(paths, 'codex', read_only=True).models
    assert not calls.exists()
    assert model_catalog.refresh(paths, 'codex')['source'] == 'command'
    assert list(load_adapter(paths, 'codex', read_only=True).models) == ['test']
    model_catalog.refresh(paths, 'codex')
    assert calls.read_text() == 'x'
    model_catalog.refresh(paths, 'codex', force=True)
    assert calls.read_text() == 'xx'
    atomic_write_json(paths.kankan_dir / 'agents.json', {'codex': {'models': {'override': {'efforts': ['low']}}}})
    assert list(load_adapter(paths, 'codex').models) == ['override']
