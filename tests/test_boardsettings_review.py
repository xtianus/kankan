"""Regressions for the independent whole-feature review."""
import dataclasses
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kankan import boardconfig, boardsettings, model_catalog, modelcheck, procutil, seats, settings_setup, workflow
from kankan.adapters import Classification, ModelEffort, load_adapter
from kankan.config import atomic_write_json
from kankan.launcher import Launcher
from kankan.refstore import card_ref, seat_ref
from kankan.seatstate import load_state, update_state
from tests.launcher_helpers import wait_for


def finish(pending):
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        result = pending.poll()
        if result is not None:
            return result
        time.sleep(.01)
    raise AssertionError('child never finished')


def test_shutdown_preserves_unavailable_seat(kproject, monkeypatch):
    child = subprocess.Popen(['sleep', '30'], start_new_session=True)
    try:
        lease = seats.acquire(kproject, 'claude')
        launcher = Launcher(kproject, load_adapter(kproject.paths, 'claude'), SimpleNamespace(), 'claude')
        launcher.lease = lease
        launcher.agent = SimpleNamespace(pid=child.pid, poll=lambda: 0)
        monkeypatch.setattr(procutil, 'terminate_agent_tree', lambda *args, **kwargs: [child.pid])
        launcher.shutdown()
        assert kproject.refs.read(seat_ref(lease.seat)) is not None
        assert seats.liveness(kproject, lease.seat) == 'unavailable'
    finally:
        child.terminate()
        child.wait(5)


def test_older_version_completion_cannot_replace_new_observation(tmp_path):
    source, started, release = [tmp_path/name for name in ('version', 'started', 'release')]
    script = tmp_path/'version.py'
    script.write_text('import pathlib,time\nsource=pathlib.Path('+repr(str(source))+')\nv=source.read_text()\nif v=="v1":\n pathlib.Path('+repr(str(started))+').touch()\n while not pathlib.Path('+repr(str(release))+').exists(): time.sleep(.01)\nprint(v)\n')
    source.write_text('v1')
    adapter = dataclasses.replace(load_adapter(None, 'claude'), version_command=[sys.executable, str(script)])
    old = modelcheck.start_version(adapter)
    try:
        assert wait_for(started.exists, timeout=5)
        source.write_text('v2')
        new_adapter = dataclasses.replace(adapter)
        assert finish(modelcheck.start_version(new_adapter)) == 'v2'
        release.touch()
        assert finish(old) == 'v2'
        assert modelcheck._version(dataclasses.replace(adapter)) == 'v2'
    finally:
        release.touch()
        old.cancel()


def test_older_catalog_completion_cannot_replace_new_observation(kproject, tmp_path):
    source, started, release = [tmp_path/name for name in ('version', 'started', 'release')]
    script = tmp_path/'catalog.py'
    script.write_text('import pathlib,time,json\nv=pathlib.Path('+repr(str(source))+').read_text()\nif v=="v1":\n pathlib.Path('+repr(str(started))+').touch()\n while not pathlib.Path('+repr(str(release))+').exists(): time.sleep(.01)\nprint(json.dumps({"models":[{"slug":"gpt-6.1-sol","visibility":"list","priority":1,"supported_reasoning_levels":["low","high"]}]}))\n')
    atomic_write_json(kproject.paths.kankan_dir/'agents.json', {'codex': {'models_from': {'command': [sys.executable, str(script)], 'file': str(tmp_path/'missing')}}})
    source.write_text('v1')
    old = model_catalog.PendingCatalog(kproject.paths, 'codex', 'v1')
    try:
        assert wait_for(started.exists, timeout=5)
        source.write_text('v2')
        new = model_catalog.PendingCatalog(kproject.paths, 'codex', 'v2')
        assert wait_for(new.poll, timeout=5)
        release.touch()
        assert wait_for(old.poll, timeout=5)
        assert model_catalog.cached('codex')['version'] == 'v2'
    finally:
        release.touch()
        old.cancel()


def settled(project, fake, offset=20):
    now = datetime.now(timezone.utc)
    for raw in fake.state['cards'].values():
        raw['dateLastActivity'] = (now-timedelta(seconds=30)).isoformat()
    return dataclasses.replace(project.trello.settings_cards(project.reload_config().settings_list), response_date=now+timedelta(seconds=offset))


def test_discarded_read_cannot_claim_human_error(kproject, fake_trello):
    adapters = boardsettings.adapters_for(kproject.paths)
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    old_read = settled(kproject, fake_trello, offset=10)
    impl = next(c for c in old_read.cards if c.name == 'implementation')
    fake_trello.user_label(impl.id, kproject.cfg.label_error)
    current = boardsettings.apply_read(kproject, settled(kproject, fake_trello), adapters)
    request = next(c for c in current.checks if c.role == 'implementation' and c.agent_type == 'claude')
    boardsettings.apply_read(kproject, old_read, adapters)
    boardsettings.record_check(kproject, request, modelcheck.CheckResult('failed', 'v1', 'rejected'))
    boardsettings.drain_actions(kproject)
    boardsettings.record_check(kproject, request, modelcheck.CheckResult('passed', 'v1'))
    boardsettings.drain_actions(kproject)
    assert kproject.cfg.label_error in kproject.trello.card(impl.id).label_ids


def test_human_error_added_before_dispatch_is_not_owned(kproject, fake_trello):
    adapters = boardsettings.adapters_for(kproject.paths)
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    first = settled(kproject, fake_trello)
    impl = next(c for c in first.cards if c.name == 'implementation')
    raw_check = next(c for c in fake_trello.state['checklists'].values() if c['idCard'] == impl.id and c['name'] == 'claude')
    for item in raw_check['checkItems']:
        item['state'] = 'incomplete'
    boardsettings.apply_read(kproject, settled(kproject, fake_trello, 30), adapters)
    fake_trello.user_label(impl.id, kproject.cfg.label_error)
    boardsettings.drain_actions(kproject)
    for item in raw_check['checkItems']:
        item['state'] = 'complete' if item['name'] == 'sonnet · high' else 'incomplete'
    boardsettings.apply_read(kproject, settled(kproject, fake_trello, 40), adapters)
    boardsettings.drain_actions(kproject)
    assert kproject.cfg.label_error in kproject.trello.card(impl.id).label_ids


@pytest.mark.parametrize('data', [
    {'settings': {'implementation': {'value': 'broken'}}},
    {'settings': {'implementation': {'value': 3}}},
    {'cards': {'x': {'probes': {'claude': 'broken'}}}},
    {'construction': {'implementation': 'broken'}},
])
def test_corrupt_setting_kinds_and_nested_records_rebuild(kproject, data):
    atomic_write_json(kproject.paths.kankan_dir/'board-settings.json', data)
    state = boardconfig.load(kproject.paths)
    assert not state.settings and not state.cards and not state.construction
    assert boardconfig.effective(kproject.paths, 'implementation')['claude'] == ModelEffort('sonnet', 'high')


def test_prompt_retains_activated_ref_identity(kproject, fake_trello):
    from tests.test_ask import FakeTmux
    from kankan import messages
    lease = seats.acquire(kproject, 'claude')
    card = fake_trello.add_card(kproject.cfg.backlog, 'ref changed')
    task = workflow.tick(kproject, lease)
    value = kproject.refs.read(card_ref(card['id']))
    changed = kproject.refs.write_blob(dict(value.meta, branch='different-branch'))
    assert kproject.refs.update(card_ref(card['id']), changed, value.oid)
    tmux = FakeTmux()
    launcher = Launcher(kproject, load_adapter(kproject.paths, 'claude'), tmux, 'claude')
    launcher.lease, launcher.pane, launcher.session, launcher.task = lease, '%1', 's', task
    launcher.pending_writes = [messages.PROMPT]
    launcher.flush_assignment(Classification('ready'))
    assert tmux.pastes == [] and load_state(kproject.paths, lease.seat).task is None


def test_probe_ignores_tmpdir_inside_checkout(git_repo, isolated_home, tmp_path):
    inside = git_repo/'temporary'
    inside.mkdir()
    (git_repo/'AGENTS.md').write_text('must not be an ancestor of the probe')
    seen = tmp_path/'cwd'
    script = tmp_path/'probe.py'
    script.write_text('import os,pathlib\npathlib.Path('+repr(str(seen))+').write_text(os.getcwd())\n')
    target = isolated_home/'.config/kankan/agents.json'
    data = json.loads(target.read_text())
    data['claude']['model_check'] = [sys.executable, str(script), '{model}', '{effort}']
    target.write_text(json.dumps(data))
    code = "from kankan.adapters import load_adapter,ModelEffort; from kankan.modelcheck import check; assert check(load_adapter(None,'claude'),ModelEffort('sonnet','high')).status=='passed'"
    result = subprocess.run([sys.executable, '-c', code], env=dict(os.environ, TMPDIR=str(inside)), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not Path(seen.read_text()).is_relative_to(git_repo)


@pytest.mark.parametrize('kind', ['version', 'probe', 'catalog'])
@pytest.mark.parametrize('operation', ['timeout', 'cancel'])
def test_timeout_and_cancel_never_wait_for_child(tmp_path, monkeypatch, kind, operation):
    class UnreapedChild:
        pid = 123456789
        def poll(self):
            return None
        def wait(self, *args, **kwargs):
            raise AssertionError('blocking wait on a child which has not exited')
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: UnreapedChild())
    monkeypatch.setattr(os, 'killpg', lambda *args: None)
    adapter = load_adapter(None, 'claude')
    pair = ModelEffort('sonnet', 'high')
    modelcheck._store(version_entry=(modelcheck._version_key(adapter), 'v1'))
    if kind == 'version':
        pending = modelcheck.start_version(adapter)
    elif kind == 'probe':
        pending = modelcheck.start(adapter, pair)
    else:
        from kankan.config import Paths
        paths = SimpleNamespace(kankan_dir=tmp_path/'project')
        pending = model_catalog.PendingCatalog(paths, 'codex', 'v1')
    try:
        if operation == 'cancel':
            pending.cancel()
        else:
            now = time.monotonic()
            pending.deadline = now-1
            pending.poll()
            monkeypatch.setattr(time, 'monotonic', lambda: now+10)
            pending.poll()
    finally:
        if hasattr(pending, 'output') and not pending.output.closed:
            pending.output.close()
        for field in ('work', 'home'):
            if hasattr(pending, field):
                getattr(pending, field).cleanup()
        if getattr(pending, 'lock_fd', None) is not None:
            os.close(pending.lock_fd)
            pending.lock_fd = None
        if hasattr(modelcheck, '_RETIRED'):
            modelcheck._RETIRED.clear()


def test_snapshot_normalizes_each_checklist_once(kproject):
    adapters = boardsettings.adapters_for(kproject.paths)
    settings_setup.ensure(kproject, adapters, say=lambda text: None)
    snap = kproject.trello.snapshot(kproject.cfg.board_id)
    impl = next(c for c in snap.cards.values() if c.name == 'implementation')
    assert [c.name for c in impl.checklists] == ['claude', 'codex']


def test_malformed_observation_metadata_recovers(tmp_path):
    adapter = load_adapter(None, 'claude')
    key = modelcheck._version_key(adapter)
    atomic_write_json(modelcheck.cache_path(), {'versions': {key: 'old'}, 'version_observed': {key: 'broken'}})
    assert finish(modelcheck.start_version(adapter)) == 'fake-v1'
    atomic_write_json(model_catalog.cache_path('codex'), {'version': 'old', 'source': 'command', 'models': {'gpt-6.1-sol': {'efforts': ['high']}}, 'observed_at': 'broken'})
    assert model_catalog.cached('codex') is None
