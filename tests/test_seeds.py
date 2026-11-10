import json
import os
import pty
import sys
import termios
import threading
from pathlib import Path

import pytest

from kankan.adapters import load_adapter
from kankan.config import Paths, atomic_write_json
from kankan.errors import KankanError, Status


def test_seed_keys_and_terminal_restore():
    from kankan import seeds
    assert [seeds.KEYS[i] for i in (0, 8, 9, 34, 35, 60)] == ['1', '9', 'a', 'z', 'A', 'Z']
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        with os.fdopen(os.dup(slave), 'r') as stream:
            writer = threading.Thread(target=lambda: (os.write(master, b'!1')), daemon=True)
            writer.start()
            assert seeds.read_key(stream) == '!'
            assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_seed_merge_preserves_other_content_and_order(git_repo, isolated_home):
    from kankan import seeds
    paths = Paths.discover(git_repo)
    target = isolated_home / '.config/kankan/agents.json'
    atomic_write_json(target, {'_note': 'keep', 'claude': {'models': {'z': {'efforts': ['high']}, 'a': {'efforts': ['low']}}}}, sort_keys=False)
    seeds.save(paths, 'claude', 'implementation', {'model': 'z', 'effort': 'high'})
    seeds.save(paths, 'codex', 'review', {'model': 'c', 'effort': 'low'})
    data = json.loads(target.read_text())
    assert list(data['claude']['models']) == ['z', 'a'] and data['_note'] == 'keep'
    assert data['codex']['seed']['review'] == {'model': 'c', 'effort': 'low'}
    assert target.stat().st_mode & 0o777 == 0o600
    seeds.save(paths, 'claude', 'implementation', {'model': 'a', 'effort': 'low'})
    assert json.loads(target.read_text())['claude']['seed']['implementation']['model'] == 'z'
    target.write_text('[]')
    with pytest.raises(KankanError):
        seeds.save(paths, 'claude', 'implementation', {'model': 'z', 'effort': 'high'})
    assert target.read_text() == '[]'


def test_noninteractive_missing_seeds_and_recorded_values(git_repo, isolated_home):
    from kankan import seeds
    paths = Paths.discover(git_repo)
    # This contract deliberately starts without the shared test fixture's seeds.
    target = isolated_home / '.config/kankan/agents.json'
    target.unlink(missing_ok=True)
    with pytest.raises(KankanError) as error:
        seeds.ensure(paths, ['claude'], interactive=False, say=lambda text: None)
    assert error.value.status == Status.NEEDS_ACTION
    assert not target.exists()
    pair = {'model': 'sonnet', 'effort': 'high'}
    atomic_write_json(paths.kankan_dir / 'board-settings.json', {'settings': {
        'implementation': {'value': {'claude': pair}}, 'review': {'value': {'claude': pair}}}})
    seeds.preflight(paths, ['claude'])
    atomic_write_json(target, {'claude': {'seed': {'review': {'model': 'absent', 'effort': 'low'}}}})
    with pytest.raises(KankanError):
        seeds.preflight(paths, ['claude'])


def test_parallel_projects_keep_both_seed_writes(isolated_home):
    import subprocess
    script = "from kankan.seeds import save; import sys; save(None,sys.argv[1],'implementation',{'model':'m','effort':'high'})"
    target = isolated_home / '.config/kankan/agents.json'
    target.write_text('{}')
    children = [subprocess.Popen([sys.executable, '-c', script, name]) for name in ('project-one', 'project-two')]
    assert all(child.wait(10) == 0 for child in children)
    data = json.loads(target.read_text())
    assert data['project-one']['seed']['implementation']['model'] == 'm'
    assert data['project-two']['seed']['implementation']['model'] == 'm'
