import dataclasses
import json
import os
import sys
import time
from pathlib import Path

from kankan.adapters import ModelEffort, load_adapter


def adapter(tmp_path, code, timeout=2):
    script = tmp_path / 'probe.py'
    script.write_text(code)
    base = load_adapter(None, 'codex', read_only=True)
    return dataclasses.replace(base, version_command=[sys.executable, '-c', "print('test-v1')"],
                               model_check=[sys.executable, str(script), '{model}', '{effort}'],
                               model_check_timeout_seconds=timeout, model_check_retry_seconds=1)


def poll(pending):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        result = pending.poll()
        if result is not None:
            return result
        time.sleep(.01)
    raise AssertionError('probe did not finish')


def test_isolation_cache_and_versioned_failure(tmp_path, isolated_home):
    from kankan import modelcheck
    auth = isolated_home / '.codex/auth.json'
    auth.parent.mkdir()
    auth.write_text('private test credential')
    (auth.parent / 'AGENTS.md').write_text('must not load')
    seen = tmp_path / 'seen.json'
    code = "import os,json,pathlib,sys\nhome=pathlib.Path(os.environ['CODEX_HOME'])\npathlib.Path("+repr(str(seen))+").write_text(json.dumps({'cwd':os.getcwd(),'files':[p.name for p in home.iterdir()],'argv':sys.argv[1:]}))\n"
    current = adapter(tmp_path, code)
    pair = ModelEffort('model; literal', 'high')
    assert modelcheck.check(current, pair, read_only=True).status == 'unchecked'
    result = modelcheck.check(current, pair)
    assert result.status == 'passed'
    observed = json.loads(seen.read_text())
    assert observed['files'] == ['auth.json'] and observed['argv'] == ['model; literal', 'high']
    assert not Path(observed['cwd']).exists()
    assert modelcheck.check(current, pair).status == 'passed'
    failing = adapter(tmp_path, "import sys\nprint('unsupported pair', file=sys.stderr)\nraise SystemExit(7)\n")
    failing.version_command = [sys.executable, '-c', "print('test-v2')"]
    rejected = modelcheck.check(failing, pair)
    assert rejected.status == 'failed' and 'unsupported pair' in rejected.message
    assert modelcheck.cached(failing, pair).status == 'failed'


def test_pending_is_nonblocking_and_timeout_is_unverified(tmp_path):
    from kankan import modelcheck
    current = adapter(tmp_path, 'import time\ntime.sleep(10)\n', timeout=.1)
    pair = ModelEffort('m', 'low')
    version = modelcheck.start_version(current)
    assert poll(version) == 'test-v1'
    before = time.monotonic()
    pending = modelcheck.start(current, pair)
    assert time.monotonic()-before < .1
    assert pending.poll() is None
    result = poll(pending)
    assert result.status == 'timeout' and result.retry_at > time.time()
    assert modelcheck.start(current, pair) is None
    assert modelcheck.check(current, pair, read_only=True).status == 'timeout'


def test_two_processes_share_one_probe(tmp_path, isolated_home):
    import subprocess
    target = isolated_home / '.config/kankan/agents.json'
    data = json.loads(target.read_text())
    counter = tmp_path / 'counter'
    script = tmp_path / 'probe.py'
    script.write_text("import pathlib,time\np=pathlib.Path("+repr(str(counter))+")\nwith p.open('a') as f: f.write('probe\\n')\ntime.sleep(.3)\n")
    data['claude'].update(version_command=[sys.executable, '-c', "print('concurrent-v1')"],
                          model_check=[sys.executable, str(script), '{model}', '{effort}'])
    target.write_text(json.dumps(data))
    code = "from kankan.adapters import load_adapter,ModelEffort; from kankan.modelcheck import check; print(check(load_adapter(None,'claude'),ModelEffort('sonnet','high')).status)"
    children = [subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        out, err = child.communicate(timeout=10)
        assert child.returncode == 0 and out.strip() == b'passed', err.decode()
    assert counter.read_text() == 'probe\n'


def test_probe_error_never_caches_linked_credentials(tmp_path, isolated_home):
    from kankan import modelcheck
    home = isolated_home / '.codex'
    home.mkdir()
    (home/'auth.json').write_text(json.dumps({'tokens': {'id_token': 'private-id-token-12345', 'access_token': 'private-access-token-12345'}}))
    current = adapter(tmp_path, "import os,pathlib,sys\nprint((pathlib.Path(os.environ['CODEX_HOME'])/'auth.json').read_text(),file=sys.stderr)\nraise SystemExit(1)\n")
    pair = ModelEffort('m', 'high')
    result = modelcheck.check(current, pair)
    assert result.status == 'failed'
    assert 'private-id-token-12345' not in result.message
    assert 'private-access-token-12345' not in modelcheck.cache_path().read_text()
