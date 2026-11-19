"""Helpers of the live agents run (tests/live/e2e_support.py), exercised on the fake Trello server."""

import os
import subprocess

import pytest

from kankan import seats
from kankan.config import registry_block, write_registry_block
from kankan.project import Project
from kankan.trello import TrelloClient
from tests.conftest import git
from tests.fakes.fake_trello import KEY, TOKEN
from tests.live import e2e_support as es

MARKER = "kankan-test-board"


@pytest.fixture
def board(fake_trello, monkeypatch, tmp_path):
    monkeypatch.setenv("KANKAN_TRELLO_URL", fake_trello.url)
    b = fake_trello.add_board("kankan test", f"Test board {MARKER}.")
    creds = tmp_path / "test.properties"
    creds.write_text(f"kankan.trello.key={KEY}\nkankan.trello.token={TOKEN}\n")
    return fake_trello, b, creds


def client(fake):
    return TrelloClient(KEY, TOKEN, base_url=fake.url, timeout=0.5)


def lists_named(fake, board_id, name):
    return [l for l in fake.state["lists"].values() if l["idBoard"] == board_id and l["name"] == name]


def test_scenario_flags():
    assert [c.key for c in es.scenario(ask=False, hold=False, attach=False, qa=False, instructions=False)] == [
        "hello", "word", "name"]
    full = es.scenario()
    assert [c.key for c in full] == ["hello", "word", "name", "ask", "hold", "attach", "instr", "instr2", "qa"]
    assert {c.key: c.interactive for c in full} == {"hello": None, "word": None, "name": None, "ask": "ask",
                                                     "hold": "hold", "attach": "attach", "instr": None, "instr2": None,
                                                     "qa": "qa"}
    assert all("never push `main`" in c.desc for c in full)
    qa = full[-1]
    assert "check.sh" in qa.desc and "Commit" not in qa.desc


def test_new_run_id():
    run_id = es.new_run_id()
    assert run_id.startswith("ke-") and run_id != es.new_run_id()


def test_make_repos(tmp_path):
    clone, origin = es.make_repos(tmp_path)
    assert git(origin, "rev-parse", "main") == git(clone, "rev-parse", "main")
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "main"
    assert subprocess.run(["sh", "check.sh"], cwd=clone).returncode == 0
    (clone / "bad.txt").write_text("no final newline")
    assert subprocess.run(["sh", "check.sh"], cwd=clone, capture_output=True).returncode != 0


def test_bind_adopts_existing_lists(board, tmp_path, virtual_time):
    fake, b, creds = board
    existing = {name: fake.add_list(b["id"], name)["id"] for name in ("Backlog", "Done", "Blocked")}
    hold = fake.add_label(b["id"], "HOLD")["id"]
    clone, _ = es.make_repos(tmp_path)
    cfg = es.bind(clone, tmp_path, b["url"], creds, lambda s: None)
    assert (cfg.backlog, cfg.done, cfg.blocked) == (existing["Backlog"], existing["Done"], existing["Blocked"])
    assert cfg.label_hold == hold
    assert all(len(lists_named(fake, b["id"], n)) == 1 for n in existing)
    assert cfg.push_main and cfg.project_name == "kankan-e2e" and cfg.test_command == "sh check.sh"
    assert cfg.worktree_root == str(tmp_path / "worktrees") and cfg.poll_seconds == 10
    assert cfg.integration_branch == "kankan-agents"


def test_bind_takes_over_foreign_registry(board, tmp_path, virtual_time):
    fake, b, creds = board
    fake.state["boards"][b["id"]]["desc"] = write_registry_block(
        f"Test board {MARKER}.", {"registry-id": "abc", "host": "elsewhere", "path": "/x"})
    said = []
    clone, _ = es.make_repos(tmp_path)
    cfg = es.bind(clone, tmp_path, b["url"], creds, said.append)
    desc = fake.state["boards"][b["id"]]["desc"]
    assert MARKER in desc and registry_block(desc)["registry-id"] == cfg.registry_id
    assert any("elsewhere:/x" in s for s in said)


def test_blocking_cards(board):
    fake, b, _ = board
    desc_before = fake.state["boards"][b["id"]]["desc"]
    backlog, done = fake.add_list(b["id"], "Backlog"), fake.add_list(b["id"], "Done")
    review = fake.add_list(b["id"], "claude-1 · Review")
    foreign = fake.add_list(b["id"], "Ideas")
    questions = fake.add_list(b["id"], "Q&A")
    expected = {fake.add_card(l["id"], "x")["id"] for l in (backlog, done, review, questions)}
    fake.add_card(foreign["id"], "y")
    assert {c.id for c in es.blocking_cards(client(fake), b["id"])} == expected
    assert fake.state["boards"][b["id"]]["desc"] == desc_before


def test_seat_lists_adopted(board, tmp_path, virtual_time, monkeypatch):
    fake, b, creds = board
    names = [f"claude-1 · {s}" for s in ("In Progress", "Implemented", "Review")]
    ids = [fake.add_list(b["id"], n)["id"] for n in names]
    clone, _ = es.make_repos(tmp_path)
    es.bind(clone, tmp_path, b["url"], creds, lambda s: None)
    project = Project.open(clone)
    seat = seats.ensure_created(project, "claude-1", "claude")
    assert [seat.in_progress, seat.implemented, seat.review] == ids


def test_create_cards_order_and_names(board, tmp_path, virtual_time):
    fake, b, creds = board
    clone, _ = es.make_repos(tmp_path)
    cfg = es.bind(clone, tmp_path, b["url"], creds, lambda s: None)
    cards = es.create_cards(client(fake), cfg, "ke-1-abc", es.scenario())
    assert list(cards) == ["hello", "word", "name", "ask", "hold", "attach", "instr", "instr2", "qa"]
    assert cards["hello"].name == "ke-1-abc Say hello" and "greetings.txt" in cards["hello"].desc
    order = sorted((c for c in fake.state["cards"].values() if c["idList"] == cfg.backlog), key=lambda c: c["pos"])
    assert [c["id"] for c in order] == [c.id for key, c in cards.items() if key != "qa"]
    assert cfg.qa_list and fake.card_list(cards["qa"].id) == cfg.qa_list


def test_teardown_board(board):
    fake, b, _ = board
    lst = fake.add_list(b["id"], "Backlog")
    label = fake.add_label(b["id"], "HOLD")
    mine, other = fake.add_card(lst["id"], "ke-1 a"), fake.add_card(lst["id"], "ke-2 b")
    fake.state["boards"][b["id"]]["desc"] = write_registry_block(f"Keep me {MARKER}.", {"registry-id": "mine"})
    actions = es.teardown_board(client(fake), b["id"], [mine["id"]], "mine")
    assert mine["id"] not in fake.state["cards"] and other["id"] in fake.state["cards"]
    desc = fake.state["boards"][b["id"]]["desc"]
    assert registry_block(desc) is None and "Keep me" in desc and MARKER in desc
    assert lst["id"] in fake.state["lists"] and label["id"] in fake.state["labels"] and actions
    fake.state["boards"][b["id"]]["desc"] = write_registry_block(f"Keep me {MARKER}.", {"registry-id": "theirs"})
    es.teardown_board(client(fake), b["id"], [], "mine")
    assert registry_block(fake.state["boards"][b["id"]]["desc"])["registry-id"] == "theirs"


def test_cleanup_board(board):
    fake, b, _ = board
    lst = fake.add_list(b["id"], "Backlog")
    kt_list = fake.add_list(b["id"], "kt-1 A")
    ke, kt, keep = (fake.add_card(lst["id"], n) for n in ("ke-1 a", "kt-1 b", "real work"))
    fake.state["boards"][b["id"]]["desc"] = write_registry_block(f"{MARKER}", {"registry-id": "any"})
    es.cleanup_board(client(fake), b["id"])
    assert ke["id"] not in fake.state["cards"] and kt["id"] not in fake.state["cards"]
    assert keep["id"] in fake.state["cards"]
    assert fake.state["lists"][kt_list["id"]]["closed"]
    desc = fake.state["boards"][b["id"]]["desc"]
    assert registry_block(desc) is None and MARKER in desc


# ---------------------------------------------------------------- final verification (Task 3)

from kankan import auditlog, integrate, workflow  # noqa: E402
from tests.flow import agent_ctx, implement  # noqa: E402
from tests.helpers import bind_project  # noqa: E402


def _review(project, card_id, lease, monkeypatch):
    task = workflow.tick(project, lease)
    assert task["kind"] == "review" and task["card"] == card_id, task
    ctx = agent_ctx(project, lease, monkeypatch)
    integrate.sync(ctx)
    tip = git(ctx.worktree, "rev-parse", "HEAD")
    integrate.integrate(ctx, tip)
    integrate.done_review(ctx)


@pytest.fixture
def run(git_repo, fake_trello, monkeypatch, tmp_path):
    """Two cards through the real workflow: claude implements `word`, codex implements `name`, cross-reviewed."""
    from kankan import seats as seats_mod
    monkeypatch.setattr(seats_mod, "KILL_GRACE", 0.5)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(git_repo, "remote", "add", "origin", str(origin))
    git(git_repo, "push", "-q", "origin", "main")
    git(git_repo, "branch", "kankan-agents")
    project = bind_project(git_repo, fake_trello, monkeypatch, integration_branch="kankan-agents", push_main=True)
    claude, codex = seats.acquire(project, "claude"), seats.acquire(project, "codex")
    project.reload_config()
    word = implement(project, fake_trello, claude, "word", files={"words.txt": "apple\n"})
    _review(project, word["id"], codex, monkeypatch)
    name = implement(project, fake_trello, codex, "name", files={"names.txt": "codex\n"})
    _review(project, name["id"], claude, monkeypatch)
    c = client(fake_trello)
    cards = {"word": c.card(word["id"]), "name": c.card(name["id"])}
    refs = {"main": git(git_repo, "rev-parse", "main")}
    return project, fake_trello, c, cards, origin, refs, (claude, codex)


def test_verify_run_passes_on_consistent_run(run):
    project, fake, c, cards, origin, refs, _ = run
    assert es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs) == []


def test_verify_run_reports_missing_merge(run):
    project, fake, c, cards, origin, refs, _ = run
    ghost = fake.add_card(project.cfg.done, "ke-1 ghost")
    cards = {**cards, "word2": c.card(ghost["id"])}
    problems = es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs)
    assert any("ghost" in p and "merge" in p for p in problems), problems


def test_verify_run_reports_same_type_review(run):
    project, fake, c, cards, origin, refs, _ = run
    log = project.paths.kankan_dir / "log.jsonl"
    lines = log.read_text().splitlines()
    word_id = cards["word"].id
    rewritten = [l.replace('"seat": "codex-1"', '"seat": "claude-9"') if '"task_assigned"' in l and word_id in l
                 and '"review"' in l else l for l in lines]
    log.write_text("\n".join(rewritten) + "\n")
    problems = es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs)
    assert any("claude-1" in p and "claude-9" in p for p in problems), problems


def test_verify_run_reports_origin_behind(run):
    project, fake, c, cards, origin, refs, _ = run
    git(origin, "update-ref", "refs/heads/kankan-agents", refs["main"])
    problems = es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs)
    assert any("origin" in p for p in problems), problems


def test_verify_run_reports_moved_user_branch(run):
    project, fake, c, cards, origin, refs, _ = run
    from tests.flow import commit
    commit(project.paths.repo, "mine.txt")
    problems = es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs)
    assert any("main moved" in p for p in problems), problems


def test_verify_run_ask_without_answer_fails(run):
    project, fake, c, cards, origin, refs, _ = run
    problems = es.verify_run(project, c, cards, origin, ask_ran=True, hold_ran=False, user_refs=refs)
    assert any("answer" in p for p in problems), problems


def test_user_answers_filters_member_and_marker(fake_trello):
    board_ = fake_trello.add_board("b")
    lst = fake_trello.add_list(board_["id"], "Backlog")
    card = fake_trello.add_card(lst["id"], "c")
    other = fake_trello.add_member("someone")
    fake_trello.user_comment(card["id"], "kankan: question\n\n`kankan:op1`", fake_trello.me["id"])
    fake_trello.user_comment(card["id"], "red", fake_trello.me["id"])  # kankan's own account: never an answer
    fake_trello.user_comment(card["id"], "blue", other["id"])
    fake_trello.user_comment(card["id"], "green")
    assert es.user_answers(client(fake_trello), card["id"], fake_trello.me["id"]) == ["blue", "green"]


def _ev(event, **fields):
    return {"event": event, **fields}


def test_hold_problems():
    good = [_ev("task_assigned", seat="claude-1", kind="impl", card="H", branch="26kankan-Hh"),
            _ev("suspend_requested", seat="claude-1", reason="hold"),
            _ev("transition", card="H", phase="parked"),
            _ev("task_assigned", seat="codex-1", kind="impl", card="H", branch="26kankan-Hh")]
    assert es.hold_problems(good, "H") == []
    assert es.hold_path(good, "H").startswith("the launcher")
    # The agent saw HOLD in `kankan task` and yielded before the launcher's request (live run ke-1794762085).
    agent_first = [good[0], good[2], good[1], good[3]]
    assert es.hold_problems(agent_first, "H") == []
    assert es.hold_path(agent_first, "H").startswith("the agent")
    not_parked = [e for e in good if e["event"] != "transition"]
    assert "not parked" in es.hold_problems(not_parked, "H")[0]
    assert es.hold_path(not_parked, "H") is None
    never_resumed = good[:3]
    assert "not implemented again" in es.hold_problems(never_resumed, "H")[0]
    other_branch = good[:3] + [_ev("task_assigned", seat="codex-1", kind="impl", card="H", branch="26kankan-Xx")]
    assert len(es.hold_problems(other_branch, "H")) == 1


def test_settled(run, monkeypatch):
    project, fake, c, cards, origin, refs, (claude, codex) = run
    assert es.settled(project, c, cards, origin)
    git(origin, "update-ref", "refs/heads/kankan-agents", refs["main"])
    assert not es.settled(project, c, cards, origin)
    git(origin, "update-ref", "refs/heads/kankan-agents", git(project.paths.repo, "rev-parse", "kankan-agents"))
    pending = fake.add_card(project.cfg.backlog, "ke-1 pending", pos=0)
    workflow.tick(project, claude)
    assert not es.settled(project, c, {**cards, "p": c.card(pending["id"])}, origin)


def test_runner_prerequisites_missing_config(monkeypatch, isolated_home, capsys):
    from tests.live import agents_e2e
    monkeypatch.setenv("KANKAN_REAL_HOME", str(isolated_home))
    monkeypatch.delenv("KANKAN_TEST_BOARD_URL", raising=False)
    monkeypatch.delenv("KANKAN_TEST_CREDENTIALS", raising=False)
    assert agents_e2e.main([]) == 2
    assert "no test board configured" in capsys.readouterr().out


def test_ask_check_ignores_blank_answers():
    """M9: an empty comment is not an answer."""
    path, check, _ = es.EXPECTED_FILES["ask"]
    assert not check("red\n", {"answers": ["", "   "]})
    assert check("red\n", {"answers": ["", "Red"]})


def test_teardown_board_by_clone_path(board):
    """I3: before the configuration exists, the registry block is removed only if it names this run's clone."""
    fake, b, _ = board
    fake.state["boards"][b["id"]]["desc"] = write_registry_block(f"{MARKER}", {"registry-id": "x", "path": "/run/clone"})
    es.teardown_board(client(fake), b["id"], [], None, clone_path="/other")
    assert registry_block(fake.state["boards"][b["id"]]["desc"])
    es.teardown_board(client(fake), b["id"], [], None, clone_path="/run/clone")
    assert registry_block(fake.state["boards"][b["id"]]["desc"]) is None


def test_runner_failure_after_bind_tears_down(board, tmp_path, monkeypatch, capsys, virtual_time):
    """I3: a manual project check after bind still removes the run's registry block."""
    from tests.live import agents_e2e
    from kankan import doctor as doctor_mod
    fake, b, creds = board
    monkeypatch.setattr(agents_e2e, "prerequisites", lambda: (b["url"], KEY, TOKEN, creds))
    monkeypatch.setattr(agents_e2e, "_tmux", lambda: type("T", (), {"sessions": lambda self: []})())
    real_client = TrelloClient

    def fake_client(key, token, **kw):
        return real_client(key, token, base_url=fake.url, timeout=0.5)
    monkeypatch.setattr(agents_e2e, "TrelloClient", fake_client)
    monkeypatch.setattr(doctor_mod, "run", lambda *a, **k: [doctor_mod.Check("hook", doctor_mod.MANUAL, "x", "y")])
    assert agents_e2e.main(["--base", str(tmp_path)]) == 2
    assert registry_block(fake.state["boards"][b["id"]]["desc"]) is None
    assert "run directory kept" in capsys.readouterr().out


def test_clean_leftovers(board, tmp_path):
    """--clean kills leftover sessions, cleans the board and removes old run directories only."""
    from tests.live import agents_e2e
    fake, b, _ = board
    lst = fake.add_list(b["id"], "Backlog")
    leftover = fake.add_card(lst["id"], "ke-1 old")
    fake.state["boards"][b["id"]]["desc"] = write_registry_block(f"{MARKER}", {"registry-id": "old"})
    (tmp_path / "kankan-e2e-ke-1-abc").mkdir()
    (tmp_path / "unrelated").mkdir()

    class StubTmux:
        def __init__(self):
            self.names = ["ai-kankan-e2e-codex-1", "ai-other-claude-1"]

        def sessions(self):
            return list(self.names)

        def kill_session(self, name):
            self.names.remove(name)
    tmux = StubTmux()
    actions = agents_e2e.clean_leftovers(tmux, client(fake), b["id"], tmp_path)
    assert tmux.names == ["ai-other-claude-1"]
    assert leftover["id"] not in fake.state["cards"] and registry_block(fake.state["boards"][b["id"]]["desc"]) is None
    assert not (tmp_path / "kankan-e2e-ke-1-abc").exists() and (tmp_path / "unrelated").exists()
    assert any("killed" in a for a in actions)



def test_kankan_link_created_and_removed(isolated_home, tmp_path, monkeypatch):
    """The runner puts this checkout's kankan on the login PATH (~/.local/bin) for the run and removes its own link."""
    import sys
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "kankan").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    link = isolated_home / ".local" / "bin" / "kankan"
    created = es.ensure_kankan_link()
    assert created == link and os.readlink(link) == str(venv_bin / "kankan")
    assert es.ensure_kankan_link() is None  # already there: not ours to remove later
    es.remove_kankan_link(created)
    assert not link.exists() and not link.is_symlink()


def test_kankan_link_refuses_another_installation(isolated_home, tmp_path, monkeypatch):
    import sys
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "kankan").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    link = isolated_home / ".local" / "bin" / "kankan"
    link.parent.mkdir(parents=True)
    link.write_text("#!/bin/sh\n# a pipx copy\n")
    with pytest.raises(es.LinkError) as exc:
        es.ensure_kankan_link()
    assert str(link) in str(exc.value)
    es.remove_kankan_link(link)  # never removes something that is not our link
    assert link.exists()


# ---------------------------------------------------------------- attachments and board settings

def test_attach_card_starts_on_hold(board, tmp_path, virtual_time):
    fake, b, creds = board
    clone, _ = es.make_repos(tmp_path)
    cfg = es.bind(clone, tmp_path, b["url"], creds, lambda text: None)
    cards = es.create_cards(client(fake), cfg, "ke-1", es.scenario(ask=False, hold=False))
    assert cfg.label_hold in fake.state["cards"][cards["attach"].id]["idLabels"]
    assert cfg.label_hold not in fake.state["cards"][cards["hello"].id]["idLabels"]


def test_user_upload_and_sha256(fake_trello):
    import hashlib
    b = fake_trello.add_board("b")
    card = fake_trello.add_card(fake_trello.add_list(b["id"], "Backlog")["id"], "c")
    person = fake_trello.add_member("person")
    c = client(fake_trello)
    fake_trello.upload(card["id"], "mine.txt", b"bot\n", member_id=fake_trello.me["id"])  # kankan's own upload is not a task input
    fake_trello.add_attachment(card["id"], "https://example.com/x", member_id=person["id"])
    assert es.user_upload(c, card["id"], fake_trello.me["id"]) is None
    fake_trello.upload(card["id"], "theirs.txt", b"from a person\n", "text/plain", member_id=person["id"])
    upload = es.user_upload(c, card["id"], fake_trello.me["id"])
    assert upload.file_name == "theirs.txt"
    assert es.attachment_sha256(c, card["id"], upload, 1024) == hashlib.sha256(b"from a person\n").hexdigest()


def test_attach_expected_file():
    path, check, _ = es.EXPECTED_FILES["attach"]
    assert path == "hashes.txt"
    assert check("ab" * 32 + "\n", {"sha256": "ab" * 32})
    assert not check("ab" * 32 + "  theirs.txt\n", {"sha256": "ab" * 32})
    assert not check("\n", {"sha256": ""})


def _settings_read(ticks):
    """A settings read with cards {role: {type: [(item, ticked)]}}."""
    from kankan.boardconfig import title
    from kankan.trello import Card, CheckItem, Checklist, SettingsRead
    cards = []
    for n, (role, lists) in enumerate(ticks.items()):
        card = Card(f"c{n}", f"s{n}", "b", title(role), "", "", "l", n, [], [], False, "")
        card.checklists = [Checklist(f"{role}-{t}", card.id, t, i,
                                     [CheckItem(f"{role}-{t}-{j}", name, "complete" if on else "incomplete", j)
                                      for j, (name, on) in enumerate(items)])
                           for i, (t, items) in enumerate(lists.items())]
        cards.append(card)
    return SettingsRead(cards, None, True)


def _adapters():
    from types import SimpleNamespace
    return {"claude": SimpleNamespace(type="claude", command=["claude"], model_args=[], effort_args=[],
                                      models={"opus": {"efforts": ["high", "low"]}, "haiku": {"efforts": ["low"]}})}


def test_settings_targets_pick_cheapest_new_review_pair():
    from kankan.adapters import ModelEffort
    items = [("opus · high", False), ("opus · low", False), ("haiku · low", False)]
    impl = [(n, n == "opus · high") for n, _ in items]
    review = [(n, n == "haiku · low") for n, _ in items]
    read = _settings_read({"implementation": {"claude": impl}, "review": {"claude": review}})
    targets = es.settings_targets(read, _adapters())
    assert targets == {"claude": ModelEffort("opus", "low")}
    assert not es.settings_ready(read, targets)
    done = _settings_read({"review": {"claude": [(n, n == "opus · low") for n, _ in items]}})
    assert es.settings_ready(done, targets)
    two = _settings_read({"review": {"claude": [(n, n != "opus · high") for n, _ in items]}})
    assert not es.settings_ready(two, targets)


def test_settings_targets_without_a_free_pair():
    items = [("opus · high", True), ("opus · low", True), ("haiku · low", True)]
    read = _settings_read({"implementation": {"claude": items}, "review": {"claude": items}})
    with pytest.raises(ValueError):
        es.settings_targets(read, _adapters())


def test_selection_problems():
    from kankan.adapters import ModelEffort, start_command
    adapters = _adapters()
    adapters["claude"].model_args, adapters["claude"].effort_args = ["--model", "{model}"], ["--effort", "{effort}"]
    card = type("C", (), {"id": "card1", "name": "ke-1 word"})()
    impl, review = ModelEffort("opus", "high"), ModelEffort("opus", "low")
    expected = {"implementation": {"claude": impl}, "review": {"claude": review}}
    events = [_ev("agent_started", seat="claude-1", command=start_command(adapters["claude"], impl)),
              _ev("task_assigned", seat="claude-1", card="card1", role="implementation", model="opus", effort="high"),
              _ev("task_assigned", seat="claude-1", card="card1", role="review", model="opus", effort="low")]
    problems = es.selection_problems(events, {"word": card}, expected, adapters)
    assert len(problems) == 1 and "started with" in problems[0], problems
    events.insert(2, _ev("agent_started", seat="claude-1", command=start_command(adapters["claude"], review)))
    assert es.selection_problems(events, {"word": card}, expected, adapters) == []
    events[-1]["effort"] = "high"
    assert "instead of opus/low" in es.selection_problems(events, {"word": card}, expected, adapters)[0]


def test_applied_problems():
    from kankan.adapters import ModelEffort
    targets = {"claude": ModelEffort("haiku", "low"), "codex": ModelEffort("mini", "low")}
    ok = "applied: `claude opus/high, codex big/high` → `claude haiku/low, codex mini/low`, from the next task"
    assert es.applied_problems(["help", ok], targets) == []
    assert "no `applied:`" in es.applied_problems([], targets)[0]
    wrong = "applied: `claude haiku/low, codex big/high` → `claude opus/high, codex mini/low`, from the next task"
    assert len(es.applied_problems([wrong], targets)) == 1


def test_restart_report():
    events = [_ev("task_assigned", seat="claude-1", model="opus", effort="high"),
              _ev("task_assigned", seat="claude-1", model="opus", effort="low"),
              _ev("task_assigned", seat="codex-1", model="big", effort="high"),
              _ev("task_assigned", seat="codex-1", model="mini", effort="low"),
              _ev("agent_restart", seat="codex-1", ended="exit_command"),
              _ev("task_assigned", seat="codex-2", model="big", effort="high"),
              _ev("task_assigned", seat="codex-2", model="big", effort="high")]
    problems, seen = es.restart_report(events)
    assert problems == ["claude-1: ran tasks with 2 model/effort pairs but never restarted its agent"]
    assert seen == ["codex-1: old agent ended by exit_command"]
    events.append(_ev("agent_restart", seat="claude-1", ended="sigterm"))
    problems, _ = es.restart_report(events)
    assert problems == ["claude-1: old agent needed sigterm (the exit command did not end it)"]


def test_verify_run_checks_selection_and_claim_comments(run):
    from kankan import boardconfig
    project, fake, c, cards, origin, refs, _ = run
    selection = {role: dict(boardconfig.effective(project.paths, role)) for role in es.ROLES}
    problems = es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs,
                             selection=selection)
    assert problems == [], problems
    other = {role: dict(values) for role, values in selection.items()}
    from kankan.adapters import ModelEffort
    other["review"]["codex"] = ModelEffort("not-a-model", "low")
    problems = es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs,
                             selection=other)
    assert any("not-a-model" in p and "review" in p for p in problems), problems
    events = [dict(e, model="other") if e.get("event") == "task_assigned" else e for e in auditlog.read(project.paths)]
    assert es.claim_comment_problems(c, events, cards, project.cfg.kankan_member_id), "claim comments not checked"


def test_verify_run_attachment_dir_left(run):
    from kankan import attachments
    project, fake, c, cards, origin, refs, _ = run
    attachments.card_dir(project.paths, cards["word"].short).mkdir(parents=True)
    cards = {"attach": cards["word"], "name": cards["name"]}
    problems = es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs,
                             attach_sha256="ab" * 32)
    assert any("attachment directory" in p for p in problems), problems


# ---------------------------------------------------------------- Q&A dialogue (questions F§8)

def _qa_comment(fake, card_id, kind, conversation, turn, index, text):
    return fake._comment(card_id, f"{text}\n\n`kankan:{kind}-{conversation}-{turn}-{index}`", fake.me["id"])


def test_qa_turns_counts_answered_turns_and_handoffs(fake_trello):
    b = fake_trello.add_board("b")
    card = fake_trello.add_card(fake_trello.add_list(b["id"], "Q&A")["id"], "q")
    c = client(fake_trello)
    assert es.qa_turns(c, card["id"], fake_trello.me["id"]) == {"qa": 0, "qh": 0}
    _qa_comment(fake_trello, card["id"], "qa", "a" * 16, "b" * 16, 1, "1/2 first part")
    _qa_comment(fake_trello, card["id"], "qa", "a" * 16, "b" * 16, 2, "2/2 second part")
    _qa_comment(fake_trello, card["id"], "qa", "a" * 16, "c" * 16, 1, "another answer")
    fake_trello.user_comment(card["id"], "`kankan:qa-" + "a" * 16 + "-" + "d" * 16 + "-1` typed by a person")
    assert es.qa_turns(c, card["id"], fake_trello.me["id"]) == {"qa": 2, "qh": 0}
    _qa_comment(fake_trello, card["id"], "qh", "a" * 16, "e" * 16, 1, "the summary")
    assert es.qa_turns(c, card["id"], fake_trello.me["id"]) == {"qa": 2, "qh": 1}


def test_qa_expected_file():
    path, check, _ = es.EXPECTED_FILES["qa"]
    assert path == "qa.txt"
    assert check("blue sky\n", {"qa_line": "blue sky"})
    assert not check("blue sky\nmore\n", {"qa_line": "blue sky"})
    assert not check("\n", {"qa_line": ""})


def test_qa_problems():
    card = type("C", (), {"id": "q1", "name": "ke-1 Ask about check.sh"})()
    good = [{"event": "task_assigned", "card": "q1", "seat": "claude-qa", "kind": "qa"},
            {"event": "task_assigned", "card": "q1", "seat": "codex-1", "kind": "impl"},
            {"event": "task_assigned", "card": "q1", "seat": "claude-1", "kind": "review"}]
    turns = {"qa": 2, "qh": 1}
    assert es.qa_problems(good, card, turns) == []
    assert any("answers" in p for p in es.qa_problems(good, card, {"qa": 1, "qh": 1}))
    assert any("handoff" in p for p in es.qa_problems(good, card, {"qa": 2, "qh": 0}))
    assert any("never" in p for p in es.qa_problems(good[:1], card, turns))
    wrong = good + [{"event": "task_assigned", "card": "q1", "seat": "claude-qa", "kind": "impl"}]
    assert any("claude-qa" in p for p in es.qa_problems(wrong, card, turns))


def test_verify_run_reports_a_left_conversation_directory(run):
    project, fake, c, cards, origin, refs, _ = run
    assert es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs, qa_line=None) == []
    (project.paths.kankan_dir / "qa" / "abc123").mkdir(parents=True)
    problems = es.verify_run(project, c, cards, origin, ask_ran=False, hold_ran=False, user_refs=refs)
    assert any("conversation directories left: abc123" in p for p in problems), problems


def _card_in(list_id):
    from kankan.trello import Card
    return Card("id-" + list_id, "s", "b", "n", "", "u", list_id, 1.0, [], [], False, "")


def test_not_done_and_stray_cards():
    from types import SimpleNamespace
    cfg = SimpleNamespace(done="D", workflow_lists=lambda: {"B": "Backlog", "D": "Done", "Q": "Q&A"})
    names = {"B": "Backlog", "D": "Done", "Q": "Q&A", "X": "archive"}
    cards = {k: _card_in("B") for k in ("hello", "qa", "ask", "gone")}
    state = {"hello": _card_in("D"), "qa": _card_in("X"), "ask": _card_in("Q")}
    assert es.not_done(cfg, cards, state, names) == ["qa (archive)", "ask (Q&A)", "gone (archived or deleted)"]
    assert es.stray_cards(cfg, cards, state, names) == {"qa": "in list 'archive', outside the workflow",
                                                        "gone": "archived or deleted"}


def test_instructions_card_and_check():
    from kankan.cardsections import for_seat, parse
    card = next(c for c in es.scenario() if c.key == "instr")
    parsed = parse(card.desc, ["claude", "codex"])
    assert parsed.valid and "never push `main`" in parsed.spec and "agent:" not in parsed.spec
    assert "advisor" in for_seat(parsed, "claude", "review") and "advisor" not in for_seat(parsed, "codex", "review")
    assert for_seat(parsed, "claude", "implementation") is None
    good = [("claude-1", "card-instructions: claude review, advisor consulted"), ("claude-1", "no teleport tool here")]
    assert es.instruction_problems(good, "claude-1") == []
    assert es.instruction_problems(good, None)  # never reviewed
    assert es.instruction_problems([("claude-1", "card-instructions: claude review"), ("claude-1", "teleport")], "claude-1")
    assert es.instruction_problems(good + [("codex-1", "card-instructions: codex review")], "claude-1")  # not the reviewer
    assert es.instruction_problems([("codex-1", "card-instructions: codex review")], "codex-1")  # no teleport note
    assert es.instruction_problems([("codex-1", "card-instructions: codex review"), ("codex-1", "teleport: no")], "codex-1") == []
    events = [{"event": "task_assigned", "card": "c", "kind": "impl", "seat": "claude-1"},
              {"event": "task_assigned", "card": "c", "kind": "review", "seat": "codex-1"}]
    assert es.reviewer_seat(events, "c") == "codex-1" and es.reviewer_seat(events, "d") is None
