import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kankan.adapters import classify, load_adapter, parse_resume, tail
from kankan.config import Paths

SCREENS = Path(__file__).parent / "fixtures" / "screens"
NOW = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)


def fixtures():
    return sorted(SCREENS.glob("*.txt"))


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_classify_saved_screens(path):
    state = path.stem.split("__")[0]
    types = [t for t in ("claude", "codex") if t in path.stem] or ["claude", "codex"]
    for agent_type in types:
        result = classify(load_adapter(None, agent_type), path.read_text(), 30, NOW, timezone.utc)
        assert result.state == state, (agent_type, result)


def test_known_dialog_answers():
    codex = load_adapter(None, "codex")
    result = classify(codex, (SCREENS / "known__codex_model_switch.txt").read_text())
    assert result.answer == ["3"]
    result = classify(codex, (SCREENS / "trust__codex_trust.txt").read_text())
    assert result.answer == ["Enter"]
    assert classify(load_adapter(None, "claude"), (SCREENS / "trust__claude_trust.txt").read_text()).answer == ["Down", "Enter"]


def test_limit_resume_time():
    claude = load_adapter(None, "claude")
    result = classify(claude, (SCREENS / "limit__claude_session_limit.txt").read_text(), 30, NOW, timezone.utc)
    assert result.resume_at == datetime(2026, 11, 8, 16, 0, tzinfo=ZoneInfo("Europe/Rome"))
    codex = load_adapter(None, "codex")
    result = classify(codex, (SCREENS / "limit__codex_usage_limit.txt").read_text(), 30, NOW, timezone.utc)
    assert result.resume_at == datetime(2026, 11, 8, 19, 5, tzinfo=timezone.utc)


def test_old_limit_outside_tail_ignored():
    claude = load_adapter(None, "claude")
    screen = (SCREENS / "ready__claude_old_limit_scrolled.txt").read_text()
    assert classify(claude, screen, 30).state == "ready"
    assert classify(claude, screen, 100).state == "limit"


def test_override_changes_classification(git_repo, isolated_home):
    paths = Paths.discover(git_repo)
    screen = (SCREENS / "unknown__random_program.txt").read_text()
    assert classify(load_adapter(paths, "claude"), screen).state == "unknown"
    paths.kankan_dir.mkdir(parents=True, exist_ok=True)
    (paths.kankan_dir / "agents.json").write_text(json.dumps({"claude": {"ready": ["press any key"]}}))
    assert classify(load_adapter(paths, "claude"), screen).state == "ready"
    adapter = load_adapter(paths, "claude")
    assert adapter.working and adapter.new_conversation == "/clear"
    user = isolated_home / ".config" / "kankan" / "agents.json"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(json.dumps({"claude": {"new_conversation": "/new"}}))
    assert load_adapter(paths, "claude").new_conversation == "/new"


def test_resume_past_time_rolls_to_tomorrow():
    result = parse_resume("resets 1pm", NOW, timezone.utc)
    assert result == datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)
    assert parse_resume("resets 13:30", NOW, timezone.utc) == datetime(2026, 11, 9, 13, 30, tzinfo=timezone.utc)


def test_resume_uses_message_timezone():
    result = parse_resume("resets 4pm (America/New_York)", NOW, timezone.utc)
    assert result == datetime(2026, 11, 8, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    assert parse_resume("try again in 2 hours 5 minutes", NOW).isoformat() == "2026-11-08T16:05:00+00:00"
    assert parse_resume("resets Nov 10, 9am", NOW, timezone.utc) == datetime(2026, 11, 10, 9, tzinfo=timezone.utc)
    assert parse_resume("nothing here", NOW) is None


def test_ansi_and_urls_stripped():
    screen = "\x1b[31mYou've hit your usage limit\x1b[0m https://example.com/10:30pm\n"
    assert "\x1b" not in tail(screen, 30)
    result = classify(load_adapter(None, "codex"), screen, 30, NOW, timezone.utc)
    assert result.state == "limit" and result.resume_at is None


def test_adapter_check_cli_file(capsys):
    from kankan import cli
    code = cli.main(["adapter", "check", str(SCREENS / "working__codex_working.txt"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["data"]["state"] == "working" and out["data"]["type"] == "codex"


def test_ordered_models_replace_and_build_argv(git_repo, isolated_home):
    from kankan.adapters import ModelEffort, adapter_types, combinations, start_command
    from kankan.config import atomic_write_json
    paths = Paths.discover(git_repo)
    target = paths.kankan_dir / 'agents.json'
    atomic_write_json(target, {'claude': {'models': {'z-model': {'efforts': ['high', 'low']},
                                                   'a-model': {'efforts': ['low']}},
                                        'seed': {role: {'model': 'z-model', 'effort': 'high'} for role in ('implementation', 'review')}}},
                      sort_keys=False)
    adapter = load_adapter(paths, 'claude', read_only=True)
    assert combinations(adapter) == [ModelEffort('z-model', 'high'), ModelEffort('z-model', 'low'), ModelEffort('a-model', 'low')]
    assert start_command(adapter, ModelEffort('z-model', 'high'))[-4:] == ['--model', 'z-model', '--effort', 'high']
    assert adapter.exit_timeout_seconds == 20 and adapter.start_timeout_seconds == 120
    assert adapter.model_check_timeout_seconds == 90 and adapter.model_check_retry_seconds == 600
    data = json.loads(target.read_text())
    from importlib import resources
    third = json.loads(resources.files('kankan').joinpath('adapters_default.json').read_text())['claude']
    data['third'] = third | data['claude'] | {'command': ['third']}
    atomic_write_json(target, data, sort_keys=False)
    assert adapter_types(paths) == ['claude', 'codex', 'third']
    assert load_adapter(paths, 'third').command == ['third']


@pytest.mark.parametrize('change', [
    {'models': {'bad': {'efforts': []}}}, {'models': {'bad\nmodel': {'efforts': ['high']}}},
    {'model_args': ['{unknown}']}, {'model_args': ['{model!r}']},
    {'model_args': ['{model', '}']}, {'start_timeout_seconds': False},
    {'model_check_retry_seconds': 0}, {'restart_keep': ['[']},
    {'seed': {'implementation': {'model': 'absent', 'effort': 'low'}}},
    {'models': {f'm{i}': {'efforts': ['high']} for i in range(62)}},
])
def test_invalid_model_adapter_rejected(git_repo, change):
    from kankan.config import atomic_write_json
    from kankan.errors import KankanError
    paths = Paths.discover(git_repo)
    atomic_write_json(paths.kankan_dir / 'agents.json', {'claude': change})
    with pytest.raises(KankanError):
        load_adapter(paths, 'claude', read_only=True)
