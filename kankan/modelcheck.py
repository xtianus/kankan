"""Isolated one-shot model probes, polled by launchers and shared across projects."""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kankan.adapters import Adapter, ModelEffort, expand
from kankan.config import atomic_write_json, file_lock

_RETIRED = []


@dataclass(frozen=True)
class CheckResult:
    status: str
    version: str
    message: str = ''
    retry_at: Optional[float] = None


def cache_path():
    return Path.home() / '.cache/kankan/model-checks.json'


def _read():
    try:
        data = json.loads(cache_path().read_text())
        if not isinstance(data, dict) or not isinstance(data.get('results', {}), dict) or not isinstance(data.get('versions', {}), dict):
            return {}
        observations = data.get('version_observed', {})
        if not isinstance(observations, dict) or any(type(value) is not int or value < 0 for value in observations.values()):
            return {}
        return data
    except (OSError, ValueError):
        return {}


def _version_key(adapter):
    return hashlib.sha256(json.dumps([adapter.type, adapter.version_command]).encode()).hexdigest()


def _version(adapter):
    versions = _read().get('versions', {})
    return versions.get(_version_key(adapter), getattr(adapter, '_check_version', ''))


def _key(adapter, pair, version):
    return hashlib.sha256(json.dumps([adapter.type, version, pair.model, pair.effort]).encode()).hexdigest()


def _store(*, version_entry=None, result_entry=None, observed_at=None):
    target = cache_path()
    with file_lock(target.with_suffix('.lock'), what='model check cache lock'):
        data = _read()
        if version_entry:
            key, value = version_entry
            observed_at = time.time_ns() if observed_at is None else observed_at
            if observed_at >= data.get('version_observed', {}).get(key, 0):
                data.setdefault('versions', {})[key] = value
                data.setdefault('version_observed', {})[key] = observed_at
        if result_entry:
            key, value = result_entry
            data.setdefault('results', {})[key] = dataclasses.asdict(value)
        atomic_write_json(target, data)
        return data


def cached(adapter: Adapter, pair: ModelEffort) -> Optional[CheckResult]:
    version = _version(adapter)
    if not version:
        return None
    data = _read().get('results', {}).get(_key(adapter, pair, version))
    try:
        result = CheckResult(**data) if data else None
        if result and result.status not in ('passed', 'failed', 'timeout'):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _sanitize(text, secrets=()):
    for value in sorted(secrets, key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "[redacted]")
    for key, value in os.environ.items():
        if value and len(value) >= 4 and any(word in key.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')):
            text = text.replace(value, '[redacted]')
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:access_token|refresh_token|api_key|password)["\s:=]+)[^\s,"}]+', r'\1[redacted]', text)
    return '\n'.join(text.splitlines()[-8:])[-2000:]


def _kill(process):
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _retire(process):
    if process is not None and process.poll() is None and process not in _RETIRED:
        _RETIRED.append(process)


def poll_retired() -> set[int]:
    """Keep Popen ownership after bounded cancellation until the child can actually be reaped."""
    _RETIRED[:] = [process for process in _RETIRED if process.poll() is None]
    return {process.pid for process in _RETIRED}


class PolledCommand:
    def __init__(self, argv, timeout, *, cwd=None, env=None, output_dir=None):
        self.output = tempfile.TemporaryFile(dir=output_dir)
        self.deadline = time.monotonic() + timeout
        self.kill_deadline = None
        self.timed_out = False
        try:
            self.process = subprocess.Popen(argv, stdout=self.output, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, cwd=cwd, env=env)
        except OSError:
            self.process = None

    def poll_exit(self):
        """No wait(), including after SIGKILL; hand off an unreapable child after a bounded grace."""
        code = self.process.poll() if self.process is not None else 1
        now = time.monotonic()
        if code is not None:
            return True, code
        if self.kill_deadline is None:
            if now < self.deadline:
                return False, None
            self.timed_out = True
            _kill(self.process)
            self.kill_deadline = now+2
            return False, None
        if now < self.kill_deadline:
            return False, None
        _retire(self.process)
        return True, None

    def cancel_child(self):
        _kill(self.process)
        _retire(self.process)
        if not self.output.closed:
            self.output.close()


class PendingVersion(PolledCommand):
    def __init__(self, adapter):
        self.adapter = adapter
        self.observed_at = time.time_ns()
        self.result = None
        self.done = False
        super().__init__(adapter.version_command, adapter.model_check_timeout_seconds)

    def poll(self):
        if self.done:
            return self.result
        finished, code = self.poll_exit()
        if not finished:
            return None
        self.output.seek(0)
        version = self.output.read(4096).decode('utf-8', 'replace').strip() if code == 0 and not self.timed_out else ''
        self.output.close()
        data = _store(version_entry=(_version_key(self.adapter), version), observed_at=self.observed_at)
        # A newer observation can finish first. Never make this late answer authoritative again.
        self.result = data.get('versions', {}).get(_version_key(self.adapter), '')
        self.adapter._check_version = self.result
        self.done = True
        return self.result

    def cancel(self):
        self.cancel_child()
        self.done, self.result = True, ''


def start_version(adapter: Adapter) -> PendingVersion:
    return PendingVersion(adapter)


class Completed:
    def __init__(self, result):
        self.result = result
        self.process = None

    def poll(self):
        return self.result

    def cancel(self):
        pass


class Busy:
    def __init__(self, adapter, pair):
        self.adapter, self.pair = adapter, pair
        self.deadline = time.monotonic() + adapter.model_check_timeout_seconds
        self.process = None

    def poll(self):
        result = cached(self.adapter, self.pair)
        if result and not (result.status == 'timeout' and result.retry_at and result.retry_at <= time.time()):
            return result
        if time.monotonic() >= self.deadline:
            return CheckResult('unchecked', _version(self.adapter), 'another model check did not finish')
        return None

    def cancel(self):
        pass


def _isolated_directory(prefix):
    """Ignore inherited TMPDIR; reject repository and instruction-file ancestors explicitly."""
    for root in (Path('/tmp'), Path('/var/tmp')):
        root = root.resolve()
        ancestors = (root, *root.parents)
        if any((parent/'.git').exists() or (parent/'AGENTS.md').exists() or (parent/'CLAUDE.md').exists()
               or ((parent/'HEAD').is_file() and (parent/'objects').is_dir() and (parent/'refs').is_dir())
               for parent in ancestors):
            continue
        try:
            return tempfile.TemporaryDirectory(prefix=prefix, dir=str(root))
        except OSError:
            continue
    raise OSError('no temporary directory outside repositories and instruction files is available')


class PendingCheck(PolledCommand):
    def __init__(self, adapter, pair, version, lock_fd):
        self.adapter, self.pair, self.version, self.lock_fd = adapter, pair, version, lock_fd
        self.result = None
        self.work = _isolated_directory('kankan-model-check-')
        self.home = _isolated_directory('kankan-model-home-')
        env = dict(os.environ)
        self.secrets = set()
        if adapter.model_check_home:
            variable = adapter.model_check_home['env']
            source = Path(env.get(variable, str(Path.home()/'.codex')))
            for name in adapter.model_check_home.get('files', []):
                original = source / name
                if original.is_file():
                    # A CLI failure can echo credential data; none of it belongs in the result cache.
                    try:
                        raw = original.read_text()
                        self.secrets.add(raw.strip())
                        def collect(value):
                            if isinstance(value, str):
                                self.secrets.add(value)
                            elif isinstance(value, dict):
                                for item in value.values():
                                    collect(item)
                            elif isinstance(value, list):
                                for item in value:
                                    collect(item)
                        collect(json.loads(raw))
                    except (OSError, ValueError):
                        pass
                    target = Path(self.home.name)/name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(original.resolve())
            env[variable] = self.home.name
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR'):
            env.pop(key, None)
        env.update(PWD=self.work.name, TMPDIR=self.work.name, TEMP=self.work.name, TMP=self.work.name)
        super().__init__(expand(adapter.model_check, model=pair.model, effort=pair.effort),
            adapter.model_check_timeout_seconds, cwd=self.work.name, env=env, output_dir=self.work.name)
        if self.process is None:
            # Not a rejection: the pair stays unverified and is checked again after the retry interval.
            self._finish(CheckResult('timeout', version, 'cannot run model check',
                                     retry_at=time.time()+adapter.model_check_retry_seconds))

    def _cleanup(self):
        self.output.close()
        self.work.cleanup()
        self.home.cleanup()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def _finish(self, result):
        self.result = result
        try:
            _store(result_entry=(_key(self.adapter, self.pair, self.version), result))
        finally:
            self._cleanup()
        return result

    def poll(self):
        if self.result:
            return self.result
        finished, code = self.poll_exit()
        if not finished:
            return None
        if self.timed_out:
            return self._finish(CheckResult('timeout', self.version, retry_at=time.time()+self.adapter.model_check_retry_seconds))
        self.output.seek(0, os.SEEK_END)
        self.output.seek(max(0, self.output.tell()-8192))
        message = _sanitize(self.output.read().decode('utf-8', 'replace'), self.secrets)
        return self._finish(CheckResult('passed' if code == 0 else 'failed', self.version, '' if code == 0 else message))

    def cancel(self):
        if self.result:
            return
        self.cancel_child()
        self._cleanup()
        self.result = CheckResult('unchecked', self.version, 'canceled')


def start(adapter: Adapter, pair: ModelEffort):
    version = _version(adapter)
    if not version:
        return Completed(CheckResult('unchecked', '', 'CLI version is unavailable'))
    result = cached(adapter, pair)
    if result and result.status in ('passed', 'failed'):
        return Completed(result)
    if result and result.retry_at and time.time() < result.retry_at:
        return None
    target = cache_path().parent / 'model-check-locks' / (_key(adapter, pair, version)+'.lock')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return Busy(adapter, pair)
    # The winner may have finished between our first cache read and acquiring the lock.
    result = cached(adapter, pair)
    if result and (result.status in ('passed', 'failed') or result.retry_at and time.time() < result.retry_at):
        os.close(fd)
        return Completed(result)
    try:
        return PendingCheck(adapter, pair, version, fd)
    except OSError as err:
        os.close(fd)
        return Completed(CheckResult('unchecked', version, str(err)))


def check(adapter: Adapter, pair: ModelEffort, *, read_only: bool = False) -> CheckResult:
    if read_only:
        return cached(adapter, pair) or CheckResult('unchecked', _version(adapter))
    version = start_version(adapter)
    while version.poll() is None:
        time.sleep(.01)
    if not version.result:
        return CheckResult('unchecked', '', 'CLI version is unavailable')
    pending = start(adapter, pair)
    if pending is None:
        return cached(adapter, pair) or CheckResult('unchecked', version.result)
    while True:
        result = pending.poll()
        if result is not None:
            return result
        time.sleep(.01)
