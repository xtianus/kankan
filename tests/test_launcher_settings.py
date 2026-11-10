import json

import pytest

from kankan import boardconfig
from kankan.config import atomic_write_json
from kankan.seatstate import load_state
from tests.helpers import bind_project
from tests.launcher_helpers import agent_events, launcher_state, proc_info, start_agent, wait_for

pytestmark = [pytest.mark.tmux, pytest.mark.slow]


def test_new_task_restarts_agent_and_keeps_seat(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=1, write_quiet_seconds=0)
    _, log = start_agent(tmux_server, project, scenario={'steps': [{'sleep': .2}]})
    assert wait_for(lambda: launcher_state(project, 'claude-1') == 'idle', timeout=25)
    old = proc_info(project, 'claude-1')
    state = boardconfig.load(project.paths)
    state.settings['implementation'] = {'value': {'claude': {'model': 'opus', 'effort': 'high'}}, 'revision': 1}
    atomic_write_json(project.paths.kankan_dir / 'board-settings.json', state.to_dict())
    card = fake_trello.add_card(project.reload_config().backlog, 'use new model')
    assert wait_for(lambda: len([e for e in agent_events(log) if e['event'] == 'start']) == 2, timeout=30)
    assert wait_for(lambda: load_state(project.paths, 'claude-1').task is not None, timeout=10)
    new = proc_info(project, 'claude-1')
    assert new['agent']['pid'] != old['agent']['pid']
    assert new['launcher'] == old['launcher']
    assert new['supervisor'] == old['supervisor']
    starts = [e for e in agent_events(log) if e['event'] == 'start']
    assert starts[-1]['argv'][-4:] == ['--model', 'opus', '--effort', 'high']
    task = load_state(project.paths, 'claude-1').task
    assert task['card'] == card['id'] and task['model'] == 'opus'
    assert load_state(project.paths, 'claude-1').running_selection == {'model': 'opus', 'effort': 'high'}
    assert load_state(project.paths, 'claude-1').planned_restart is None
    assert any(e['event'] == 'exit_command' for e in agent_events(log))


@pytest.mark.parametrize('ignore_exit', [True, False])
def test_restart_honors_hold_and_stop_before_prompt(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path, ignore_exit):
    from kankan import auditlog
    from kankan.seatstate import update_state
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=1, write_quiet_seconds=0)
    atomic_write_json(project.paths.kankan_dir / 'agents.json', {'claude': {'exit_timeout_seconds': .2}})
    _, log = start_agent(tmux_server, project, scenario={'ignore_exit': ignore_exit})
    assert wait_for(lambda: launcher_state(project, 'claude-1') == 'idle', timeout=25)
    state = boardconfig.load(project.paths)
    state.settings['implementation'] = {'value': {'claude': {'model': 'opus', 'effort': 'high'}}, 'revision': 1}
    atomic_write_json(project.paths.kankan_dir / 'board-settings.json', state.to_dict())
    card = fake_trello.add_card(project.reload_config().backlog, 'cancel before prompt')
    assert wait_for(lambda: load_state(project.paths, 'claude-1').planned_restart is not None, timeout=25)
    if ignore_exit:
        update_state(project.paths, 'claude-1', lambda current: setattr(current, 'stop_requested', True))
        assert wait_for(lambda: any(e['event'] == 'seat_released' for e in auditlog.read(project.paths)), timeout=15)
    else:
        fake_trello.user_label(card['id'], project.cfg.label_hold)
        assert wait_for(lambda: fake_trello.card_list(card['id']) == project.cfg.backlog, timeout=15)
    assert not any(e['event'] == 'input' and e.get('line') == 'Run the current kankan task (kankan skill).' for e in agent_events(log))


def test_start_deadline_is_abnormal(git_repo, fake_trello, monkeypatch, agent_path, tmux_server):
    from kankan import auditlog
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=1, write_quiet_seconds=0)
    atomic_write_json(project.paths.kankan_dir / 'agents.json', {'claude': {'start_timeout_seconds': .2}})
    _, log = start_agent(tmux_server, project, scenario={'no_ready': True})
    assert wait_for(lambda: any(e['event'] == 'launcher_error' and 'startup deadline' in e.get('error', '') for e in auditlog.read(project.paths)), timeout=25)
    assert not any(e['event'] == 'input' for e in agent_events(log))


def test_stop_is_handled_during_long_probe(git_repo, fake_trello, monkeypatch, agent_path, tmux_server):
    import sys
    import time
    from kankan import auditlog
    from kankan.seatstate import update_state
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=1, write_quiet_seconds=0)
    atomic_write_json(project.paths.kankan_dir/'agents.json', {'claude': {
        'model_check': [sys.executable, '-c', 'import time; time.sleep(30)', '{model}', '{effort}']}})
    start_agent(tmux_server, project)
    assert wait_for(lambda: launcher_state(project, 'claude-1') == 'starting', timeout=15)
    before = time.monotonic()
    update_state(project.paths, 'claude-1', lambda state: setattr(state, 'stop_requested', True))
    assert wait_for(lambda: any(e['event'] == 'seat_released' for e in auditlog.read(project.paths)), timeout=5)
    assert time.monotonic()-before < 5


def test_stop_during_restart_cleans_detached_descendant(git_repo, fake_trello, monkeypatch, agent_path, tmux_server):
    from kankan import procutil
    from kankan.seatstate import update_state
    from tests.launcher_helpers import pid_alive
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=1, write_quiet_seconds=0)
    fake_trello.add_card(project.cfg.backlog, 'first')
    shell = "echo x > x.txt && git add x.txt && git commit -qm first && kankan done --json"
    _, log = start_agent(tmux_server, project, scenario={'steps': [{'spawn': 'setsid', 'shell': shell, 'sleep': .1}]})
    child = None
    try:
        assert wait_for(lambda: any(e['event'] == 'spawned' for e in agent_events(log)), timeout=25)
        child = next(e['pid'] for e in agent_events(log) if e['event'] == 'spawned')
        started = procutil.start_time(child)
        assert wait_for(lambda: launcher_state(project, 'claude-1') == 'idle' and load_state(project.paths, 'claude-1').task is None, timeout=20)
        state = boardconfig.load(project.paths)
        state.settings['implementation'] = {'value': {'claude': {'model': 'opus', 'effort': 'high'}}, 'revision': 1}
        atomic_write_json(project.paths.kankan_dir/'board-settings.json', state.to_dict())
        fake_trello.add_card(project.reload_config().backlog, 'second')
        assert wait_for(lambda: load_state(project.paths, 'claude-1').planned_restart is not None, timeout=20)
        update_state(project.paths, 'claude-1', lambda current: setattr(current, 'stop_requested', True))
        assert wait_for(lambda: not pid_alive(child), timeout=8)
    finally:
        if child:
            procutil.terminate_pids([(child, started)], grace=.2)


def test_blocked_start_role_uses_other_verified_pair(git_repo, fake_trello, monkeypatch, agent_path, tmux_server):
    import sys
    from kankan import diag
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=1, write_quiet_seconds=0)
    atomic_write_json(project.paths.kankan_dir/'agents.json', {'claude': {
        'seed': {'implementation': {'model': 'sonnet', 'effort': 'high'}, 'review': {'model': 'opus', 'effort': 'high'}},
        'model_check': [sys.executable, '-c', 'import sys; sys.exit(9 if sys.argv[1]=="sonnet" else 0)', '{model}', '{effort}']}})
    card = fake_trello.add_card(project.cfg.backlog, 'blocked implementation')
    _, log = start_agent(tmux_server, project)
    assert wait_for(lambda: launcher_state(project, 'claude-1') == 'idle', timeout=25)
    start = next(e for e in agent_events(log) if e['event'] == 'start')
    assert start['argv'][-4:] == ['--model', 'opus', '--effort', 'high']
    assert load_state(project.paths, 'claude-1').task is None
    assert fake_trello.card_list(card['id']) == project.reload_config().backlog
    assert diag.status(project)['waiting'][0]['reason'] == 'model_check_failed'


def test_shared_daemon_survives_restart_and_serves_other_seat(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    import socket
    import sys
    from kankan import procutil, seats
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=1, write_quiet_seconds=0)
    marker = 'kankan-shared-test-service'
    atomic_write_json(project.paths.kankan_dir/'agents.json', {'claude': {'restart_keep': [marker]}})
    portfile = tmp_path/'port'
    service = tmp_path/'service.py'
    service.write_text('import socket,pathlib\ns=socket.socket();s.bind(("127.0.0.1",0));s.listen()\npathlib.Path('+repr(str(portfile))+').write_text(str(s.getsockname()[1]))\nwhile True:\n c,_=s.accept();c.sendall(b"OK");c.close()\n')
    fake_trello.add_card(project.cfg.backlog, 'first')
    shell = "echo x > x.txt && git add x.txt && git commit -qm first && kankan done --json"
    _, log = start_agent(tmux_server, project, scenario={'steps': [{'spawn': 'setsid', 'spawn_argv': [sys.executable, str(service), marker], 'shell': shell, 'sleep': .1}]})
    child, other = None, None
    try:
        assert wait_for(lambda: portfile.exists() and any(e['event'] == 'spawned' for e in agent_events(log)), timeout=25)
        child = next(e['pid'] for e in agent_events(log) if e['event'] == 'spawned')
        started = procutil.start_time(child)
        assert wait_for(lambda: launcher_state(project, 'claude-1') == 'idle' and load_state(project.paths, 'claude-1').task is None, timeout=20)
        oldpid = proc_info(project, 'claude-1')['agent']['pid']
        state = boardconfig.load(project.paths)
        state.settings['implementation'] = {'value': {'claude': {'model': 'opus', 'effort': 'high'}}, 'revision': 1}
        atomic_write_json(project.paths.kankan_dir/'board-settings.json', state.to_dict())
        fake_trello.add_card(project.reload_config().backlog, 'second')
        assert wait_for(lambda: proc_info(project, 'claude-1').get('agent', {}).get('pid') not in (None, oldpid), timeout=25)
        assert procutil.alive(child, started)
        other = seats.acquire(project, 'codex')
        code = 'import socket; s=socket.create_connection(("127.0.0.1",'+portfile.read_text()+')); assert s.recv(2)==b"OK"'
        result = __import__('subprocess').run([sys.executable, '-c', code], cwd=project.paths.seat_worktree(other.seat), capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
    finally:
        if child:
            procutil.terminate_pids([(child, started)], grace=.2)
        if other:
            seats.release(project, other)
