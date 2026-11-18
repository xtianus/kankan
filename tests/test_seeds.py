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
from tests.helpers import add_questions_seed


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
    seeds.preflight(paths, ['claude'], ('implementation', 'review'))
    atomic_write_json(target, {'claude': {'seed': {'review': {'model': 'absent', 'effort': 'low'}}}})
    with pytest.raises(KankanError):
        seeds.preflight(paths, ['claude'], ('implementation', 'review'))


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


def drop_seed(isolated_home, role):
    target = isolated_home / '.config/kankan/agents.json'
    data = json.loads(target.read_text())
    data['claude']['seed'].pop(role)
    target.write_text(json.dumps(data))


def test_missing_questions_seed_only_matters_for_the_questions_role(git_repo, isolated_home):
    from kankan import seeds
    paths = Paths.discover(git_repo)
    add_questions_seed()
    drop_seed(isolated_home, 'questions')
    seeds.preflight(paths, ['claude'], ('implementation', 'review'))
    with pytest.raises(KankanError) as one:
        seeds.preflight(paths, ['claude'], ('questions',))
    drop_seed(isolated_home, 'review')
    with pytest.raises(KankanError) as other:
        seeds.preflight(paths, ['claude'], ('review',))
    assert one.value.status == other.value.status == Status.NEEDS_ACTION
    assert one.value.message == other.value.message.replace('review', 'questions')
    assert 'questions' in one.value.message


def test_ensure_asks_for_the_questions_seed_only_when_roles_include_it(git_repo, isolated_home):
    from kankan import seeds
    paths = Paths.discover(git_repo)
    add_questions_seed()
    drop_seed(isolated_home, 'questions')
    seeds.ensure(paths, ['claude'], interactive=False, say=lambda text: None)
    with pytest.raises(KankanError) as error:
        seeds.ensure(paths, ['claude'], interactive=False, say=lambda text: None, roles=('implementation', 'review', 'questions'))
    assert error.value.status == Status.NEEDS_ACTION and 'questions' in error.value.message


def test_questions_seed_is_accepted_by_adapter_validation(isolated_home):
    adapter = load_adapter(None, 'claude', read_only=True)
    target = isolated_home / '.config/kankan/agents.json'
    data = json.loads(target.read_text())
    data['claude']['seed']['questions'] = {'model': 'sonnet', 'effort': 'high'}
    target.write_text(json.dumps(data))
    assert load_adapter(None, 'claude', read_only=True).seed['questions'] == {'model': 'sonnet', 'effort': 'high'}
