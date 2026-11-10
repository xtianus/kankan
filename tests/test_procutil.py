import os
import subprocess
import sys
import time

from kankan import procutil


def test_start_time_and_session():
    assert procutil.start_time(os.getpid()) > 0
    assert procutil.session_id(os.getpid()) == os.getsid(0)
    assert procutil.alive(os.getpid(), procutil.start_time(os.getpid()))
    assert not procutil.alive(os.getpid(), 1)
    assert len(procutil.boot_id()) >= 32


def test_terminate_session_kills_ignoring_child():
    script = ("import signal,subprocess,sys,time\n"
              "subprocess.Popen(['sleep','1000'])\n"
              "subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(1000)'])\n"
              "time.sleep(1000)\n")
    leader = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    time.sleep(0.5)
    members = procutil.pids_in_session(leader.pid)
    assert len(members) == 3
    survivors = procutil.terminate_session(leader.pid, exclude=set(), grace=0.5)
    leader.wait(5)
    assert survivors == []
    assert procutil.pids_in_session(leader.pid) == []


def test_ancestors_contains_parent():
    if os.getppid() > 1:
        assert os.getppid() in procutil.ancestors(os.getpid())
    assert 1 not in procutil.ancestors(os.getpid())


def test_subreaper_does_not_consume_owned_exit_status():
    code = '''
import subprocess,sys,time
from kankan import procutil
procutil.set_child_subreaper()
owned=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.1); raise SystemExit(7)'])
parent=subprocess.Popen([sys.executable,'-c',"import subprocess; subprocess.Popen(['sleep','.05'])"])
parent.wait()
time.sleep(.2)
procutil.reap_adopted({owned.pid})
assert owned.wait() == 7
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
