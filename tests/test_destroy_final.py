"""``kankan destroy``: regressions of the final whole-branch review (destroy spec F§5, T§2–§7)."""

import json
import os
import shutil
import subprocess

import pytest

from kankan import cli, config, destroy, init, messages, procutil, seats
from kankan.config import Paths, SeatConfig, load_config, save_config, write_registry_block
from kankan.errors import KankanError, Status
from kankan.project import Project
from kankan.refstore import RefStore, seat_ref
from kankan.trello import TrelloClient
from tests.conftest import git
from tests.fakes.fake_trello import KEY, TOKEN
from tests.helpers import NoTmux, hold
from tests.test_destroy_run import (NAME, Script, board_phrase, disk, full_project, run, target_lists,  # noqa: F401
                                    writes)

SHORT = "Ab12Cd34"


def plan_of(repo):
    ctx = destroy.Ctx.open(repo)
    return destroy.local_plan(ctx, destroy.known_seats(ctx))


def set_config(paths, **changes):
    cfg = load_config(paths)
    for key, value in changes.items():
        setattr(cfg, key, value)
    save_config(paths, cfg)


def raw_config(paths, **changes):
    path = paths.kankan_dir / "config.json"
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


# --- 1. A custom worktree root overlapping the state directory ----------------------------------------------

@pytest.mark.parametrize("where", ["inside", "equal", "around"])
def test_root_overlapping_state_refused(full_project, where):
    full = full_project
    kdir = full.paths.kankan_dir
    root = {"inside": kdir / "wt", "equal": kdir, "around": full.paths.gcd}[where]
    sentinel_dir = kdir / "wt" / "mine"
    sentinel_dir.mkdir(parents=True)
    (sentinel_dir / "sentinel").write_text("keep")
    set_config(full.paths, worktree_root=str(root))
    before = (repr(full.fake.state), disk(full.paths))
    script = Script(board_phrase())
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and "overlap" in caught.value.message
    assert script.prompts == []
    assert (repr(full.fake.state), disk(full.paths)) == before
    assert (sentinel_dir / "sentinel").read_text() == "keep"


def test_worktree_left_inside_state_refused_after_root_moved(full_project, tmp_path):
    """Following the refusal by only pointing worktree_root elsewhere must not turn the worktrees left inside
    <gcd>/kankan into plain state: any registered worktree there is refused too."""
    full = full_project
    kdir = full.paths.kankan_dir
    inside = kdir / "wt" / "claude-1"
    inside.parent.mkdir(parents=True)
    git(full.repo, "worktree", "move", str(full.seat_wt), str(inside))
    (inside / "dirty.txt").write_text("work")
    set_config(full.paths, worktree_root=str(kdir / "wt"))
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    assert "git worktree move" in caught.value.message
    set_config(full.paths, worktree_root=str(tmp_path / "outside"))
    before = (repr(full.fake.state), disk(full.paths))
    script = Script(destroy.DISCARD_PHRASE, board_phrase())
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == []
    assert (repr(full.fake.state), disk(full.paths)) == before
    assert (inside / "dirty.txt").read_text() == "work"


# --- 2. A missing checkout keeps recoverable work in its administrative directory --------------------------

def missing_seat(full):
    wt = full.seat_wt
    shutil.rmtree(wt)
    return full.paths.gcd / "worktrees" / wt.name


def test_missing_clean_checkout_not_at_risk(full_project):
    full = full_project
    missing_seat(full)
    before = disk(full.paths)
    plan = plan_of(full.repo)
    target = next(t for t in plan.worktrees if t.path == full.seat_wt)
    assert target.missing and not target.at_risk
    assert disk(full.paths) == before  # the index inspection rewrote nothing


def test_missing_checkout_with_staged_file_at_risk(full_project):
    full = full_project
    (full.seat_wt / "staged.txt").write_text("work")
    git(full.seat_wt, "add", "staged.txt")
    admin = missing_seat(full)
    before = disk(full.paths)
    plan = plan_of(full.repo)
    assert disk(full.paths) == before
    target = next(t for t in plan.worktrees if t.path == full.seat_wt)
    assert target.missing and target.at_risk and target.dirty == ["staged.txt"]
    said = []
    assert run(full.repo, Script(None), said=said) == {"cancelled": True}
    text = "\n".join(said)
    assert "staged.txt" in text and str(admin / "index") in text
    with pytest.raises(KankanError) as caught:
        run(full.repo, confirm=[board_phrase()])
    assert caught.value.status == Status.USAGE_ERROR and admin.is_dir()
    report = run(full.repo, confirm=[destroy.DISCARD_PHRASE, board_phrase()])
    assert str(full.seat_wt) in report["worktrees_removed"] and not admin.exists()


def test_missing_checkout_with_submodule_storage_at_risk(full_project):
    full = full_project
    admin = missing_seat(full)
    (admin / "modules" / "lib").mkdir(parents=True)
    target = next(t for t in plan_of(full.repo).worktrees if t.path == full.seat_wt)
    assert target.at_risk and target.submodules == [str(admin / "modules")]
    said = []
    run(full.repo, Script(None), said=said)
    assert str(admin / "modules") in "\n".join(said)


def test_missing_checkout_uninspectable_index_needs_action(full_project, monkeypatch):
    full = full_project
    (full.seat_wt / "staged.txt").write_text("work")
    git(full.seat_wt, "add", "staged.txt")
    missing_seat(full)
    real = destroy.gitutil.run_bytes

    def broken(cwd, args, **kwargs):
        if "diff-index" in args:
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: index file corrupt")
        return real(cwd, args, **kwargs)

    monkeypatch.setattr(destroy.gitutil, "run_bytes", broken)
    with pytest.raises(KankanError) as caught:
        plan_of(full.repo)
    assert caught.value.status == Status.NEEDS_ACTION


def test_missing_checkout_unreadable_modules_needs_action(full_project, monkeypatch):
    full = full_project
    admin = missing_seat(full)
    (admin / "modules").mkdir()
    real = os.listdir

    def denied(path, *args):
        if str(path).endswith("modules"):
            raise PermissionError(13, "denied", str(path))
        return real(path, *args)

    monkeypatch.setattr(os, "listdir", denied)
    with pytest.raises(KankanError) as caught:
        plan_of(full.repo)
    assert caught.value.status == Status.NEEDS_ACTION


# --- 3. The configuration is reloaded under the project lock ------------------------------------------------

def during_prompt(action, phrase):
    def reply():
        action()
        return phrase
    return reply


def test_seat_created_during_prompt_stops(full_project):
    full = full_project

    def new_seat():
        project = Project.open(full.repo)
        seats.ensure_created(project, "claude-2", "claude")

    before_cards = set(full.fake.state["cards"])
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(during_prompt(new_seat, board_phrase())))
    assert caught.value.status == Status.NEEDS_ACTION
    assert set(full.fake.state["cards"]) == before_cards
    assert (full.paths.kankan_dir / "config.json").exists() and full.seat_wt.exists()
    assert not [w for w in writes(full.fake) if w[0] == "DELETE"]


def test_config_created_during_local_only_prompt_stops(git_repo, fake_trello, monkeypatch):
    paths = Paths.discover(git_repo)
    paths.kankan_dir.mkdir(parents=True)
    (paths.kankan_dir / "stale.json").write_text("{}")

    def bind():
        from tests.helpers import bind_project
        bind_project(git_repo, fake_trello, monkeypatch)

    with pytest.raises(KankanError) as caught:
        run(git_repo, Script(during_prompt(bind, destroy.LOCAL_PHRASE)))
    assert caught.value.status == Status.NEEDS_ACTION
    assert (paths.kankan_dir / "config.json").exists() and (paths.kankan_dir / "stale.json").exists()


def test_binding_changed_during_prompt_stops(full_project):
    full = full_project
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(during_prompt(lambda: set_config(full.paths, registry_id="other"), board_phrase())))
    assert caught.value.status == Status.NEEDS_ACTION and not writes(full.fake)


def test_target_list_changed_during_prompt_stops(full_project):
    full = full_project
    other = full.fake.add_list(full.cfg.board_id, "Backlog 2")["id"]
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(during_prompt(lambda: set_config(full.paths, backlog=other), board_phrase())))
    assert caught.value.status == Status.NEEDS_ACTION and not writes(full.fake)


def test_new_ref_during_prompt_stops(full_project):
    full = full_project

    def new_ref():
        project = Project.open(full.repo)
        project.refs.create(seat_ref("claude-9"), project.refs.write_blob({"schema": 1, "seat": "claude-9"}))

    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(during_prompt(new_ref, board_phrase())))
    assert caught.value.status == Status.NEEDS_ACTION and not writes(full.fake)


def test_new_snapshot_after_discard_stops(full_project):
    full = full_project
    (full.seat_wt / "new.txt").write_text("work")

    def snapshot():
        files = full.paths.kankan_dir / "blocks" / SHORT / "files"
        files.mkdir(parents=True)
        (files / "only-copy.txt").write_text("w")

    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(destroy.DISCARD_PHRASE, during_prompt(snapshot, board_phrase())))
    assert caught.value.status == Status.NEEDS_ACTION and not writes(full.fake)
    assert (full.paths.kankan_dir / "blocks" / SHORT / "files" / "only-copy.txt").exists()


def test_changed_snapshot_after_discard_stops(full_project):
    full = full_project
    files = full.paths.kankan_dir / "blocks" / SHORT / "files"
    files.mkdir(parents=True)
    (files / "a.txt").write_text("a")

    def more():
        (files / "b.txt").write_text("b")

    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(destroy.DISCARD_PHRASE, during_prompt(more, board_phrase())))
    assert caught.value.status == Status.NEEDS_ACTION and not writes(full.fake)
    assert (files / "b.txt").exists()


# --- 4. The board deletion loop re-checks each object -------------------------------------------------------

def other_target_card(full, current):
    targets = target_lists(full.cfg)
    return next(card_id for card_id, card in full.fake.state["cards"].items()
                if card["idList"] in targets and card_id != current)


def test_card_moved_out_during_deletion_is_skipped(full_project, monkeypatch):
    full = full_project
    real = TrelloClient.delete_card
    moved = []

    def delete(self, card_id):
        if not moved:
            moved.append(other_target_card(full, card_id))
            full.fake.user_move(moved[0], full.ideas)
        return real(self, card_id)

    monkeypatch.setattr(TrelloClient, "delete_card", delete)
    report = run(full.repo, Script(board_phrase()))
    assert moved[0] in full.fake.state["cards"]
    assert report["board"]["cards_skipped"] == 1 and report["state_removed"]
    assert ("GET", f"/1/cards/{moved[0]}") in full.fake.requests
    assert "1 card" in destroy.report_text(report)


def test_resume_label_gains_outside_user_during_deletion(full_project, monkeypatch):
    full = full_project
    real = TrelloClient.archive_list

    def archive(self, list_id):
        full.fake.user_label(full.ideas_card, full.resume)
        return real(self, list_id)

    monkeypatch.setattr(TrelloClient, "archive_list", archive)
    report = run(full.repo, Script(board_phrase()))
    assert full.resume in full.fake.state["labels"]
    assert report["board"]["labels_deleted"] == [] and report["board"]["labels_kept"]


def test_card_added_during_deletion_keeps_registry_and_config(full_project, monkeypatch):
    full = full_project
    real = TrelloClient.archive_list
    added = []

    def archive(self, list_id):
        if not added:
            added.append(full.fake.add_card(full.cfg.backlog, "late")["id"])
        return real(self, list_id)

    monkeypatch.setattr(TrelloClient, "archive_list", archive)
    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(board_phrase()))
    assert caught.value.status == Status.NEEDS_ACTION
    assert added[0] in full.fake.state["cards"]
    assert config.REGISTRY_START in full.fake.state["boards"][full.cfg.board_id]["desc"]
    assert (full.paths.kankan_dir / "config.json").exists() and full.seat_wt.exists()
    report = run(full.repo, Script(board_phrase()))
    assert added[0] not in full.fake.state["cards"] and report["state_removed"]


def test_card_place_read(fake_trello):
    board = fake_trello.add_board("b")
    lst = fake_trello.add_list(board["id"], "L")["id"]
    card = fake_trello.add_card(lst, "c")["id"]
    client = TrelloClient(KEY, TOKEN, base_url=fake_trello.url)
    assert client.card_place(card) == (lst, False)
    fake_trello.user_delete(card)
    assert client.card_place(card) is None


# --- 5. Contradictory ownership data ------------------------------------------------------------------------

def test_registry_with_two_ids_is_malformed():
    block = f"{config.REGISTRY_START}\nregistry-id: other\nregistry-id: local\n{config.REGISTRY_END}"
    assert config.registry_state(block) == ("malformed", None)
    same = f"{config.REGISTRY_START}\nregistry-id: local\nregistry-id: local\n{config.REGISTRY_END}"
    assert config.registry_state(same) == ("malformed", None)


def test_closed_cards_conflicting_rows(fake_trello, monkeypatch):
    client = TrelloClient(KEY, TOKEN, base_url=fake_trello.url)
    rows = [{"id": "c1", "idList": "outside", "idLabels": []}, {"id": "c1", "idList": "target", "idLabels": []}]
    monkeypatch.setattr(client, "_ok", lambda *args, **kwargs: rows)
    with pytest.raises(KankanError) as caught:
        client.closed_cards("b")
    assert caught.value.status == Status.TRELLO_ERROR
    rows[:] = [{"id": "c1", "idList": "t", "idLabels": ["a", "b"]}, {"id": "c1", "idList": "t", "idLabels": ["a", "b"]}]
    assert [card.id for card in client.closed_cards("b")] == ["c1"]


def test_conflicting_archived_rows_refuse_before_question(full_project, monkeypatch):
    full = full_project
    real = TrelloClient._ok

    def doubled(self, method, path, *args, **kwargs):
        result = real(self, method, path, *args, **kwargs)
        if path.endswith("/cards/closed") and result:
            result = result + [dict(result[0], idList=full.ideas)]
        return result

    monkeypatch.setattr(TrelloClient, "_ok", doubled)
    script = Script(board_phrase())
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == [] and not writes(full.fake)
    assert caught.value.message == messages.DESTROY_BOARD_CHANGED


# --- 6. A hook in a shared hooks directory ------------------------------------------------------------------

def global_hooks(tmp_path, monkeypatch):
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    (shared / "pre-push").write_text(init.hook_content())
    (shared / "pre-push").chmod(0o755)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(f"[core]\n\thooksPath = {shared}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    return shared / "pre-push"


def test_shared_hook_kept(full_project, tmp_path, monkeypatch):
    full = full_project
    hook = global_hooks(tmp_path, monkeypatch)
    plan = plan_of(full.repo)
    assert plan.hook == "shared"
    report = run(full.repo, Script(board_phrase()))
    assert hook.read_text() == init.hook_content()
    assert report["hook"] == "shared, kept"
    assert str(hook) in destroy.report_text(report)


def test_shared_hook_alone_is_nothing_to_destroy(git_repo, tmp_path, monkeypatch):
    hook = global_hooks(tmp_path, monkeypatch)
    assert run(git_repo, Script()) == {"nothing_to_destroy": True}
    assert hook.exists()


def test_local_hooks_path_inside_repository_owned(kproject):
    repo = kproject.paths.repo
    hooks = repo / ".githooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text(init.hook_content())
    git(repo, "config", "--local", "core.hooksPath", ".githooks")
    assert plan_of(repo).hook == "kankan"


def test_local_hooks_path_outside_repository_shared(kproject, tmp_path):
    repo = kproject.paths.repo
    hooks = tmp_path / "elsewhere"
    hooks.mkdir()
    (hooks / "pre-push").write_text(init.hook_content())
    git(repo, "config", "--local", "core.hooksPath", str(hooks))
    assert plan_of(repo).hook == "shared"


# --- 7. No-follow ---------------------------------------------------------------------------------------------

def test_state_root_symlink_config_never_read(git_repo, tmp_path, monkeypatch):
    import builtins
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.json").write_text("{}")
    kdir = Paths.discover(git_repo).kankan_dir
    os.symlink(outside, kdir)
    opened = []
    real = builtins.open

    def recorder(path, *args, **kwargs):
        if str(path).startswith((str(outside), str(kdir) + os.sep)):
            opened.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recorder)
    ctx = destroy.Ctx.open(git_repo)
    assert ctx.cfg is None and "symbolic link" in ctx.reason
    with pytest.raises(KankanError) as caught:
        run(git_repo, Script())
    monkeypatch.undo()
    assert caught.value.status == Status.NEEDS_ACTION and opened == []


@pytest.mark.parametrize("name", ["project.lock", "log.jsonl", "seats/claude-1.lock", "seats/claude-1.proc.json"])
def test_unsafe_state_file_refused_before_question(full_project, tmp_path, name):
    full = full_project
    path = full.paths.kankan_dir / name
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere.file"
    os.symlink(target, path)
    script = Script(board_phrase())
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == []
    assert not target.exists() and not writes(full.fake)


def test_project_lock_link_created_during_prompt(full_project, tmp_path):
    full = full_project
    lock = full.paths.kankan_dir / "project.lock"
    target = tmp_path / "elsewhere.lock"

    def swap():
        if lock.exists():
            lock.unlink()
        os.symlink(target, lock)

    with pytest.raises(KankanError) as caught:
        run(full.repo, Script(during_prompt(swap, board_phrase())))
    assert caught.value.status == Status.NEEDS_ACTION
    assert not target.exists() and not writes(full.fake)


def test_snapshot_files_listed_through_descriptors(full_project, monkeypatch):
    full = full_project
    files = full.paths.kankan_dir / "blocks" / SHORT / "files"
    (files / "dir").mkdir(parents=True)
    (files / "dir" / "a.txt").write_text("a")
    os.symlink("/etc", files / "link")
    walked = []
    real = os.walk
    monkeypatch.setattr(os, "walk", lambda top, *a, **k: walked.append(str(top)) or real(top, *a, **k))
    plan = plan_of(full.repo)
    block = next(b for b in plan.blocks if b.short == SHORT)
    assert block.files == ["dir/a.txt", "link"]
    assert not any(str(files) in top for top in walked)


def test_snapshot_files_link_never_followed(full_project, tmp_path):
    full = full_project
    snap = full.paths.kankan_dir / "blocks" / SHORT
    snap.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    os.symlink(outside, snap / "files")
    block = next(b for b in plan_of(full.repo).blocks if b.short == SHORT)
    assert block.files == [] and block.files_link


# --- 8. The remote probe cannot touch credentials -----------------------------------------------------------

def test_remote_probe_disables_credential_helpers(git_repo, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(git_repo, "remote", "add", "origin", str(remote))
    calls = []
    real = destroy.gitutil.run

    def spy(repo, args, **kwargs):
        if "ls-remote" in args:
            calls.append((args, kwargs.get("env")))
        return real(repo, args, **kwargs)

    monkeypatch.setattr(destroy.gitutil, "run", spy)
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")
    assert destroy._origin_has(git_repo, "refs/heads/kankan-agents") is False
    (args, env), = calls
    assert args[:2] == ["-c", "credential.helper="]
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ASKPASS"] == "/bin/false"
    assert env["SSH_ASKPASS"] == "/bin/false" and "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert env["GIT_SSH_COMMAND"].startswith("ssh -i key")


def test_remote_probe_helper_never_runs(git_repo, tmp_path):
    """An HTTP remote asking for credentials: a configured helper would write a sentinel; it never runs."""
    import http.server
    import threading

    class Refuse(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Refuse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sentinel = tmp_path / "helper-ran"
        helper = tmp_path / "helper.sh"
        helper.write_text(f"#!/bin/sh\necho \"$1\" >> '{sentinel}'\necho username=u\necho password=p\n")
        helper.chmod(0o755)
        url = f"http://127.0.0.1:{server.server_port}"
        git(git_repo, "config", "credential.helper", str(helper))
        git(git_repo, "config", f"credential.{url}.helper", str(helper))  # a URL-specific helper too
        git(git_repo, "remote", "add", "origin", f"{url}/repo.git")
        unguarded = subprocess.run(["git", "-C", str(git_repo), "ls-remote", "origin"], capture_output=True,
                                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=20)
        assert unguarded.returncode != 0 and sentinel.exists()  # the helper runs without the guard
        sentinel.unlink()
        assert destroy._origin_has(git_repo, "refs/heads/kankan-agents") == "unknown"
        assert not sentinel.exists()
    finally:
        server.shutdown()


# --- 9. CLI usage errors never echo the arguments -----------------------------------------------------------

@pytest.mark.parametrize("argv", [["destroy", "--confirm", "destroy", "Sample", "Board"],
                                  ["destroy", "--confirm"], ["destroy", "--confrim=Sample Board"]])
@pytest.mark.parametrize("as_json", [False, True])
def test_destroy_usage_error_fixed_message(git_repo, monkeypatch, capsys, argv, as_json):
    monkeypatch.chdir(git_repo)
    code = cli.main(argv + (["--json"] if as_json else []))
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code == Status.USAGE_ERROR.exit_code
    assert "Sample" not in text and "Board" not in text and "confrim" not in text
    assert messages.DESTROY_USAGE in text
    if as_json:
        assert json.loads(captured.out)["status"] == "usage_error"


# --- 10. List IDs validated by try_load ---------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [("backlog", []), ("done", 3), ("blocked", {}), ("qa_list", 1),
                                         ("settings_list", ["x"])])
def test_try_load_rejects_bad_list_ids(kproject, field, value):
    raw_config(kproject.paths, **{field: value})
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and field in ctx.reason


@pytest.mark.parametrize("key,value", [("in_progress", []), ("implemented", 2), ("review", {}), ("role", 5)])
def test_try_load_rejects_bad_seat_list_ids(kproject, key, value):
    raw_config(kproject.paths, seats={"claude-1": {"type": "claude", key: value}})
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and key in ctx.reason


def test_seat_with_null_role_keeps_its_lists_targeted(full_project):
    full = full_project
    seat = full.cfg.seats["claude-1"]
    data = json.loads((full.paths.kankan_dir / "config.json").read_text())
    data["seats"]["claude-1"]["role"] = None
    raw_config(full.paths, seats=data["seats"])
    board, _ = destroy.board_plan(destroy.Ctx.open(full.repo))
    assert seat.in_progress in board.cards and seat.in_progress in board.seat_lists


def test_bad_backlog_is_local_only(kproject):
    raw_config(kproject.paths, backlog=[])
    said = []
    assert run(kproject.paths.repo, Script(None), said=said) == {"cancelled": True}
    assert messages.DESTROY_SKIP_NO_CONFIG in "\n".join(said)


# --- 11. Minors ---------------------------------------------------------------------------------------------

def test_gate_seat_outside_config_gets_concrete_remedy(kproject):
    me = os.getpid()
    path = kproject.paths.seats_dir / "claude-7.proc.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"boot_id": procutil.boot_id(), "agent": {"pid": me, "start": procutil.start_time(me)}}))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.usable and "claude-7" not in ctx.cfg.seats
    entries = destroy.gate(ctx, NoTmux())
    assert entries[0]["remedy"] == f"kill {me}"


def test_refstore_delete_exact(kproject):
    refs = kproject.refs
    oid = refs.write_blob({"schema": 1, "seat": "claude-1"})
    refs.create(seat_ref("claude-1"), oid)
    other = refs.write_blob({"schema": 1, "seat": "claude-2"})
    assert refs.delete_exact({seat_ref("claude-1"): other}) is not None
    assert refs.read(seat_ref("claude-1")) is not None
    assert refs.delete_exact({seat_ref("claude-1"): oid}) is None
    assert refs.read(seat_ref("claude-1")) is None
    with pytest.raises(KankanError):
        refs.delete_exact({"refs/heads/main": oid})


def test_destroy_has_no_ref_transaction_of_its_own():
    import inspect
    assert "update-ref" not in inspect.getsource(destroy)


def test_seats_directory_link_refused_before_question(full_project, tmp_path):
    full = full_project
    seats_dir = full.paths.seats_dir
    elsewhere = tmp_path / "other-seats"
    if seats_dir.exists():
        shutil.move(str(seats_dir), str(elsewhere))
    else:
        elsewhere.mkdir()
    os.symlink(elsewhere, seats_dir)
    script = Script(board_phrase())
    with pytest.raises(KankanError) as caught:
        run(full.repo, script)
    assert caught.value.status == Status.NEEDS_ACTION and script.prompts == [] and not writes(full.fake)
