import multiprocessing
import os
import time

import pytest

from kankan import auditlog, faults
from kankan.config import (Config, Paths, SeatConfig, atomic_write_json, check_lists, check_registry,
                           load_config, load_credentials, project_lock, read_json, registry_block, save_config,
                           write_registry_block)
from kankan.errors import KankanError, Status
from kankan.properties import PropertiesFile
from kankan.trello import TList


def make_cfg(**kw):
    base = dict(board_id="b" * 24, board_short="Ab12Cd34", registry_id="reg1", backlog="1" * 24, done="2" * 24,
                blocked="3" * 24, label_hold="4" * 24, label_question="5" * 24, label_error="6" * 24,
                notify_id="7" * 24, notify_username="me", test_command="")
    base.update(kw)
    return Config(**base)


def test_paths_discover_from_worktree(git_repo, tmp_path):
    paths = Paths.discover(git_repo)
    assert paths.project == "proj"
    assert paths.repo == git_repo
    assert paths.gcd == git_repo / ".git"
    assert paths.kankan_dir == git_repo / ".git" / "kankan"
    assert paths.worktree_root == tmp_path / "home" / "coding" / ".kankan" / "proj"
    wt = tmp_path / "wt"
    os.system(f"git -C {git_repo} worktree add -q --detach {wt}")
    assert Paths.discover(wt).repo == git_repo


def test_config_roundtrip(git_repo):
    paths = Paths.discover(git_repo)
    with pytest.raises(KankanError) as exc:
        load_config(paths)
    assert exc.value.status == Status.CONFIG_ERROR
    cfg = make_cfg(seats={"claude-1": SeatConfig("claude", "a" * 24, "b" * 24, "c" * 24)})
    save_config(paths, cfg)
    assert load_config(paths) == cfg


def test_credentials_lookup_order(git_repo, tmp_path, monkeypatch, isolated_home):
    paths = Paths.discover(git_repo)
    user = isolated_home / ".config" / "kankan" / "credentials"
    user.parent.mkdir(parents=True)
    user.write_text("kankan.trello.key=ukey\nkankan.trello.token=utok\n")
    os.chmod(user, 0o600)
    assert load_credentials(paths, make_cfg()) == ("ukey", "utok")
    props = tmp_path / "proj.properties"
    props.write_text("kankan.trello.key=pkey\nkankan.trello.token=ptok\n")
    assert load_credentials(paths, make_cfg(credentials_path=str(props))) == ("pkey", "ptok")
    monkeypatch.setenv("KANKAN_TRELLO_KEY", "ekey")
    monkeypatch.setenv("KANKAN_TRELLO_TOKEN", "etok")
    assert load_credentials(paths, make_cfg(credentials_path=str(props))) == ("ekey", "etok")
    monkeypatch.delenv("KANKAN_TRELLO_KEY")
    user.unlink()
    with pytest.raises(KankanError) as exc:
        load_credentials(paths, make_cfg())
    assert exc.value.status == Status.CONFIG_ERROR


def test_registry_block_roundtrip():
    desc = write_registry_block("Free text\n", {"registry-id": "abc", "host": "h", "path": "/p"})
    assert desc.startswith("Free text\n")
    assert registry_block(desc) == {"registry-id": "abc", "host": "h", "path": "/p"}
    again = write_registry_block(desc + "more\n", {"registry-id": "def", "host": "h", "path": "/p"})
    assert again.count("--- kankan-registry ---") == 1 and "more" in again and "Free text" in again
    assert registry_block(again)["registry-id"] == "def"
    assert registry_block("nothing") is None


def test_check_registry_mismatch_config_error():
    cfg = make_cfg()
    check_registry(cfg, write_registry_block("", {"registry-id": "reg1", "host": "h", "path": "/p"}))
    for desc in ("", write_registry_block("", {"registry-id": "other", "host": "h", "path": "/p"})):
        with pytest.raises(KankanError) as exc:
            check_registry(cfg, desc)
        assert exc.value.status == Status.CONFIG_ERROR


def test_check_lists_archived_config_error():
    cfg = make_cfg()
    lists = {i: TList(i, "x", False, 1) for i in (cfg.backlog, cfg.done, cfg.blocked)}
    check_lists(cfg, lists)
    lists[cfg.done] = TList(cfg.done, "Done", True, 1)
    with pytest.raises(KankanError) as exc:
        check_lists(cfg, lists)
    assert exc.value.status == Status.CONFIG_ERROR
    del lists[cfg.done]
    with pytest.raises(KankanError):
        check_lists(cfg, lists)


def _hold_lock(repo, ready, release):
    paths = Paths.discover(repo)
    with project_lock(paths):
        ready.set()
        release.wait(10)


def test_project_lock_serializes(git_repo):
    paths = Paths.discover(git_repo)
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(git_repo, ready, release))
    proc.start()
    ready.wait(10)
    with pytest.raises(KankanError):
        with project_lock(paths, timeout=0.3):
            pass
    start = time.monotonic()
    ctx_timer = __import__("threading").Timer(0.3, release.set)
    ctx_timer.start()
    with project_lock(paths, timeout=10):
        assert time.monotonic() - start >= 0.25
        with project_lock(paths):  # reentrant
            pass
    proc.join()
    assert (paths.kankan_dir / "project.lock").exists()


def test_atomic_write_json_no_partial(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    atomic_write_json(target, {"a": 1})
    monkeypatch.setenv("KANKAN_FAULT", "before_replace")
    with pytest.raises(faults.SimulatedCrash):
        atomic_write_json(target, {"a": 2})
    assert read_json(target, None) == {"a": 1}
    assert read_json(tmp_path / "none.json", {"d": 1}) == {"d": 1}


def test_auditlog_append(git_repo):
    paths = Paths.discover(git_repo)
    auditlog.append(paths, "seat_recovered", seat="claude-1")
    auditlog.append(paths, "x")
    lines = (paths.kankan_dir / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2 and '"seat_recovered"' in lines[0]
