import os
import signal
import subprocess
import sys
import threading

import pytest

from kankan import agents_cmd, build, seats
from kankan.errors import KankanError, Status
from kankan.seatstate import load_state
from tests.conftest import ROOT, git
from tests.helpers import remove_seat, bind_project
from tests.launcher_helpers import launcher_state, pid_alive, proc_info, wait_for

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=5)


@pytest.fixture
def env(monkeypatch, agent_path, tmux_server, tmp_path):
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    scenario = tmp_path / "scenario.json"
    scenario.write_text('{"steps": [{"sleep": 120}]}')
    monkeypatch.setenv("FAKE_AGENT_SCENARIO", str(scenario))
    return tmux_server


@pytest.fixture
def project(git_repo, fake_trello, monkeypatch, env):
    return bind_project(git_repo, fake_trello, monkeypatch, **FAST)


def alive(project, agent_type):
    return agents_cmd.live_counts(project).get(agent_type, 0)


def test_up_starts_only_missing(project, env):
    started = agents_cmd.up(project, {"claude": 1, "codex": 1})
    assert len(started) == 2
    assert agents_cmd.up(project, {"claude": 1, "codex": 1}) == []
    assert len(agents_cmd.up(project, {"claude": 2})) == 1
    assert alive(project, "claude") == 2 and alive(project, "codex") == 1
    # up() returns once the seats are taken; each launcher renames its session right after
    assert wait_for(lambda: {"ai-proj-claude-1", "ai-proj-claude-2", "ai-proj-codex-1"} <= set(env.sessions()),
                    timeout=15)


def test_kill9_supervisor_and_launcher_up_recovers(project, fake_trello, env):
    card = fake_trello.add_card(project.cfg.backlog, "c")
    agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    first = load_state(project.paths, "claude-1").task
    info = proc_info(project, "claude-1")
    os.kill(info["supervisor"]["pid"], signal.SIGKILL)
    try:
        os.kill(info["launcher"]["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass  # the launcher's PDEATHSIG may already have killed it with the supervisor
    assert wait_for(lambda: not pid_alive(info["launcher"]["pid"]), timeout=10)
    assert wait_for(lambda: not pid_alive(info["agent"]["pid"]), timeout=10)
    assert seats.liveness(project, "claude-1") == "dead"
    agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: (load_state(project.paths, "claude-1").task or {}).get("id") not in (None, first["id"]),
                    timeout=40)
    task = load_state(project.paths, "claude-1").task
    assert task["card"] == card["id"] and task["resumed"] is True


def test_autostart_unit_content(project, tmp_path, monkeypatch, isolated_home):
    calls = tmp_path / "systemctl.log"
    stub = tmp_path / "stubbin"
    stub.mkdir()
    for name, body in (("systemctl", f'echo "$@" >> {calls}'), ("loginctl", "echo Linger=no")):
        (stub / name).write_text(f"#!/bin/sh\n{body}\n")
        (stub / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    warnings = agents_cmd.autostart_enable(project, {"claude": 2, "codex": 1})
    unit = isolated_home / ".config" / "systemd" / "user" / "kankan-up@proj.service"
    text = unit.read_text()
    assert f"WorkingDirectory={project.paths.repo}" in text
    assert f'-c "{build.BOOTSTRAP}" up claude=2 codex=1\n' in text and "WantedBy=default.target" in text
    assert calls.read_text().splitlines() == ["--user daemon-reload", "--user enable kankan-up@proj.service"]
    assert warnings and "linger" in warnings[0]
    agents_cmd.autostart_disable(project)
    assert not unit.exists()


def test_two_projects_isolated(tmp_path, fake_trello, monkeypatch, env):
    repos = []
    for name in ("proj1", "proj2"):
        repo = tmp_path / "coding" / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        (repo / "f").write_text(name)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        repos.append(bind_project(repo, fake_trello, monkeypatch, **FAST))
    for project in repos:
        agents_cmd.up(project, {"claude": 1})
    assert wait_for(lambda: {"ai-proj1-claude-1", "ai-proj2-claude-1"} <= set(env.sessions()), timeout=15)
    lists = [p.reload_config().seats["claude-1"].in_progress for p in repos]
    assert lists[0] != lists[1]
    assert all(alive(p, "claude") == 1 for p in repos)


def test_concurrent_up_no_overstart(project, env):
    cmd = [sys.executable, "-m", "kankan", "up", "claude=2"]
    environment = dict(os.environ, PYTHONPATH=str(ROOT))
    procs = [subprocess.Popen(cmd, cwd=project.paths.repo, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, err
    assert alive(project, "claude") == 2
    assert wait_for(lambda: len([s for s in env.sessions() if s.startswith("ai-proj-claude")]) == 2, timeout=15)


def test_sanitized_name_collision_refused(tmp_path, fake_trello, monkeypatch, env):
    projects = []
    for name in ("a.b", "a_b"):
        repo = tmp_path / "coding" / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        (repo / "f").write_text(name)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        projects.append(bind_project(repo, fake_trello, monkeypatch, **FAST))
    agents_cmd.up(projects[0], {"claude": 1})
    with pytest.raises(KankanError) as exc:
        agents_cmd.up(projects[1], {"claude": 1})
    assert exc.value.status == Status.CONFIG_ERROR and "project_name" in exc.value.message


def test_parse_wanted():
    assert agents_cmd.parse_wanted(["claude=2", "codex=1"]) == ({"claude": 2, "codex": 1}, None)
    assert agents_cmd.parse_wanted(["claude=2", "qa=codex"]) == ({"claude": 2}, "codex")
    assert agents_cmd.parse_wanted(["qa=claude"]) == ({}, "claude")
    for bad in (["gpt=1"], ["qa=gpt"], ["qa=claude", "qa=codex"], []):
        with pytest.raises(KankanError):
            agents_cmd.parse_wanted(bad)


def test_up_counts_work_seats_only(project, fake_trello, env):
    from tests.helpers import add_qa_list
    add_qa_list(project, fake_trello)
    qa = seats.acquire_qa(project, "claude", None)
    try:
        assert len(agents_cmd.up(project, {"claude": 2})) == 2
        assert alive(project, "claude") == 2
        assert agents_cmd.up(project, {"claude": 2}) == []
    finally:
        seats.release(project, qa)


# ---------------------------------------------------------------------- the Q&A seat (questions F§2, F§8.13)

QA_FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=0.05, qa_poll_seconds=1)


@pytest.fixture
def qa_project(git_repo, fake_trello, monkeypatch, env, tmp_path):
    from tests.helpers import add_qa_list, add_questions_seed
    project = bind_project(git_repo, fake_trello, monkeypatch, **QA_FAST)
    add_questions_seed()
    project.qa_list = add_qa_list(project, fake_trello)
    project.scenario = tmp_path / "scenario.json"
    return project


def scenario(project, tmp_path, steps, name):
    """The scenario of the next agent started (every agent reads the shared scenario file when it starts)."""
    import json
    log = tmp_path / f"{name}.log"
    project.scenario.write_text(json.dumps({"log": str(log), "steps": steps}))
    return log


def qa_alive(project, seat):
    return seats.liveness(project, seat) == "alive"


def answer_step(text):
    return {"do": [["kankan", "answer", "--turn", "{turn}", "--file", "{file}"]], "text": text}


def comments_with(fake, card, needle):
    return [text for text in fake.card_comments(card["id"]) if needle in text]


def test_agent_qa_starts_the_qa_seat(qa_project, env):
    name = agents_cmd.agent(qa_project, "claude", qa=True)
    assert name.startswith("ai-proj-claude-starting-")
    assert wait_for(lambda: qa_alive(qa_project, "claude-qa"), timeout=30)
    assert qa_project.reload_config().qa_seat() == "claude-qa"
    assert wait_for(lambda: "ai-proj-claude-qa" in env.sessions(), timeout=15)
    assert alive(qa_project, "claude") == 0  # no work seat


def test_up_with_qa_is_idempotent(qa_project, env):
    started = agents_cmd.up(qa_project, {"claude": 2, "codex": 1}, qa="codex")
    assert len(started) == 4
    assert alive(qa_project, "claude") == 2 and alive(qa_project, "codex") == 1
    assert qa_alive(qa_project, "codex-qa")
    assert agents_cmd.up(qa_project, {"claude": 2, "codex": 1}, qa="codex") == []


def test_up_qa_of_another_type_stops_and_removes_the_pool_then_starts_the_new_router(qa_project, fake_trello, env,
                                                                                     tmp_path):
    """qapool F§7 type change: `up qa=codex` with a Claude pool running stops it at once without asking, removes its
    seats and starts codex-qa; every card is idle, and the next comment is answered by the new pool in a new
    conversation over the whole thread."""
    from kankan import qapool
    from kankan.refstore import card_ref, seat_ref
    project, fake = qa_project, fake_trello
    card = fake.add_card(project.qa_list, "how", desc="how does it work?")
    scenario(project, tmp_path, [answer_step("first answer")], "claude")
    agents_cmd.up(project, {}, qa="claude")
    assert wait_for(lambda: comments_with(fake, card, "first answer")
                    and launcher_state(project, "claude-qa") == "qa_waiting", timeout=40)
    conversation = project.refs.read(card_ref(card["id"])).meta["conversation"]
    scenario(project, tmp_path, [answer_step("continued by codex")], "codex")
    assert len(agents_cmd.up(project, {}, qa="codex")) == 1
    cfg = project.reload_config()
    assert cfg.qa_seat() == "codex-qa" and "claude-qa" not in cfg.seats
    assert project.refs.read(seat_ref("claude-qa")) is None and not qapool.stopping(project.paths)
    assert wait_for(lambda: qa_alive(project, "codex-qa"), timeout=30)
    assert project.refs.read(card_ref(card["id"])) is None  # idle: nothing new, no agent
    fake.user_comment(card["id"], "and what about X?")
    assert wait_for(lambda: comments_with(fake, card, "continued by codex"), timeout=40)
    value = project.refs.read(card_ref(card["id"]))
    assert value.meta["owner"] == "codex-qa" and value.meta["conversation"] != conversation
    assert agents_cmd.up(project, {}, qa="codex") == []


def test_autostart_unit_with_qa(project, tmp_path, monkeypatch, isolated_home):
    stub = tmp_path / "stubbin"
    stub.mkdir()
    for name, body in (("systemctl", "true"), ("loginctl", "echo Linger=yes")):
        (stub / name).write_text(f"#!/bin/sh\n{body}\n")
        (stub / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    agents_cmd.autostart_enable(project, {"claude": 2}, qa="codex")
    unit = isolated_home / ".config" / "systemd" / "user" / "kankan-up@proj.service"
    assert f'-c "{build.BOOTSTRAP}" up claude=2 qa=codex\n' in unit.read_text()


def _drop_seeds(agent_type, roles):
    import json
    from pathlib import Path
    target = Path.home() / ".config/kankan/agents.json"
    data = json.loads(target.read_text())
    for role in roles:
        data[agent_type]["seed"].pop(role, None)
    target.write_text(json.dumps(data))


def test_preflight_checks_only_the_questions_seed_for_the_qa_type(qa_project, env):
    _drop_seeds("codex", ("implementation", "review"))
    agents_cmd.agent(qa_project, "codex", qa=True)  # the work seeds of codex are not needed
    assert wait_for(lambda: qa_alive(qa_project, "codex-qa"), timeout=30)
    _drop_seeds("claude", ("questions",))
    with pytest.raises(KankanError) as exc:
        agents_cmd.up(qa_project, {}, qa="claude")
    # the type change comes before the seed check (qapool F§7: the codex pool is removed, claude-qa registered)
    assert exc.value.status == Status.NEEDS_ACTION and "claude questions seed" in exc.value.message
    assert qa_project.reload_config().qa_seat() == "claude-qa"
    assert agents_cmd.up(qa_project, {"claude": 1}) != []  # a missing questions seed never blocks work seats


def test_first_qa_start_without_a_questions_seed_registers_the_seat_so_doctor_asks(qa_project, env, monkeypatch):
    """questions F§7: `up qa=<type>` without a `questions` seed refuses with the usual manual step; the Q&A seat is
    registered, so `kankan doctor` asks for the seed and creates the `questions` card; then `up qa=<type>` starts."""
    import sys
    from kankan import diag, doctor, seeds
    project = qa_project
    _drop_seeds("claude", ("questions",))
    with pytest.raises(KankanError) as exc:
        agents_cmd.up(project, {}, qa="claude")
    assert exc.value.status == Status.NEEDS_ACTION
    assert "claude questions seed" in exc.value.message and "both roles" not in exc.value.message
    assert project.reload_config().qa_seat() == "claude-qa"
    status = diag.status(project)["qa"]
    assert status["seat"] == "claude-qa" and status["liveness"] == "free" and status["card"] is None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(seeds, "read_key", lambda: "1")
    doctor.run(project.paths.repo, fix=True, online=True)
    assert "questions" in json_seeds()["claude"]["seed"]
    names = [c.name for c in project.trello.settings_cards(project.reload_config().settings_list).cards]
    assert "Q&A Agents" in names
    assert len(agents_cmd.up(project, {}, qa="claude")) == 1
    assert wait_for(lambda: qa_alive(project, "claude-qa"), timeout=30)


def test_a_registered_qa_seat_that_never_started_is_removed(qa_project, env):
    from kankan import diag
    _drop_seeds("claude", ("questions",))
    with pytest.raises(KankanError):
        agents_cmd.agent(qa_project, "claude", qa=True)
    assert qa_project.reload_config().qa_seat() == "claude-qa"
    remove_seat(qa_project, "claude-qa")
    assert qa_project.reload_config().qa_seat() is None


def json_seeds():
    import json
    from pathlib import Path
    return json.loads((Path.home() / ".config/kankan/agents.json").read_text())


# ---------------------------------------------------------------------- shared board reads (board reader F§8.1)

def test_three_idle_agents_share_board_reads_with_the_cache_on(git_repo, fake_trello, monkeypatch, env):
    """Board reader functional test 1, with the cache on in every launcher: two work agents and the Q&A agent,
    idle, read the board about once per interval instead of once per agent; a card added to Backlog is still
    claimed within about twice ``poll_seconds``."""
    import time
    from kankan import auditlog
    from kankan.refstore import card_ref
    from tests.helpers import add_qa_list, add_questions_seed
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE")  # inherited by every launcher started below
    poll = 2
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=poll, qa_poll_seconds=poll,
                           write_quiet_seconds=0, idle_ready_minutes=0.05)
    add_questions_seed()
    add_qa_list(project, fake_trello)
    assert len(agents_cmd.up(project, {"claude": 2}, qa="claude")) == 3
    expected = {"claude-1", "claude-2", "claude-qa"}

    def polling():
        return {e.get("seat") for e in auditlog.read(project.paths) if e["event"] == "poll_done"}
    assert wait_for(lambda: expected <= polling(), timeout=60), polling()
    board_path = f"/1/boards/{project.cfg.board_id}"
    start = sum(1 for m, p in list(fake_trello.requests) if m == "GET" and p == board_path)
    polls_before = sum(1 for e in auditlog.read(project.paths) if e["event"] == "poll_done")
    window = 10
    time.sleep(window)
    reads = sum(1 for m, p in list(fake_trello.requests) if m == "GET" and p == board_path) - start
    polls = sum(1 for e in auditlog.read(project.paths) if e["event"] == "poll_done") - polls_before
    assert polls >= 2 * window // poll  # the launchers kept polling...
    assert reads <= window // poll + 3, (reads, polls)  # ...but the board was read about once per interval
    card = fake_trello.add_card(project.cfg.backlog, "new work")
    added = time.monotonic()
    assert wait_for(lambda: project.refs.read(card_ref(card["id"])) is not None, timeout=4 * poll + 5)
    assert time.monotonic() - added <= 2 * poll + 3  # within about twice the setting (board reader F§3)
