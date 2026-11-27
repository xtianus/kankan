"""Q&A pool seat names, configuration and adapters (qapool T§2)."""

import pytest

from kankan import agents_cmd, destroy, qapool, seats
from kankan.adapters import load_adapter
from kankan.config import Config, Paths, SeatConfig, atomic_write_json, load_config
from kankan.errors import KankanError, Status
from kankan.refstore import validate_seat


def _cfg(**seat_cfgs) -> Config:
    return Config(board_id="b", board_short="s", registry_id="r", backlog="l1", done="l2", blocked="l3",
                  label_hold="h", label_question="q", label_error="e", test_command="",
                  seats=dict(seat_cfgs))


def test_seat_pattern_accepts_qa_members():
    for good in ("claude-qa", "claude-qa2", "claude-qa9", "claude-qa10", "codex-qa123"):
        assert validate_seat(good) == good
    for bad in ("claude-qa1", "claude-qa0", "claude-qa02", "claude-qa-2", "claude-qa2x"):
        with pytest.raises(KankanError):
            validate_seat(bad)


def test_destroy_ref_pattern_matches_members():
    for ref in ("refs/agent-seats/claude-qa", "refs/agent-seats/claude-qa2", "refs/agent-seats/codex-qa17"):
        assert destroy._KANKAN_REF.fullmatch(ref)
    for ref in ("refs/agent-seats/claude-qa1", "refs/agent-seats/claude-qa02"):
        assert not destroy._KANKAN_REF.fullmatch(ref)


def test_is_qa_router_and_members():
    assert seats.is_qa("claude-qa") and seats.is_qa("claude-qa2") and seats.is_qa("codex-qa10")
    assert not seats.is_qa("claude-1") and not seats.is_qa("claude-12")
    assert seats.seat_type("claude-qa2") == "claude" and seats.seat_type("codex-qa10") == "codex"
    assert qapool.member_number("claude-qa") == 1 and qapool.member_number("claude-qa12") == 12
    assert qapool.is_member("claude-qa3") and not qapool.is_member("claude-qa") and not qapool.is_member("claude-3")


def test_members_not_counted_in_cap_and_counts():
    cfg = _cfg(**{"claude-qa2": SeatConfig("claude", role="qa"), "claude-1": SeatConfig("claude", "a", "b", "c"),
                  "claude-qa": SeatConfig("claude", role="qa"), "claude-qa3": SeatConfig("claude", role="qa")})
    assert list(cfg.work_seats()) == ["claude-1"]
    assert cfg.qa_seat() == "claude-qa"  # the router, whatever the order of the entries
    assert qapool.router(cfg) == "claude-qa"
    assert qapool.members(cfg) == ["claude-qa2", "claude-qa3"]
    only_members = _cfg(**{"claude-qa2": SeatConfig("claude", role="qa")})
    assert only_members.qa_seat() is None and qapool.router(only_members) is None


def test_registered_args_skip_members():
    cfg = _cfg(**{"claude-qa2": SeatConfig("claude", role="qa"), "claude-1": SeatConfig("claude", "a", "b", "c"),
                  "claude-qa": SeatConfig("claude", role="qa")})
    assert agents_cmd.registered_args(cfg) == "claude=1 qa=claude"
    members_only = _cfg(**{"claude-qa2": SeatConfig("claude", role="qa")})
    assert agents_cmd.registered_args(members_only) == ""


@pytest.mark.parametrize("key,value", [("qa_prefix_warm", ""), ("qa_prefix_warm", "a b"), ("qa_prefix_cold", 3),
                                       ("qa_prefix_cold", " "), ("qa_prefix_warm", None)])
def test_prefix_config_validation(git_repo, key, value):
    paths = Paths.discover(git_repo)
    data = _cfg().to_dict()
    assert data["qa_prefix_warm"] == "🟢" and data["qa_prefix_cold"] == "⚪"
    atomic_write_json(paths.kankan_dir / "config.json", data)
    assert load_config(paths).qa_prefix_cold == "⚪"
    data[key] = value
    atomic_write_json(paths.kankan_dir / "config.json", data)
    with pytest.raises(KankanError) as exc:
        load_config(paths)
    assert exc.value.status == Status.CONFIG_ERROR and key in exc.value.message
    data[key] = "🟢🔥"  # several characters, no whitespace
    atomic_write_json(paths.kankan_dir / "config.json", data)
    assert getattr(load_config(paths), key) == "🟢🔥"


def test_adapter_prompt_cache_defaults_and_validation(git_repo, isolated_home):
    paths = Paths.discover(git_repo)
    claude = load_adapter(paths, "claude", read_only=True)
    codex = load_adapter(paths, "codex", read_only=True)
    assert claude.prompt_cache == {"expiry": "exact", "lifetime_minutes": 60, "reporter": "claude_status_line"}
    assert codex.prompt_cache == {"expiry": "estimated", "lifetime_minutes": 30, "reporter": None}
    for bad in ({"expiry": "exact", "reporter": None}, {"expiry": "exact", "reporter": "unknown"},
                {"expiry": "sometimes"}, {"lifetime_minutes": 0}, {"lifetime_minutes": True},
                {"lifetime_minutes": "60"}):
        atomic_write_json(paths.kankan_dir / "agents.json", {"claude": {"prompt_cache": bad}})
        with pytest.raises(KankanError) as exc:
            load_adapter(paths, "claude", read_only=True)
        assert "prompt_cache" in exc.value.message
    atomic_write_json(paths.kankan_dir / "agents.json",
                      {"codex": {"prompt_cache": {"lifetime_minutes": 0.05}}})  # tests: seconds-long lifetimes
    assert load_adapter(paths, "codex", read_only=True).prompt_cache["lifetime_minutes"] == 0.05


def test_next_member_name_lowest_free():
    cfg = _cfg(**{"claude-qa": SeatConfig("claude", role="qa")})
    assert qapool.next_member_name(cfg, "claude") == "claude-qa2"
    cfg.seats["claude-qa2"] = SeatConfig("claude", role="qa")
    cfg.seats["claude-qa4"] = SeatConfig("claude", role="qa")
    assert qapool.next_member_name(cfg, "claude") == "claude-qa3"
    cfg.seats["claude-qa3"] = SeatConfig("claude", role="qa")
    assert qapool.next_member_name(cfg, "claude") == "claude-qa5"
