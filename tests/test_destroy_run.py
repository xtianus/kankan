"""``kankan destroy``: questions and execution (destroy spec F§3–§7, T§4–§5)."""

import fcntl
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from kankan import cardsections, destroy, messages, seats
from kankan.config import Paths, REGISTRY_START, load_config, write_registry_block
from kankan.errors import KankanError, Status
from kankan.trello import TrelloClient
from kankan.init import init
from kankan.project import Project
from kankan.refstore import card_ref, seat_ref
from tests.conftest import git
from tests.fakes.fake_trello import KEY, TOKEN
from tests.helpers import NoTmux, bind_project, put_card_ref

NAME = "Team Board 2"
SLUG = "old-name"
DESC = "Intro text.\n\nMore about the team."
SHORT = "Ab12Cd34"
RESUME = "Resume 2026-11-20 10:00"
RESUME_OUTSIDE = "Resume 2026-11-21 11:00"
SEAT_STEPS = ("In Progress", "Implemented", "Review")
COMMON = ("Backlog", "Done", "Blocked", "Q&A", "kankan")


def answers(*values, default=""):
    queue = list(values)
    return lambda question: queue.pop(0) if queue else default


def run_init(repo, board):
    cfg, _ = init(repo, board["url"], prompt=answers(), say=lambda text: None)
    return cfg


class Full:
    """What ``full_project`` built, for the assertions."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def full_project(git_repo, fake_trello, monkeypatch, isolated_home):
    """A project created through real ``init`` against the fake board ``Team Board 2`` (URL slug ``old-name``),
    with a seat and its worktree, a blocked worktree, cards in every kankan list (one archived), a ``Resume``
    label, a foreign list and labels, a downloaded ``yarn.lock``, a user exclude line and user-level sentinels."""
    fake = fake_trello
    monkeypatch.setenv("KANKAN_TRELLO_URL", fake.url)
    monkeypatch.setenv("KANKAN_TRELLO_KEY", KEY)
    monkeypatch.setenv("KANKAN_TRELLO_TOKEN", TOKEN)
    monkeypatch.setattr(seats, "pane_sid", lambda: None)
    board = fake.add_board(NAME, DESC, url_slug=SLUG)
    run_init(git_repo, board)
    project = Project.open(git_repo)
    seats.ensure_created(project, "claude-1", "claude")
    project = Project.open(git_repo)
    cfg = project.cfg
    board_id = cfg.board_id
    seat = cfg.seats["claude-1"]
    resume = fake.add_label(board_id, RESUME)["id"]
    resume_outside = fake.add_label(board_id, RESUME_OUTSIDE)["id"]
    hiring = fake.add_label(board_id, "Resume hiring")["id"]
    ideas = fake.add_list(board_id, "Ideas")["id"]
    ideas_card = fake.add_card(ideas, "an idea")["id"]
    fake.user_label(ideas_card, resume_outside)
    first = fake.add_card(cfg.backlog, "first")["id"]
    fake.user_label(first, resume)
    fake.add_card(cfg.backlog, "second")
    fake.add_card(cfg.done, "done")
    archived = fake.add_card(cfg.done, "archived")["id"]
    fake.user_archive(archived)
    fake.add_card(cfg.qa_list, "question")
    held = fake.add_card(seat.in_progress, "in progress")
    put_card_ref(project, held["id"], held["shortLink"], phase="impl", seat="claude-1")
    oid = project.refs.write_blob({"schema": 1, "seat": "claude-1"})
    project.refs.create(seat_ref("claude-1"), oid)
    blocked = project.paths.blocked_worktree(SHORT)
    git(git_repo, "worktree", "add", "-q", "--detach", str(blocked))
    git(git_repo, "branch", "26kankan-" + SHORT)
    yarn = project.paths.kankan_dir / "attachments" / SHORT / "att1" / "yarn.lock"
    yarn.parent.mkdir(parents=True)
    yarn.write_text("lock content")
    exclude = project.paths.gcd / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "*.tmp\n")
    sentinels = [isolated_home / ".config" / "kankan" / "sentinel", isolated_home / ".cache" / "kankan" / "sentinel"]
    for path in sentinels:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep")
    fake.requests.clear()  # the setup's own writes; the tests look at destroy's requests only
    return Full(fake=fake, board=board, project=project, cfg=cfg, repo=git_repo, paths=project.paths,
                resume=resume, resume_outside=resume_outside, hiring=hiring, ideas=ideas, ideas_card=ideas_card,
                sentinels=sentinels, seat_wt=project.paths.seat_worktree("claude-1"), blocked_wt=blocked)


class Script:
    """A scripted ``ask``: answers in order (``None`` for Ctrl-C/EOF); an answer may be a callable run first."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply()
        return reply


def no_tty(prompt):
    raise KankanError(Status.USAGE_ERROR, "kankan destroy needs a phrase: run it in a terminal or pass --confirm")


def run(repo, ask=None, confirm=(), said=None):
    said = [] if said is None else said
    ctx = destroy.Ctx.open(repo)
    return destroy.destroy(ctx, tmux=NoTmux(), ask=ask or no_tty, say=said.append, confirm=list(confirm))


def board_phrase():
    return f"destroy {NAME}"


def writes(fake):
    """Requests that change the board."""
    return [(method, path) for method, path in fake.requests if method in ("DELETE", "PUT", "POST")]


def disk(paths):
    """Every path under the git common dir and the worktree root, with its kind, size and mtime."""
    result = []
    for root in (paths.gcd, paths.worktree_root):
        for path in sorted(Path(root).rglob("*")):
            info = os.lstat(path)
            result.append((str(path), info.st_mode, info.st_size, info.st_mtime_ns))
    return result


def branches(repo):
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")


def destroy_full(full):
    """The right phrase on the full project; returns the report."""
    script = Script(board_phrase())
    report = run(full.repo, script)
    assert len(script.prompts) == 1
    return report


def target_lists(cfg):
    ids = [cfg.backlog, cfg.done, cfg.blocked, cfg.qa_list, cfg.settings_list]
    for seat in cfg.seats.values():
        if seat.role != "qa":
            ids += [seat.in_progress, seat.implemented, seat.review]
    return set(ids)


def only_locks(kankan_dir):
    files = [p.relative_to(kankan_dir) for p in kankan_dir.rglob("*") if not p.is_dir()]
    return all(destroy._is_lock(p.parts) for p in files), files


# --- The whole run ------------------------------------------------------------------------------------------

def test_full_destroy(full_project):
    full = full_project
    fake, cfg, paths = full.fake, full.cfg, full.paths
    before_branches = branches(full.repo)
    label_ids = {cfg.label_hold, cfg.label_question, cfg.label_error}
    report = destroy_full(full)
    state = fake.state
    # F§7.3: the board
    targets = target_lists(cfg)
    assert not [c for c in state["cards"].values() if c["idList"] in targets]
    seat = cfg.seats["claude-1"]
    for list_id in (seat.in_progress, seat.implemented, seat.review):
        assert state["lists"][list_id]["closed"]
    for list_id in (cfg.backlog, cfg.done, cfg.blocked, cfg.qa_list, cfg.settings_list):
        assert not state["lists"][list_id]["closed"]
    labels = state["labels"]
    assert full.resume not in labels
    assert full.resume_outside in labels and full.hiring in labels
    assert label_ids <= set(labels)
    assert state["boards"][cfg.board_id]["desc"] == DESC
    assert full.ideas_card in state["cards"] and not state["lists"][full.ideas]["closed"]
    # F§7.4: the clone
    assert not full.seat_wt.exists() and not full.blocked_wt.exists()
    assert git(full.repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert git(full.repo, "for-each-ref", "refs/agent-cards", "refs/agent-seats") == ""
    ok, files = only_locks(paths.kankan_dir)
    assert ok, files
    assert not (paths.kankan_dir / "attachments").exists()
    assert not (paths.kankan_dir / "config.json").exists() and not (paths.kankan_dir / "log.jsonl").exists()
    assert (paths.kankan_dir / "project.lock").exists()
    assert not (paths.gcd / "hooks" / "pre-push").exists()
    assert subprocess.run(["git", "-C", str(full.repo), "config", "--get", "kankan.integrationBranch"],
                          capture_output=True).returncode == 1
    exclude = (paths.gcd / "info" / "exclude").read_text().splitlines()
    assert "# kankan" not in exclude and "/.kankan/" not in exclude and "*.tmp" in exclude
    assert branches(full.repo) == before_branches
    assert all(path.read_text() == "keep" for path in full.sentinels)
    assert (Path.home() / ".config" / "kankan" / "agents.json").exists()
    # the report
    board = report["board"]
    assert board["url"] == f"https://trello.com/b/{cfg.board_short}"
    assert board["registry_removed"] and sum(board["cards_deleted"].values()) >= 7
    assert board["labels_deleted"] == [RESUME]
    assert sorted(board["lists_archived"]) == sorted(f"claude-1 · {step}" for step in SEAT_STEPS)
    assert report["board_skipped"] is None and report["manual_board_cleanup"] is None
    assert sorted(report["worktrees_removed"]) == sorted([str(full.seat_wt), str(full.blocked_wt)])
    assert report["refs_deleted"] == 2 and report["hook"] == "removed"
    assert report["git_config"] and report["exclude"] and report["state_removed"]
    assert sorted(item["name"] for item in report["kept_lists"]) == sorted(COMMON)
    assert report["next"] == f"kankan init https://trello.com/b/{cfg.board_short}"
    text = destroy.report_text(report)
    assert RESUME_OUTSIDE in text and "kankan init https://trello.com/b/" in text


def test_wrong_phrase_cancels(full_project):
    full = full_project
    for answer in ("destroy team board 2", f"destroy {SLUG}", "", None, "remove local kankan state"):
        before = (repr(full.fake.state), disk(full.paths))
        requests = len(full.fake.requests)
        report = run(full.repo, Script(answer))
        assert report == {"cancelled": True}
        assert (repr(full.fake.state), disk(full.paths)) == before
        assert not writes(full.fake) and all(m == "GET" for m, _ in full.fake.requests[requests:])
    assert destroy.report_text({"cancelled": True}) == "cancelled: nothing changed"


def test_name_never_printed(full_project):
    full = full_project
    said = []
    script = Script(None)
    run(full.repo, script, said=said)
    with pytest.raises(KankanError) as caught:
        run(full.repo, confirm=[f"destroy {SLUG}"], said=said)
    assert caught.value.status == Status.USAGE_ERROR
    report = run(full.repo, Script(board_phrase()), said=said)
    texts = said + script.prompts + [destroy.report_text(report), caught.value.message]
    for text in texts:
        assert NAME not in text and SLUG not in text
    assert any(f"https://trello.com/b/{full.cfg.board_short}" in text for text in said)
    assert NAME not in repr(report) and SLUG not in repr(report)


# --- Work at risk and the questions -------------------------------------------------------------------------

def test_at_risk_needs_discard(full_project):
    full = full_project
    (full.seat_wt / "new.txt").write_text("work")
    said = []
    script = Script(destroy.DISCARD_PHRASE, None)
    assert run(full.repo, script, said=said) == {"cancelled": True}
    assert destroy.DISCARD_PHRASE in script.prompts[0] and len(script.prompts) == 2
    text = "\n".join(said)
    wt = shlex.quote(str(full.seat_wt))
    assert "new.txt" in text and f"git -C {wt} status" in text
    assert f"git -C {wt} clean -fd" in text and f"git -C {wt} switch -c" in text
    assert not writes(full.fake) and (full.seat_wt / "new.txt").exists()
    script = Script(None)
    assert run(full.repo, script) == {"cancelled": True}
    assert len(script.prompts) == 1 and not writes(full.fake)
    report = run(full.repo, Script(destroy.DISCARD_PHRASE, board_phrase()))
    assert str(full.seat_wt) in report["worktrees_removed"] and not full.seat_wt.exists()


def test_block_snapshot_needs_discard(full_project):
    full = full_project
    snap = full.paths.kankan_dir / "blocks" / SHORT
    (snap / "files" / "dir").mkdir(parents=True)
    (snap / "files" / "dir" / "work.txt").write_text("w")
    said = []
    script = Script(None)
    assert run(full.repo, script, said=said) == {"cancelled": True}
    assert destroy.DISCARD_PHRASE in script.prompts[0]
    text = "\n".join(said)
    assert str(snap) in text and "dir/work.txt" in text and "kankan agent" not in text
    assert not writes(full.fake)


def test_block_journal_names_launcher(full_project):
    full = full_project
    from kankan.seatstate import update_state
    update_state(full.paths, "claude-1", lambda s: setattr(s, "block_journal", {"short": SHORT, "files": ["a"]}))
    said = []
    assert run(full.repo, Script(None), said=said) == {"cancelled": True}
    assert "kankan agent claude --seat claude-1" in "\n".join(said)


def test_ignored_only_asks_once(full_project):
    full = full_project
    exclude = full.paths.gcd / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "build/\n")
    (full.seat_wt / "build").mkdir()
    (full.seat_wt / "build" / "out.o").write_text("o")
    said = []
    script = Script(board_phrase())
    report = run(full.repo, script, said=said)
    assert len(script.prompts) == 1 and destroy.DISCARD_PHRASE not in script.prompts[0]
    assert "build/out.o" in "\n".join(said)
    assert not full.seat_wt.exists() and report["board"]["registry_removed"]


def test_confirm_rules(full_project):
    full = full_project
    (full.seat_wt / "new.txt").write_text("work")
    for confirm in ([destroy.DISCARD_PHRASE], [board_phrase()], [destroy.DISCARD_PHRASE, board_phrase(), "x"],
                    [destroy.DISCARD_PHRASE, destroy.DISCARD_PHRASE, board_phrase()],
                    [destroy.DISCARD_PHRASE, destroy.LOCAL_PHRASE]):
        with pytest.raises(KankanError) as caught:
            run(full.repo, confirm=confirm)
        assert caught.value.status == Status.USAGE_ERROR, confirm
        assert NAME not in caught.value.message
        assert not writes(full.fake) and (full.seat_wt / "new.txt").exists()
    report = run(full.repo, confirm=[f"  {board_phrase()} ", destroy.DISCARD_PHRASE])
    assert not full.seat_wt.exists() and report["board"]["registry_removed"]


def test_discard_confirm_when_nothing_at_risk(full_project):
    full = full_project
    with pytest.raises(KankanError) as caught:
        run(full.repo, confirm=[destroy.DISCARD_PHRASE, board_phrase()])
    assert caught.value.status == Status.USAGE_ERROR and not writes(full.fake)


def test_confirm_answers_question_with_terminal(full_project):
    full = full_project
    (full.seat_wt / "new.txt").write_text("work")
    script = Script(board_phrase())
    run(full.repo, script, confirm=[destroy.DISCARD_PHRASE])
    assert len(script.prompts) == 1 and destroy.DISCARD_PHRASE not in script.prompts[0]


# --- Re-checks under the lock -------------------------------------------------------------------------------

def assert_needs_action_nothing_deleted(full, ask, text=None):
    with pytest.raises(KankanError) as caught:
        run(full.repo, ask)
    assert caught.value.status == Status.NEEDS_ACTION
    if text is not None:
        assert caught.value.message.startswith(text)
    assert not [r for r in writes(full.fake) if r[0] == "DELETE" or r[1].endswith("/closed")]
    assert full.seat_wt.exists() and (full.paths.kankan_dir / "config.json").exists()
    assert git(full.repo, "for-each-ref", "refs/agent-cards") != ""
    return caught.value


def test_rename_after_answer_cancels(full_project):
    full = full_project

    def rename():
        full.fake.state["boards"][full.cfg.board_id]["name"] = "Renamed"
        return board_phrase()
    err = assert_needs_action_nothing_deleted(full, Script(rename), messages.DESTROY_BOARD_CHANGED_AFTER)
    assert not writes(full.fake)
    assert NAME not in err.message and "Renamed" not in err.message


def test_rebind_after_answer_cancels(full_project):
    full = full_project

    def rebind():
        board = full.fake.state["boards"][full.cfg.board_id]
        board["desc"] = write_registry_block(DESC, {"registry-id": "other0000000000"})
        return board_phrase()
    assert_needs_action_nothing_deleted(full, Script(rebind), messages.DESTROY_BOARD_CHANGED_AFTER)
    assert not writes(full.fake)


def test_block_removed_after_answer_cancels(full_project):
    full = full_project

    def unbind():
        full.fake.state["boards"][full.cfg.board_id]["desc"] = DESC
        return board_phrase()
    assert_needs_action_nothing_deleted(full, Script(unbind), messages.DESTROY_BOARD_CHANGED_AFTER)


def test_gate_after_answer(full_project):
    full = full_project
    held = []

    def take_lock():
        path = full.paths.seats_dir / "claude-1.lock"
        path.parent.mkdir(exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(fd)
        return board_phrase()
    try:
        err = assert_needs_action_nothing_deleted(full, Script(take_lock))
    finally:
        for fd in held:
            os.close(fd)
    assert "kankan stop" in err.message and not writes(full.fake)


def test_dirty_after_answer_cancels(full_project):
    full = full_project

    def dirty():
        (full.seat_wt / "late.txt").write_text("late work")
        return board_phrase()
    err = assert_needs_action_nothing_deleted(full, Script(dirty))
    assert "changed since the inventory" in err.message and str(full.seat_wt) in err.message
    assert (full.seat_wt / "late.txt").exists()


def test_other_worktree_dirty_after_discard_cancels(full_project):
    """The discard phrase covers only the worktrees at risk at the inventory: another one dirtied while the
    questions are open stops destroy before the first board write."""
    full = full_project
    (full.seat_wt / "new.txt").write_text("work")
    late = full.blocked_wt / "late.txt"

    def dirty():
        late.write_text("late work")
        return board_phrase()
    err = assert_needs_action_nothing_deleted(full, Script(destroy.DISCARD_PHRASE, dirty))
    assert "changed since the inventory" in err.message and str(full.blocked_wt) in err.message
    assert not writes(full.fake)
    assert late.read_text() == "late work" and full.blocked_wt.exists()
    assert (full.seat_wt / "new.txt").exists()


def test_discarded_worktree_edited_after_answer_removed(full_project):
    """Edits inside a worktree confirmed for discard while the questions are open are discarded with it."""
    full = full_project
    (full.seat_wt / "new.txt").write_text("work")

    def edit():
        (full.seat_wt / "new.txt").write_text("more work")
        (full.seat_wt / "later.txt").write_text("later work")
        return board_phrase()
    report = run(full.repo, Script(destroy.DISCARD_PHRASE, edit))
    assert str(full.seat_wt) in report["worktrees_removed"] and not full.seat_wt.exists()
    assert not full.blocked_wt.exists() and report["state_removed"]


def test_gate_before_questions(full_project):
    full = full_project
    full.paths.seats_dir.mkdir(exist_ok=True)
    fd = os.open(full.paths.seats_dir / "claude-1.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        script = Script()
        with pytest.raises(KankanError) as caught:
            run(full.repo, script)
    finally:
        os.close(fd)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == []
    assert "kankan stop" in caught.value.message and caught.value.data["running"]


# --- Interruptions ------------------------------------------------------------------------------------------

def test_card_404_during_delete(full_project, monkeypatch):
    full = full_project
    card = next(c for c in full.fake.state["cards"].values() if c["idList"] == full.cfg.backlog)
    real = TrelloClient.card_place

    def place_then_gone(self, card_id):
        result = real(self, card_id)
        if card_id == card["id"]:
            full.fake.user_delete(card_id)  # deleted by a user between the re-read and the delete
        return result

    monkeypatch.setattr(TrelloClient, "card_place", place_then_gone)
    report = run(full.repo, Script(board_phrase()))
    assert ("DELETE", f"/1/cards/{card['id']}") in full.fake.requests
    assert report["state_removed"] and not (full.paths.kankan_dir / "config.json").exists()


def test_user_deleted_card_during_question(full_project):
    full = full_project

    def user_delete():
        card = next(c for c in full.fake.state["cards"].values() if c["idList"] == full.cfg.backlog)
        full.fake.user_delete(card["id"])
        return board_phrase()
    report = run(full.repo, Script(user_delete))
    assert report["state_removed"]


def test_board_items_moved_during_question_kept(full_project):
    full = full_project
    card = next(c for c in full.fake.state["cards"].values()
                if c["idList"] == full.cfg.backlog and full.resume not in c["idLabels"])["id"]

    def move():
        full.fake.user_move(card, full.ideas)
        full.fake.user_label(full.ideas_card, full.resume)
        return board_phrase()
    report = run(full.repo, Script(move))
    assert card in full.fake.state["cards"] and full.resume in full.fake.state["labels"]
    assert report["board"]["labels_deleted"] == [] and report["state_removed"]
    assert ("DELETE", f"/1/cards/{card}") not in full.fake.requests


def test_interrupt_during_cards_then_rerun(full_project, virtual_time):
    full = full_project
    plan, _ = destroy.board_plan(destroy.Ctx.open(full.repo))
    order = [card for cards in plan.cards.values() for card in cards]
    full.fake.inject("DELETE", f"/1/cards/{order[2]}$", "500", times=10)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    err = caught.value
    assert err.status == Status.NEEDS_ACTION
    assert sum(err.data["board"]["cards_deleted"].values()) == 2
    assert err.data["left"] and not err.data["worktrees_removed"]
    assert NAME not in err.message and "injected" not in err.message
    assert (full.paths.kankan_dir / "config.json").exists() and full.seat_wt.exists()
    full.fake.faults.clear()
    plan, _ = destroy.board_plan(destroy.Ctx.open(full.repo))
    assert len([card for cards in plan.cards.values() for card in cards]) == len(order) - 2
    report = run(full.repo, Script(board_phrase()))
    assert report["state_removed"] and not full.seat_wt.exists()
    assert not [c for c in full.fake.state["cards"].values() if c["idList"] in target_lists(full.cfg)]


def test_interrupt_after_registry_then_rerun(full_project, monkeypatch):
    full = full_project
    original = destroy._remove_worktree

    def broken(ctx, target, **kwargs):
        raise KankanError(Status.NEEDS_ACTION, "simulated")
    monkeypatch.setattr(destroy, "_remove_worktree", broken)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    assert caught.value.status == Status.NEEDS_ACTION
    assert caught.value.data["board"]["registry_removed"]
    assert REGISTRY_START not in full.fake.state["boards"][full.cfg.board_id]["desc"]
    assert (full.paths.kankan_dir / "config.json").exists()
    assert git(full.repo, "for-each-ref", "refs/agent-cards") != ""
    monkeypatch.setattr(destroy, "_remove_worktree", original)
    said = []
    report = run(full.repo, Script(board_phrase()), said=said)
    assert report["board"]["registry_removed"] is False and sum(report["board"]["cards_deleted"].values()) == 0
    assert report["state_removed"] and not full.seat_wt.exists()
    assert full.fake.state["boards"][full.cfg.board_id]["desc"] == DESC


def test_worktree_failure_stops_before_refs(full_project, monkeypatch):
    full = full_project
    real = destroy.gitutil.run_bytes

    def failing(repo, args, **kwargs):
        if args[:2] == ["worktree", "remove"]:
            raise PermissionError(13, "Permission denied")
        return real(repo, args, **kwargs)
    monkeypatch.setattr(destroy.gitutil, "run_bytes", failing)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    err = caught.value
    assert err.status == Status.NEEDS_ACTION and "Permission denied" in err.message
    assert str(full.blocked_wt) in err.message or str(full.seat_wt) in err.message
    assert git(full.repo, "for-each-ref", "refs/agent-cards") != ""
    assert (full.paths.kankan_dir / "config.json").exists()


def test_state_permission_error_keeps_config(full_project, monkeypatch):
    full = full_project
    real = os.unlink

    def denied(path, *args, **kwargs):
        if str(path).endswith("yarn.lock"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(destroy.os, "unlink", denied)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    assert caught.value.status == Status.NEEDS_ACTION and "yarn.lock" in caught.value.message
    assert (full.paths.kankan_dir / "config.json").exists()
    monkeypatch.setattr(destroy.os, "unlink", real)
    report = run(full.repo, Script(board_phrase()))
    assert report["state_removed"]


def test_ref_changed_stops(full_project):
    full = full_project
    project = full.project
    ref = git(full.repo, "for-each-ref", "--format=%(refname)", "refs/agent-cards")
    new = {}

    def change():
        oid = project.refs.write_blob({"schema": 1, "changed": True})
        git(full.repo, "update-ref", ref, oid)
        new[ref] = oid
        return board_phrase()
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(change))
    err = caught.value
    # changed during the questions: the re-check under the lock stops before any write
    assert err.status == Status.NEEDS_ACTION and err.message == messages.DESTROY_REFS_CHANGED_BEFORE
    assert not writes(full.fake) and full.seat_wt.exists()
    assert git(full.repo, "rev-parse", ref) == new[ref]
    report = run(full.repo, Script(board_phrase()))
    assert report["refs_deleted"] == 2 and report["state_removed"]
    assert git(full.repo, "for-each-ref", "refs/agent-cards", "refs/agent-seats") == ""


def test_ref_changed_during_destruction_stops(full_project, monkeypatch):
    full = full_project
    project = full.project
    ref = git(full.repo, "for-each-ref", "--format=%(refname)", "refs/agent-cards")
    new = {}
    real = TrelloClient.archive_list

    def change(self, list_id):
        if not new:
            oid = project.refs.write_blob({"schema": 1, "changed": True})
            git(full.repo, "update-ref", ref, oid)
            new[ref] = oid
        return real(self, list_id)

    monkeypatch.setattr(TrelloClient, "archive_list", change)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    monkeypatch.setattr(TrelloClient, "archive_list", real)
    err = caught.value
    assert err.status == Status.NEEDS_ACTION and err.message.startswith(messages.DESTROY_REFS_CHANGED.split("{")[0])
    assert not [c for c in full.fake.state["cards"].values() if c["idList"] in target_lists(full.cfg)]
    assert not full.seat_wt.exists() and not full.blocked_wt.exists()
    assert git(full.repo, "rev-parse", ref) == new[ref]
    assert git(full.repo, "for-each-ref", "refs/agent-seats") != ""
    assert (full.paths.kankan_dir / "config.json").exists()
    report = run(full.repo, Script(board_phrase()))
    assert report["refs_deleted"] == 2 and report["state_removed"]
    assert git(full.repo, "for-each-ref", "refs/agent-cards", "refs/agent-seats") == ""


def test_symbolic_ref_refuses(full_project):
    full = full_project
    main = git(full.repo, "rev-parse", "refs/heads/main")
    git(full.repo, "symbolic-ref", "refs/agent-seats/claude-7", "refs/heads/main")
    script = Script()
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == []
    assert "git update-ref --no-deref -d refs/agent-seats/claude-7" in caught.value.message
    assert git(full.repo, "rev-parse", "refs/heads/main") == main and not writes(full.fake)


def test_unrelated_stale_registration_kept(full_project, tmp_path):
    full = full_project
    elsewhere = tmp_path / "elsewhere"
    git(full.repo, "worktree", "add", "-q", "--detach", str(elsewhere))
    admin = full.paths.gcd / "worktrees" / "elsewhere"
    assert admin.is_dir()
    shutil.rmtree(elsewhere)
    destroy_full(full)
    assert admin.is_dir()


def test_missing_target_unregistered(full_project):
    full = full_project
    shutil.rmtree(full.seat_wt)
    report = destroy_full(full)
    assert str(full.seat_wt) in report["worktrees_removed"]
    assert not (full.paths.gcd / "worktrees" / "claude-1").exists()


def test_user_worktree_under_root_kept(full_project):
    full = full_project
    mine = full.paths.worktree_root / "mine"
    git(full.repo, "worktree", "add", "-q", "--detach", str(mine))
    report = destroy_full(full)
    assert mine.is_dir() and full.paths.worktree_root.is_dir()
    assert any(str(mine) in text for text in report["kept"])


def test_foreign_hook_kept(full_project):
    full = full_project
    hook = full.paths.gcd / "hooks" / "pre-push"
    hook.write_text(hook.read_text() + "# mine\n")
    report = destroy_full(full)
    assert hook.exists() and report["hook"] == "foreign, kept"


# --- Board left alone ---------------------------------------------------------------------------------------

def test_other_clone_local_only(full_project):
    full = full_project
    board = full.fake.state["boards"][full.cfg.board_id]
    board["desc"] = write_registry_block(DESC, {"registry-id": "other0000000000"})
    desc = board["desc"]
    script = Script(destroy.LOCAL_PHRASE)
    report = run(full.repo, script)
    assert destroy.LOCAL_PHRASE in script.prompts[0]
    assert report["board"] is None and report["board_skipped"] == "another clone"
    assert report["manual_board_cleanup"] is None
    assert not writes(full.fake) and board["desc"] == desc
    assert not (full.paths.kankan_dir / "config.json").exists() and not full.seat_wt.exists()
    assert "another clone" in destroy.report_text(report)


def test_unreadable_manual_note(full_project):
    full = full_project
    full.fake.inject("GET", f"/1/boards/{full.cfg.board_id}$", "404", times=100)
    said = []
    report = run(full.repo, Script(destroy.LOCAL_PHRASE), said=said)
    assert report["board"] is None and report["board_skipped"] == "unreadable: 404"
    assert report["manual_board_cleanup"] == "unreadable: 404"
    text = destroy.report_text(report)
    assert "by hand" in text and "kankan-registry" in text and "by hand" in "\n".join(said)
    assert not writes(full.fake) and not (full.paths.kankan_dir / "config.json").exists()


def test_transient_board_read_changes_nothing(full_project, virtual_time):
    full = full_project
    full.fake.inject("GET", f"/1/boards/{full.cfg.board_id}$", "timeout", times=100)
    before = disk(full.paths)
    script = Script()
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == []
    assert disk(full.paths) == before


def test_full_page_of_archived_cards_asks_and_writes_nothing(full_project, monkeypatch):
    full = full_project
    original = TrelloClient.closed_cards
    monkeypatch.setattr(TrelloClient, "closed_cards", lambda self, board_id, limit=1000: original(self, board_id, limit=1))
    lst = full.fake.add_list(full.cfg.board_id, "Archive")
    full.fake.add_card(lst["id"], "old")["closed"] = True
    before = disk(full.paths)
    script = Script()
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION
    assert caught.value.message == messages.TOO_MANY_ARCHIVED_CARDS.format(limit=1)
    assert script.prompts == [] and not writes(full.fake)
    assert disk(full.paths) == before


def test_no_configuration_local_only(git_repo):
    paths = Paths.discover(git_repo)
    paths.kankan_dir.mkdir()
    (paths.kankan_dir / "stopped.json").write_text("{}")
    git(git_repo, "update-ref", "refs/agent-seats/claude-1", git(git_repo, "rev-parse", "HEAD"))
    script = Script(destroy.LOCAL_PHRASE)
    report = run(git_repo, script)
    assert report["board"] is None and report["board_skipped"] == "no configuration"
    assert report["next"] == "kankan init <board-url>"
    assert git(git_repo, "for-each-ref", "refs/agent-seats") == ""
    assert not (paths.kankan_dir / "stopped.json").exists() and not (paths.kankan_dir / "log.jsonl").exists()
    assert run(git_repo) == {"nothing_to_destroy": True}
    assert destroy.report_text({"nothing_to_destroy": True}).startswith("nothing to destroy")


# --- Report -------------------------------------------------------------------------------------------------

def test_integration_branch_report(full_project):
    full = full_project
    report = destroy_full(full)
    integration = report["integration_branch"]
    assert integration["name"] == "kankan-agents" and integration["local"] and integration["origin"] is False
    assert not integration["is_user_branch"]
    assert integration["commands"] == ["git branch -m kankan-agents kankan-agents-old"]
    text = destroy.report_text(report)
    assert "The integration branch `kankan-agents` is kept (local)" in text
    assert "The next `kankan init` will reuse it" in text
    assert "kankan-agents-2" in text and "git branch -m kankan-agents kankan-agents-old" in text


def test_integration_branch_is_user_branch(kproject):
    project = kproject
    report = run(project.paths.repo, Script(f"destroy {project.fake_board['name']}"))
    integration = report["integration_branch"]
    assert integration["name"] == "main" and integration["is_user_branch"] and integration["commands"] == []
    text = destroy.report_text(report)
    assert "git branch -m" not in text and "git push" not in text and "main-2" not in text
    assert "proposes `kankan-agents`" in text


def test_progress_every_25_cards(full_project):
    full = full_project
    for index in range(30):
        full.fake.add_card(full.cfg.backlog, f"c{index}")
    said = []
    run(full.repo, Script(board_phrase()), said=said)
    assert any("25 of" in text for text in said)


# --- Start again --------------------------------------------------------------------------------------------

def test_destroy_then_init(full_project):
    full = full_project
    cfg = full.cfg
    destroy_full(full)
    new = run_init(full.repo, full.board)
    for attr in ("backlog", "done", "blocked", "qa_list", "settings_list", "label_hold", "label_question",
                 "label_error"):
        assert getattr(new, attr) == getattr(cfg, attr), attr
    names = [c["name"] for c in full.fake.state["cards"].values()
             if c["idList"] == new.settings_list and not c["closed"]]
    assert cardsections.INSTRUCTIONS_TITLE in names and len(names) > 1
    assert (full.paths.kankan_dir / "config.json").exists()
    assert new.registry_id != cfg.registry_id


def test_unreachable_only_offers_branch(full_project):
    full = full_project
    git(full.seat_wt, "commit", "-q", "--allow-empty", "-m", "lonely")
    said = []
    script = Script(None)
    assert run(full.repo, script, said=said) == {"cancelled": True}
    text = "\n".join(said)
    assert destroy.DISCARD_PHRASE in script.prompts[0] and "lonely" in text
    wt = shlex.quote(str(full.seat_wt))
    assert f"git -C {wt} branch <name>" in text and "switch -c" not in text
    assert f"git -C {wt} log --oneline HEAD --not --branches --remotes --tags" in text


def test_missing_worktree_at_risk(full_project):
    full = full_project
    git(full.seat_wt, "commit", "-q", "--allow-empty", "-m", "lonely")
    tip = git(full.seat_wt, "rev-parse", "--short=7", "HEAD")
    shutil.rmtree(full.seat_wt)
    said = []
    assert run(full.repo, Script(None), said=said) == {"cancelled": True}
    text = "\n".join(said)
    assert f"git -C {shlex.quote(str(full.repo))} branch <name> {tip}" in text
    assert f"git -C {shlex.quote(str(full.seat_wt))}" not in text
    report = run(full.repo, Script(destroy.DISCARD_PHRASE, board_phrase()))
    assert str(full.seat_wt) in report["worktrees_removed"]
    assert not (full.paths.gcd / "worktrees" / "claude-1").exists()


def test_exclude_rewrite_keeps_other_bytes(tmp_path):
    (tmp_path / "info").mkdir()
    path = tmp_path / "info" / "exclude"
    path.write_bytes(b"# git ls-files\r\n/.kankan/\n# kankan\r\n/.kankan/\r\nbin\xff\n# kankan\n")
    os.chmod(path, 0o640)
    assert destroy._remove_exclude(path)
    assert path.read_bytes() == b"# git ls-files\r\n/.kankan/\nbin\xff\n# kankan\n"
    assert (os.stat(path).st_mode & 0o777) == 0o640
    assert not destroy._remove_exclude(path)
    assert sorted(p.name for p in path.parent.iterdir()) == ["exclude"]


def test_user_kankan_exclude_line_kept(full_project):
    full = full_project
    exclude = full.paths.gcd / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "/.kankan/\n")
    destroy_full(full)
    lines = exclude.read_text().splitlines()
    assert "/.kankan/" in lines and "# kankan" not in lines


def test_rebase_in_progress_needs_discard(full_project):
    full = full_project
    wt = full.seat_wt
    git(wt, "switch", "-q", "-c", "topic")
    (wt / "README.md").write_text("topic\n")
    git(wt, "commit", "-qam", "topic change")
    (full.repo / "README.md").write_text("main\n")
    git(full.repo, "commit", "-qam", "main change")
    assert subprocess.run(["git", "-C", str(wt), "rebase", "main"], capture_output=True).returncode != 0
    said = []
    script = Script(None)
    assert run(full.repo, script, said=said) == {"cancelled": True}
    assert destroy.DISCARD_PHRASE in script.prompts[0]
    assert f"git -C {shlex.quote(str(wt))} rebase --abort" in "\n".join(said) and not writes(full.fake)
    report = run(full.repo, Script(destroy.DISCARD_PHRASE, board_phrase()))
    assert not wt.exists() and report["state_removed"]


def test_renamed_backlog_reported(full_project):
    full = full_project
    full.fake.state["lists"][full.cfg.backlog]["name"] = "Todo"
    report = destroy_full(full)
    assert {"id": full.cfg.backlog, "name": "Todo"} in report["kept_lists"]
    text = destroy.report_text(report)
    assert "the list Todo" in text and "rename it back to Backlog" in text


def test_partial_text_lists_board_items_without_cards(full_project, monkeypatch, virtual_time):
    full = full_project
    review = full.cfg.seats["claude-1"].review
    full.fake.inject("PUT", f"/1/lists/{review}/closed$", "500", times=10)
    with pytest.raises(KankanError):
        run(full.repo, Script(board_phrase()))  # stops at the last seat list
    full.fake.faults.clear()

    def broken(ctx, target, **kwargs):
        raise KankanError(Status.NEEDS_ACTION, "simulated")
    monkeypatch.setattr(destroy, "_remove_worktree", broken)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))  # no card left: archives a list, removes the block, then stops
    assert caught.value.data["board"]["cards_deleted"] == {}
    done = caught.value.message.split(messages.DESTROY_PARTIAL_DONE)[1].split(messages.DESTROY_PARTIAL_LEFT)[0]
    assert "Board https://trello.com/b/" in done and "claude-1 · Review" in done


# --- Fix round 1 --------------------------------------------------------------------------------------------

def test_log_symlink_refused_before_questions(full_project):
    full = full_project
    target = Path.home() / ".config" / "kankan" / "agents.json"
    before = target.read_bytes()
    log = full.paths.kankan_dir / "log.jsonl"
    if os.path.lexists(log):
        log.unlink()
    log.symlink_to(target)
    script = Script()
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == []
    assert str(log) in caught.value.message
    assert target.read_bytes() == before and not writes(full.fake)


def test_log_symlink_created_during_question_refused(full_project):
    full = full_project
    target = Path.home() / ".config" / "kankan" / "agents.json"
    before = target.read_bytes()
    log = full.paths.kankan_dir / "log.jsonl"

    def plant():
        if os.path.lexists(log):
            log.unlink()
        log.symlink_to(target)
        return board_phrase()
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(plant))
    assert caught.value.status == Status.NEEDS_ACTION
    assert target.read_bytes() == before and not writes(full.fake)
    assert (full.paths.kankan_dir / "config.json").exists()


def test_auditlog_no_follow(tmp_path):
    from kankan import auditlog
    paths = Paths.discover
    kdir = tmp_path / "kankan"
    kdir.mkdir()
    target = tmp_path / "sentinel"
    target.write_bytes(b"keep")
    (kdir / "log.jsonl").symlink_to(target)

    class P:
        kankan_dir = kdir
    with pytest.raises(OSError):
        auditlog.append(P, "destroy", follow=False)
    assert target.read_bytes() == b"keep"
    os.unlink(kdir / "log.jsonl")
    os.mkfifo(kdir / "log.jsonl")
    with pytest.raises(OSError):
        auditlog.append(P, "destroy", follow=False)  # never blocks on a FIFO
    os.unlink(kdir / "log.jsonl")
    auditlog.append(P, "destroy", follow=False, n=1)
    assert b'"destroy"' in (kdir / "log.jsonl").read_bytes()
    del paths


def wrap_board_step(monkeypatch, before):
    original = destroy._board_step

    def wrapped(*args, **kwargs):
        before()
        return original(*args, **kwargs)
    monkeypatch.setattr(destroy, "_board_step", wrapped)


def test_file_created_during_board_step_stops(full_project, monkeypatch):
    full = full_project
    late = full.seat_wt / "late.txt"
    wrap_board_step(monkeypatch, lambda: late.write_text("late work"))
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    err = caught.value
    assert err.status == Status.NEEDS_ACTION and str(full.seat_wt) in err.message
    assert late.read_text() == "late work" and (full.paths.kankan_dir / "config.json").exists()
    assert err.data["board"]["registry_removed"] and str(full.seat_wt) not in err.data["worktrees_removed"]
    assert git(full.repo, "for-each-ref", "refs/agent-cards") != ""


def test_git_refuses_dirty_clean_target(full_project, monkeypatch):
    """The backstop: without the discard phrase for it, a target is removed without --force."""
    full = full_project
    late = full.seat_wt / "late.txt"
    wrap_board_step(monkeypatch, lambda: late.write_text("late work"))
    monkeypatch.setattr(destroy, "_reinspect", lambda ctx, target: target)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    assert caught.value.status == Status.NEEDS_ACTION and str(full.seat_wt) in caught.value.message
    assert late.read_text() == "late work" and (full.paths.kankan_dir / "config.json").exists()


def test_discarded_target_forced(full_project, monkeypatch):
    full = full_project
    (full.seat_wt / "new.txt").write_text("work")
    wrap_board_step(monkeypatch, lambda: (full.seat_wt / "later.txt").write_text("x"))
    report = run(full.repo, Script(destroy.DISCARD_PHRASE, board_phrase()))
    assert not full.seat_wt.exists() and report["state_removed"]


def test_new_card_during_question_stops(full_project):
    full = full_project

    def add():
        full.fake.add_card(full.cfg.backlog, "new card")
        return board_phrase()
    err = assert_needs_action_nothing_deleted(full, Script(add), messages.DESTROY_BOARD_CHANGED_AFTER)
    assert not writes(full.fake) and err.data["board"]["cards_deleted"] == {}


def test_card_moved_between_target_lists_stops(full_project):
    full = full_project
    card = next(c for c in full.fake.state["cards"].values() if c["idList"] == full.cfg.backlog)["id"]

    def move():
        full.fake.user_move(card, full.cfg.done)
        return board_phrase()
    assert_needs_action_nothing_deleted(full, Script(move), messages.DESTROY_BOARD_CHANGED_AFTER)
    assert not writes(full.fake)


def test_report_uses_fresh_inventory(full_project):
    full = full_project

    def change():
        full.fake.user_label(full.ideas_card, full.resume)
        full.fake.state["lists"][full.cfg.done]["name"] = "Finished"
        return board_phrase()
    report = run(full.repo, Script(change))
    assert full.resume in full.fake.state["labels"] and report["board"]["labels_deleted"] == []
    text = destroy.report_text(report)
    assert f"the label {RESUME}" in text and "the list Finished" in text
    assert "Finished" in report["board"]["cards_deleted"] and "Done" not in report["board"]["cards_deleted"]


def test_update_ref_oserror_stops(full_project, monkeypatch):
    full = full_project
    real = destroy.gitutil.run

    def failing(repo, args, **kwargs):
        if args[:1] == ["update-ref"]:
            raise PermissionError(13, "Permission denied")
        return real(repo, args, **kwargs)
    monkeypatch.setattr(destroy.gitutil, "run", failing)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    err = caught.value
    assert err.status == Status.NEEDS_ACTION and "Permission denied" in err.message
    assert len(err.data["worktrees_removed"]) == 2 and err.data["refs_deleted"] == 0
    assert (full.paths.kankan_dir / "config.json").exists()


def test_root_removal_oserror_stops(full_project, monkeypatch):
    full = full_project
    real = os.rmdir
    root = str(full.paths.worktree_root)

    def denied(path, *args, **kwargs):
        if str(path) == root:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(destroy.os, "rmdir", denied)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    err = caught.value
    assert err.status == Status.NEEDS_ACTION and "Permission denied" in err.message and root in err.message
    assert len(err.data["worktrees_removed"]) == 2
    assert (full.paths.kankan_dir / "config.json").exists()


def test_invalid_short_link_falls_back_to_config(full_project):
    full = full_project
    full.fake.state["boards"][full.cfg.board_id]["shortLink"] = f"{full.cfg.board_short}/{SLUG}"
    said = []
    report = run(full.repo, Script(board_phrase()), said=said)
    assert all(SLUG not in text for text in said) and SLUG not in repr(report)
    assert report["board"]["url"] == f"https://trello.com/b/{full.cfg.board_short}"


def test_invalid_short_link_everywhere_needs_action(full_project):
    from kankan.config import save_config
    full = full_project
    full.fake.state["boards"][full.cfg.board_id]["shortLink"] = f"x/{SLUG}"
    cfg = load_config(full.paths)
    cfg.board_short = f"y/{SLUG}"
    save_config(full.paths, cfg)
    said = []
    script = Script()
    with pytest.raises(KankanError) as caught:
        run(full.repo, script, said=said)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == [] and said == []
    assert SLUG not in caught.value.message


def test_empty_plan_validates_confirm(git_repo):
    for confirm in (["anything"], [destroy.LOCAL_PHRASE], [destroy.LOCAL_PHRASE, destroy.LOCAL_PHRASE]):
        with pytest.raises(KankanError) as caught:
            run(git_repo, confirm=confirm)
        assert caught.value.status == Status.USAGE_ERROR
    assert run(git_repo) == {"nothing_to_destroy": True}


def test_named_locks_kept_with_inodes(full_project):
    full = full_project
    kdir = full.paths.kankan_dir
    names = ["project.lock", "up.lock", "seats/claude-1.lock", "seats/claude-1.state.lock",
             "seats/claude-1.block.lock", f"attachment-locks/{SHORT}.lock", "qa/conv1/op.lock"]
    for name in names:
        (kdir / name).parent.mkdir(parents=True, exist_ok=True)
        (kdir / name).touch()
    (kdir / "qa" / "conv1" / "turn.json").write_text("{}")
    names += sorted({p.name for p in kdir.glob("*.lock")} - set(names))  # those init itself created
    before = {name: os.stat(kdir / name).st_ino for name in names}
    destroy_full(full)
    assert {name: os.stat(kdir / name).st_ino for name in names if (kdir / name).exists()} == before
    assert not (kdir / "qa" / "conv1" / "turn.json").exists()
    assert sorted(str(p.relative_to(kdir)) for p in kdir.rglob("*") if p.is_file()) == sorted(names)


# --- Fix round 2 --------------------------------------------------------------------------------------------

def test_root_removal_failure_left_names_root(full_project, monkeypatch):
    full = full_project
    real = os.rmdir
    root = str(full.paths.worktree_root)

    def denied(path, *args, **kwargs):
        if str(path) == root:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(destroy.os, "rmdir", denied)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    assert any(root in item for item in caught.value.data["left"])


@pytest.mark.parametrize("where", ["trello", "config"])
def test_short_link_with_trailing_newline_rejected(full_project, where):
    from kankan.config import save_config
    full = full_project
    bad = full.cfg.board_short + "\n"
    if where == "trello":
        full.fake.state["boards"][full.cfg.board_id]["shortLink"] = bad
        said = []
        report = run(full.repo, Script(board_phrase()), said=said)
        assert report["board"]["url"] == f"https://trello.com/b/{full.cfg.board_short}"
        lines = [line for text in said for line in text.split("\n")]
        assert f"Board https://trello.com/b/{full.cfg.board_short}" in lines
        assert "" not in lines[:3]  # no line break carried by the short link
    else:
        full.fake.state["boards"][full.cfg.board_id]["shortLink"] = "x/" + SLUG
        cfg = load_config(full.paths)
        cfg.board_short = bad
        save_config(full.paths, cfg)
        with pytest.raises(KankanError) as caught:
            run(full.repo, Script())
        assert caught.value.status == Status.NEEDS_ACTION
    assert not destroy._valid_short(bad) and destroy._url(bad) == "<board-url>"


def test_reopened_seat_list_stops(full_project):
    full = full_project
    review = full.cfg.seats["claude-1"].review
    full.fake.state["lists"][review]["closed"] = True

    def reopen():
        full.fake.state["lists"][review]["closed"] = False
        return board_phrase()
    assert_needs_action_nothing_deleted(full, Script(reopen), messages.DESTROY_BOARD_CHANGED_AFTER)
    assert not writes(full.fake)


def test_reinspection_launch_failure_keeps_partial_report(full_project, monkeypatch):
    full = full_project
    real = destroy.gitutil.run_bytes

    def failing(repo, args, **kwargs):
        if args[:1] == ["symbolic-ref"]:
            raise OSError(24, "Too many open files")
        return real(repo, args, **kwargs)
    monkeypatch.setattr(destroy.gitutil, "run_bytes", failing)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    err = caught.value
    assert err.status == Status.NEEDS_ACTION and "Too many open files" in err.message
    assert err.data["board"]["registry_removed"] and sum(err.data["board"]["cards_deleted"].values()) > 0
    assert any("worktrees to remove" in item for item in err.data["left"])
    assert (full.paths.kankan_dir / "config.json").exists()


def add_submodule(full, tmp_path):
    sub = tmp_path / "subrepo"
    sub.mkdir()
    git(sub, "init", "-q", "-b", "main")
    (sub / "f").write_text("x")
    git(sub, "add", "f")
    git(sub, "commit", "-qm", "sub")
    wt = full.seat_wt
    git(wt, "switch", "-q", "-c", "with-sub")
    git(wt, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
    git(wt, "commit", "-qm", "add submodule")
    assert git(wt, "status", "--porcelain") == ""


def test_submodule_worktree_needs_discard(full_project, tmp_path):
    full = full_project
    add_submodule(full, tmp_path)
    ctx = destroy.Ctx.open(full.repo)
    plan = destroy.local_plan(ctx, destroy.known_seats(ctx))
    target = next(t for t in plan.worktrees if t.path == full.seat_wt)
    assert target.submodules == ["vendor/sub"] and target.at_risk
    said = []
    script = Script(None)
    assert run(full.repo, script, said=said) == {"cancelled": True}
    text = "\n".join(said)
    assert destroy.DISCARD_PHRASE in script.prompts[0] and "vendor/sub" in text
    assert f"git -C {shlex.quote(str(full.seat_wt))} submodule status" in text
    assert not writes(full.fake) and full.seat_wt.exists()
    report = run(full.repo, Script(destroy.DISCARD_PHRASE, board_phrase()))
    assert not full.seat_wt.exists() and report["state_removed"]


def test_log_hard_link_refused(full_project):
    full = full_project
    target = Path.home() / ".config" / "kankan" / "agents.json"
    before = target.read_bytes()
    log = full.paths.kankan_dir / "log.jsonl"
    if os.path.lexists(log):
        log.unlink()
    os.link(target, log)
    script = Script()
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == []
    assert target.read_bytes() == before and not writes(full.fake)


def test_auditlog_no_follow_refuses_hard_link(tmp_path):
    from kankan import auditlog
    kdir = tmp_path / "kankan"
    kdir.mkdir()
    target = tmp_path / "sentinel"
    target.write_bytes(b"keep")
    os.link(target, kdir / "log.jsonl")

    class P:
        kankan_dir = kdir
    with pytest.raises(OSError):
        auditlog.append(P, "destroy", follow=False)
    assert target.read_bytes() == b"keep"


# --- Fix round 3 --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [SHORT + "\n", "not-a-short-link"])
def test_block_snapshot_with_invalid_name_needs_discard(full_project, name):
    full = full_project
    work = full.paths.kankan_dir / "blocks" / name / "files" / "work.txt"
    work.parent.mkdir(parents=True)
    work.write_text("only copy")
    said = []
    script = Script(None)
    assert run(full.repo, script, said=said) == {"cancelled": True}
    assert destroy.DISCARD_PHRASE in script.prompts[0]
    assert repr(name) in "\n".join(said) and "work.txt" in "\n".join(said)
    assert work.read_text() == "only copy" and not writes(full.fake)
    with pytest.raises(KankanError) as caught:
        run(full.repo, confirm=[board_phrase()])  # no TTY: the discard phrase is missing
    assert caught.value.status == Status.USAGE_ERROR and work.read_text() == "only copy" and not writes(full.fake)
    report = run(full.repo, Script(destroy.DISCARD_PHRASE, board_phrase()))
    assert report["state_removed"] and not work.exists()


# --- Fix round 4 --------------------------------------------------------------------------------------------

def test_block_snapshot_files_link_needs_discard_and_is_not_followed(full_project, tmp_path):
    full = full_project
    outside = tmp_path / "outside"
    (outside / "locked").mkdir(parents=True)
    (outside / "locked" / "l.txt").write_text("l")
    (outside / "keep.txt").write_text("k")
    os.chmod(outside / "locked", 0)
    try:
        snap = full.paths.kankan_dir / "blocks" / SHORT
        snap.mkdir(parents=True)
        os.symlink(outside, snap / "files")
        said = []
        script = Script(None)
        assert run(full.repo, script, said=said) == {"cancelled": True}
        assert destroy.DISCARD_PHRASE in script.prompts[0]
        text = "\n".join(said)
        assert str(snap) in text and messages.DESTROY_RISK_BLOCK_FILES_LINK in text and "keep.txt" not in text
        assert not writes(full.fake)
        report = run(full.repo, Script(destroy.DISCARD_PHRASE, board_phrase()))
        assert report["state_removed"] and not os.path.lexists(snap / "files")
        assert (outside / "keep.txt").read_text() == "k"
    finally:
        os.chmod(outside / "locked", 0o700)
    assert (outside / "locked" / "l.txt").read_text() == "l"
