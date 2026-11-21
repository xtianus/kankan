"""``kankan destroy``: context and running-agents gate (destroy spec T§2)."""

import json
import os
import shlex

import pytest

from kankan import agents_cmd, destroy, procutil, seats
from kankan.config import SeatConfig, load_config, save_config
from kankan.refstore import seat_ref
from tests.helpers import NoTmux, hold


class FakeTmux:
    def __init__(self, sessions):
        self.sessions = sessions

    def sessions_with_option(self, option):
        return dict(self.sessions)


class BrokenTmux:
    def sessions_with_option(self, option):
        raise OSError("no tmux")


def write_proc(project, seat, **entries):
    path = project.paths.seats_dir / f"{seat}.proc.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"boot_id": procutil.boot_id(), **entries}))


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_gate_running_seat(kproject, holders):
    holders.append(hold(kproject, "claude-1"))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    entries = destroy.gate(ctx, NoTmux())
    assert len(entries) == 1 and "claude-1" in entries[0]["what"]
    assert entries[0]["remedy"] == "kankan stop"


def test_gate_seat_not_in_config(kproject, holders):
    holders.append(hold(kproject, "claude-1"))
    cfg = load_config(kproject.paths)
    cfg.seats.pop("claude-1")
    save_config(kproject.paths, cfg)
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert "claude-1" not in ctx.cfg.seats
    assert len(destroy.gate(ctx, NoTmux())) == 1


def test_gate_recorded_process_without_ref(kproject):
    me = os.getpid()
    write_proc(kproject, "claude-7", agent={"pid": me, "start": procutil.start_time(me)})
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert kproject.refs.read(seat_ref("claude-7")) is None
    entries = destroy.gate(ctx, NoTmux())
    assert len(entries) == 1 and "claude-7" in entries[0]["what"]
    write_proc(kproject, "claude-7", agent={"pid": me})
    assert destroy.gate(ctx, NoTmux()) == []


def test_gate_recorded_process_other_boot_ignored(kproject):
    me = os.getpid()
    write_proc(kproject, "claude-7", agent={"pid": me, "start": procutil.start_time(me)}, boot_id="other")
    assert destroy.gate(destroy.Ctx.open(kproject.paths.repo), NoTmux()) == []


def test_gate_session_and_unit(kproject):
    unit = agents_cmd.unit_path(kproject)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n")
    tmux = FakeTmux({"ai-proj-claude-1": str(kproject.paths.repo), "other": "/elsewhere"})
    entries = destroy.gate(destroy.Ctx.open(kproject.paths.repo), tmux)
    assert len(entries) == 2
    assert any("ai-proj-claude-1" in e["what"] for e in entries)
    assert any(str(unit) in e["what"] or unit.name in e["what"] for e in entries)


def test_gate_unreachable_tmux_is_no_session(kproject):
    assert destroy.gate(destroy.Ctx.open(kproject.paths.repo), BrokenTmux()) == []


def test_gate_no_config_remedies(kproject, holders):
    holder = hold(kproject, "claude-1")
    holders.append(holder)
    unit = agents_cmd.unit_path(kproject)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n")
    (kproject.paths.kankan_dir / "config.json").unlink()
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and ctx.reason
    entries = destroy.gate(ctx, FakeTmux({"ai-x": str(kproject.paths.repo)}))
    by = {e["what"].split()[0]: e["remedy"] for e in entries}
    assert len(entries) == 3 and "kankan stop" not in "".join(by.values())
    assert by["seat"] == f"kill {holder.pid}" or f"kill {holder.pid}" in by["seat"].split("; ")
    assert by["tmux"] == "tmux kill-session -t ai-x"
    assert by["autostart"].startswith("systemctl --user disable --now ") and by["autostart"].endswith(f"rm {unit}")


def test_gate_creates_no_files(kproject):
    cfg = load_config(kproject.paths)
    cfg.seats["claude-1"] = SeatConfig(type="claude")
    save_config(kproject.paths, cfg)
    assert not (kproject.paths.seats_dir / "claude-1.lock").exists()
    before = tree(kproject.paths.kankan_dir)
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert destroy.gate(ctx, NoTmux()) == []
    assert tree(kproject.paths.kankan_dir) == before


def test_running_does_not_take_project_lock(kproject, holders, monkeypatch):
    holders.append(hold(kproject, "claude-1"))
    def forbidden(*args, **kwargs):
        raise AssertionError("the project lock must not be taken")

    monkeypatch.setattr(seats, "project_lock", forbidden)
    monkeypatch.setattr("kankan.config.file_lock", forbidden)
    reasons = seats.running(kproject.paths, "claude-1")
    assert "lock held" in reasons
    assert any(r.startswith("pid ") for r in reasons)


def test_known_seats_suffixes(kproject):
    cfg = load_config(kproject.paths)
    cfg.seats.clear()
    save_config(kproject.paths, cfg)
    seats_dir = kproject.paths.seats_dir
    seats_dir.mkdir(parents=True, exist_ok=True)
    for name in ("claude-1.lock", "claude-1.state.lock", "codex-2.block.lock", "junk.txt", "Bad.lock"):
        (seats_dir / name).write_text("")
    assert destroy.known_seats(destroy.Ctx.open(kproject.paths.repo)) == ["claude-1", "codex-2"]


def config_path(project):
    return project.paths.kankan_dir / "config.json"


def test_ctx_open_without_config(kproject):
    config_path(kproject).unlink()
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and ctx.reason and ctx.trello is None
    assert ctx.refs.integration_branch == "kankan-agents"


def test_ctx_open_integration_branch_from_git_config(kproject):
    config_path(kproject).unlink()
    from kankan import gitutil
    gitutil.run(kproject.paths.repo, ["config", "--local", "kankan.integrationBranch", "agents-x"])
    assert destroy.Ctx.open(kproject.paths.repo).refs.integration_branch == "agents-x"


def test_ctx_open_invalid_json(kproject):
    config_path(kproject).write_text("{not json")
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and "config.json" in ctx.reason


def test_ctx_open_array(kproject):
    config_path(kproject).write_text("[1, 2]")
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and ctx.reason
    assert ctx.paths.project == kproject.paths.repo.name


@pytest.mark.parametrize("seats_value", ['"x"', "[1]", '{"claude-1": "x"}', '{"claude-1": {"nope": 1}}'])
def test_ctx_open_bad_seats(kproject, seats_value):
    data = json.loads(config_path(kproject).read_text())
    data["seats"] = json.loads(seats_value)
    config_path(kproject).write_text(json.dumps(data))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and ctx.reason


def test_ctx_open_no_board(kproject):
    config_path(kproject).write_text(json.dumps({"seats": {"claude-1": {"type": "claude"}}}))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is not None and ctx.reason == "no board"
    assert list(ctx.cfg.seats) == ["claude-1"] and ctx.trello is None


def test_ctx_open_with_config_has_trello(kproject):
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.reason is None and ctx.trello is not None and ctx.refs.integration_branch == "main"


def test_ctx_open_without_credentials(kproject, monkeypatch):
    monkeypatch.delenv("KANKAN_TRELLO_KEY")
    monkeypatch.delenv("KANKAN_TRELLO_TOKEN")
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is not None and ctx.trello is None and ctx.trello_reason


def test_gate_remedies_are_shell_quoted(kproject, monkeypatch):
    spaced = kproject.paths.repo.parent / "home dir"
    spaced.mkdir()
    monkeypatch.setenv("HOME", str(spaced))
    unit = agents_cmd.unit_path(kproject)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n")
    config_path(kproject).unlink()
    entries = destroy.gate(destroy.Ctx.open(kproject.paths.repo), FakeTmux({"my session": str(kproject.paths.repo)}))
    remedies = [e["remedy"] for e in entries]
    assert "tmux kill-session -t 'my session'" in remedies
    assert any(r.endswith(f"rm '{unit}'") for r in remedies)


def test_gate_missing_credentials_uses_direct_remedies(kproject, monkeypatch):
    monkeypatch.delenv("KANKAN_TRELLO_KEY")
    monkeypatch.delenv("KANKAN_TRELLO_TOKEN")
    ctx = destroy.Ctx.open(kproject.paths.repo)
    entries = destroy.gate(ctx, FakeTmux({"ai-x": str(kproject.paths.repo)}))
    assert [e["remedy"] for e in entries] == ["tmux kill-session -t ai-x"]


def deny(monkeypatch, module, name, suffix):
    """Make ``module.name`` raise PermissionError for paths ending in ``suffix`` (deterministic under root)."""
    real = getattr(module, name)

    def guarded(path, *args, **kwargs):
        if str(path).endswith(suffix):
            raise PermissionError(13, "denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(module, name, guarded)


def test_gate_unreadable_lock_blocks(kproject, monkeypatch):
    lock = kproject.paths.seats_dir / "claude-1.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    deny(monkeypatch, os, "open", "claude-1.lock")
    entries = destroy.gate(destroy.Ctx.open(kproject.paths.repo), NoTmux())
    assert len(entries) == 1 and "claude-1" in entries[0]["what"]
    remedy = entries[0]["remedy"]
    quoted = shlex.quote(str(lock))
    assert f"chmod u+rw {quoted}" in remedy
    rest = remedy.replace(quoted, "<lock>")
    assert "rm " not in rest and "remove" not in rest


@pytest.mark.parametrize("content", ["{broken", "[1]", "null"])
def test_gate_malformed_record_blocks(kproject, content):
    path = kproject.paths.seats_dir / "claude-1.proc.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    entries = destroy.gate(destroy.Ctx.open(kproject.paths.repo), NoTmux())
    assert len(entries) == 1 and "proc.json" in entries[0]["remedy"]


def test_gate_unreadable_record_blocks(kproject, monkeypatch):
    path = kproject.paths.seats_dir / "claude-1.proc.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    deny(monkeypatch, seats, "read_json", "claude-1.proc.json")
    entries = destroy.gate(destroy.Ctx.open(kproject.paths.repo), NoTmux())
    assert len(entries) == 1 and "proc.json" in entries[0]["remedy"]


def test_ctx_open_config_is_a_directory(kproject):
    config_path(kproject).unlink()
    config_path(kproject).mkdir()
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and "config.json" in ctx.reason


def test_ctx_open_unreadable_config(kproject, monkeypatch):
    from kankan import config
    deny(monkeypatch, config, "read_json", "config.json")
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and ctx.reason and ctx.trello is None


@pytest.mark.parametrize("field,value", [("board_id", False), ("trello_base_url", None), ("trello_timeout", "5"),
                                         ("trello_timeout", True), ("worktree_root", 5), ("project_name", 5)])
def test_ctx_open_invalid_field_types(kproject, tmp_path, field, value):
    default_root = destroy.Ctx.open(kproject.paths.repo).paths.worktree_root
    data = json.loads(config_path(kproject).read_text())
    data.update({"worktree_root": str(tmp_path / "custom")} if field != "worktree_root" else {})
    data[field] = value
    config_path(kproject).write_text(json.dumps(data))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and field in ctx.reason and ctx.trello is None and not ctx.usable
    assert ctx.paths.worktree_root == default_root


def test_ctx_open_rejected_config_keeps_default_paths(kproject, tmp_path):
    default_root = destroy.Ctx.open(kproject.paths.repo).paths.worktree_root
    data = json.loads(config_path(kproject).read_text())
    data.update(seats="x", worktree_root=str(tmp_path / "custom"), project_name="custom")
    config_path(kproject).write_text(json.dumps(data))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None
    assert ctx.paths.project == kproject.paths.repo.name and ctx.paths.worktree_root == default_root


def test_ctx_open_valid_config_applies_path_overrides(kproject, tmp_path):
    data = json.loads(config_path(kproject).read_text())
    data.update(worktree_root=str(tmp_path / "custom"))
    config_path(kproject).write_text(json.dumps(data))
    assert destroy.Ctx.open(kproject.paths.repo).paths.worktree_root == tmp_path / "custom"


def test_ctx_open_board_without_other_required_fields(kproject):
    config_path(kproject).write_text(json.dumps({"board_id": "b"}))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.cfg is None and ctx.reason and ctx.trello is None


def test_ctx_open_null_base_url(kproject):
    data = json.loads(config_path(kproject).read_text())
    data["trello_base_url"] = None
    config_path(kproject).write_text(json.dumps(data))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.trello is None and ctx.trello_reason


def test_known_seats_ignores_configured_seats_without_board(kproject):
    config_path(kproject).write_text(json.dumps({"seats": {"claude-1": {"type": "claude"}}}))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.reason == "no board" and destroy.known_seats(ctx) == []


def test_known_seats_propagates_ref_listing_failure(kproject, monkeypatch):
    from kankan.errors import KankanError, Status
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert destroy.known_seats(ctx) == []

    def broken(prefix):
        raise KankanError(Status.INTERNAL_ERROR, "git for-each-ref failed")

    monkeypatch.setattr(ctx.refs, "list_raw", broken)
    with pytest.raises(KankanError):
        destroy.known_seats(ctx)


def test_ctx_open_board_id_diagnostic(kproject):
    data = json.loads(config_path(kproject).read_text())
    data["board_id"] = 1
    config_path(kproject).write_text(json.dumps(data))
    ctx = destroy.Ctx.open(kproject.paths.repo)
    assert ctx.reason == f"invalid {config_path(kproject)}: board_id must be a string"
