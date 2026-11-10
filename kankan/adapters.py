"""Per-agent-type adapters and screen classification (spec §8.6, §8.7)."""

from __future__ import annotations

import copy
import json
import os
import re
import math
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from importlib import resources
from pathlib import Path
from typing import List, Optional

from kankan.errors import KankanError, Status

_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
_URL = re.compile(r"https?://\S*")
_FLAGS = re.I | re.M


@dataclass(frozen=True)
class ModelEffort:
    model: str
    effort: str

    def to_dict(self) -> dict:
        return {"model": self.model, "effort": self.effort}

    @property
    def name(self) -> str:
        return f"{self.model} · {self.effort}"


@dataclass
class Adapter:
    type: str
    command: List[str]
    new_conversation: str
    submit_key: str
    queue_while_working: bool
    ready: List[str]
    working: List[str]
    limit: List[str]
    limit_reset: List[str]
    trust: List[str]
    trust_answer: List[str]
    known: List[dict] = field(default_factory=list)
    login_check: List[str] = field(default_factory=list)
    full_access: List[str] = field(default_factory=list)  # arguments that must be in `command` (spec §8.7)
    models: dict = field(default_factory=dict)
    seed: dict = field(default_factory=dict)
    model_args: List[str] = field(default_factory=list)
    effort_args: List[str] = field(default_factory=list)
    exit_command: str = "/exit"
    exit_timeout_seconds: float = 20
    start_timeout_seconds: float = 120
    models_from: dict = field(default_factory=dict)
    version_command: List[str] = field(default_factory=list)
    model_check: List[str] = field(default_factory=list)
    model_check_home: dict = field(default_factory=dict)
    model_check_timeout_seconds: float = 90
    model_check_retry_seconds: float = 600
    restart_keep: List[str] = field(default_factory=list)
    seed_sources: dict = field(default_factory=dict)
    stale_seeds: dict = field(default_factory=dict)  # seeds the agent's catalog no longer lists (treated as missing)



@dataclass
class Classification:
    state: str  # known, limit, working, ready, trust, unknown
    pattern: Optional[str] = None
    answer: Optional[List[str]] = None
    resume_at: Optional[datetime] = None


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key != "models" and isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return data
    except ValueError as err:
        raise KankanError(Status.CONFIG_ERROR, f"invalid adapter overrides in {path}: {err}")


def _sources(paths):
    return [Path(os.path.expanduser("~")) / ".config" / "kankan" / "agents.json"] + (
        [paths.kankan_dir / "agents.json"] if paths is not None else [])


def adapter_types(paths=None) -> list[str]:
    data = json.loads(resources.files("kankan").joinpath("adapters_default.json").read_text())
    for path in _sources(paths):
        data.update(_read(path))
    return [key for key in data if not key.startswith("_")]


def combinations(adapter: Adapter) -> list[ModelEffort]:
    return [ModelEffort(model, effort) for model, value in adapter.models.items() for effort in value["efforts"]]


def expand(template: list[str], **values) -> list[str]:
    return [arg.format_map(values) for arg in template]


def start_command(adapter: Adapter, pair: ModelEffort) -> list[str]:
    return list(adapter.command) + expand(adapter.model_args, model=pair.model) + expand(adapter.effort_args, effort=pair.effort)


def _name(value):
    return isinstance(value, str) and bool(value.strip()) and not any(char in value for char in "\r\n\x00")


def _template(argv, allowed, required=()):
    if not isinstance(argv, list) or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
        raise ValueError("argument template must be a string list")
    fields = []
    for arg in argv:
        for _, name, spec, conversion in string.Formatter().parse(arg):
            if name is not None:
                if name not in allowed or spec or conversion:
                    raise ValueError(f"invalid placeholder {name!r}")
                fields.append(name)
    if any(fields.count(name) != 1 for name in required):
        raise ValueError(f"template must contain each of {required} exactly once")


def load_adapter(paths, agent_type: str, *, read_only: bool = False) -> Adapter:
    """Load local configuration and an already prepared catalog; never run discovery here."""
    data = json.loads(resources.files("kankan").joinpath("adapters_default.json").read_text())
    merged = data.get(agent_type, {})
    explicit_models = False
    sources = {}
    try:
        for path in _sources(paths):
            override = _read(path).get(agent_type, {})
            if not isinstance(override, dict):
                raise ValueError(f"{path}: adapter must be an object")
            explicit_models |= "models" in override
            sources.update({role: str(path) for role in override.get("seed", {})})
            merged = _deep_merge(merged, override)
        if not merged:
            raise ValueError("no adapter defined")
        # Models from the agent's catalog (or the shipped list standing in for a missing one) follow the account.
        from_catalog = not explicit_models and bool(merged.get("models_from"))
        if from_catalog:
            from kankan.model_catalog import cached
            catalog = cached(agent_type)
            if catalog:
                merged["models"] = catalog["models"]
        adapter = Adapter(type=agent_type, **{k: v for k, v in merged.items() if not k.startswith("_")})
        adapter.seed_sources = sources
        if not isinstance(adapter.models, dict) or not adapter.models:
            raise ValueError("models must be a nonempty object")
        for model, value in adapter.models.items():
            if not _name(model) or not isinstance(value, dict):
                raise ValueError("invalid model")
            efforts = value.get("efforts")
            if not isinstance(efforts, list) or not efforts or not all(_name(effort) for effort in efforts) or len(efforts) != len(set(efforts)):
                raise ValueError(f"{model}: efforts must be a nonempty unique list")
        pairs = combinations(adapter)
        if len(pairs) > 61:
            raise ValueError("at most 61 model/effort combinations are supported")
        if not isinstance(adapter.seed, dict):
            raise ValueError("seeds must be an object")
        adapter.stale_seeds = {}
        if from_catalog:
            # The account or CLI can drop a seeded pair: it then counts as a missing seed, never a config error.
            for role, value in list(adapter.seed.items()):
                if role in ("implementation", "review") and isinstance(value, dict) and set(value) == {"model", "effort"} \
                        and all(isinstance(v, str) for v in value.values()) and ModelEffort(**value) not in pairs:
                    adapter.stale_seeds[role] = adapter.seed.pop(role)
        for role, value in adapter.seed.items():
            if role not in ("implementation", "review") or not isinstance(value, dict) or ModelEffort(**value) not in pairs:
                raise ValueError(f"invalid seed for {role}; allowed: {', '.join(p.name for p in pairs)}")
        _template(adapter.model_args, {"model"}, ["model"])
        _template(adapter.effort_args, {"effort"}, ["effort"])
        _template(adapter.model_check, {"model", "effort"}, ["model", "effort"])
        _template(adapter.version_command, set())
        _template(adapter.models_from.get("command", []), set())
        _template([adapter.models_from.get("file", "")], {"CODEX_HOME", "HOME"})
        if adapter.model_check_home:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", adapter.model_check_home["env"]):
                raise ValueError("invalid model_check_home variable")
            for target in adapter.model_check_home.get("files", []):
                if Path(target).is_absolute() or ".." in Path(target).parts or not _name(target):
                    raise ValueError("model_check_home files must be relative names")
        for key in ("exit_timeout_seconds", "start_timeout_seconds", "model_check_timeout_seconds", "model_check_retry_seconds"):
            value = getattr(adapter, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be positive")
        if not _name(adapter.exit_command):
            raise ValueError("invalid exit command")
        for group in (adapter.ready, adapter.working, adapter.limit, adapter.limit_reset, adapter.trust, adapter.restart_keep):
            for pattern in group:
                _compile(pattern)
        return adapter
    except (TypeError, ValueError, KeyError) as err:
        raise KankanError(Status.CONFIG_ERROR, f"invalid adapter for {agent_type}: {err}")


def _compile(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern, _FLAGS)
    except re.error as err:
        raise KankanError(Status.CONFIG_ERROR, f"invalid adapter pattern {pattern!r}: {err}")


def clean(screen: str) -> str:
    return _URL.sub("", _CSI.sub("", _OSC.sub("", screen)))


def tail(screen: str, lines: int) -> str:
    """The last ``lines`` lines ending at the last non-blank line."""
    rows = clean(screen).split("\n")
    end = len(rows)
    while end > 0 and not rows[end - 1].strip():
        end -= 1
    return "\n".join(rows[max(0, end - lines):end])


def _first(patterns: List[str], text: str) -> Optional[str]:
    for pattern in patterns:
        if _compile(pattern).search(text):
            return pattern
    return None


def classify(adapter: Adapter, screen: str, tail_lines: int = 30, now: Optional[datetime] = None,
             tz: Optional[tzinfo] = None) -> Classification:
    text = tail(screen, tail_lines)
    for dialog in adapter.known:
        patterns = dialog.get("patterns", [])
        hits = [p for p in patterns if _compile(p).search(text)]
        if patterns and ((dialog.get("all") and len(hits) == len(patterns)) or (not dialog.get("all") and hits)):
            return Classification("known", dialog.get("name") or hits[0], list(dialog.get("answer") or []))
    pattern = _first(adapter.trust, text)
    if pattern:
        return Classification("trust", pattern, list(adapter.trust_answer))
    pattern = _first(adapter.limit, text)
    if pattern:
        resume_at = None
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if _compile(pattern).search(line):
                window = " ".join(lines[index:index + 8])
                if _first(adapter.limit_reset, window):
                    resume_at = parse_resume(window, now or datetime.now(timezone.utc), tz)
                break
        return Classification("limit", pattern, None, resume_at)
    pattern = _first(adapter.working, text)
    if pattern:
        return Classification("working", pattern)
    pattern = _first(adapter.ready, text)
    if pattern:
        return Classification("ready", pattern)
    return Classification("unknown")


_DURATION = re.compile(r"(\d+)\s*(weeks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def _zone(text: str) -> Optional[tzinfo]:
    match = re.search(r"\((UTC|GMT|[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+)\)", text)
    if not match:
        return None
    name = match.group(1)
    if name in ("UTC", "GMT"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def parse_resume(text: str, now: datetime, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """Resume time stated by a limit message (§8.6): the message's zone, else the server's;
    a time already passed means the next day. Returns an aware datetime or None."""
    text = _URL.sub("", text)
    if re.search(r"(resets?|try\s+again|retry|wait)\s+in[:\s]+", text, re.I):
        total = 0
        for number, unit in _DURATION.findall(text):
            total += int(number) * _UNITS[unit[0].lower()]
        if total:
            return now + timedelta(seconds=total)
    zone = _zone(text) or tz or datetime.now().astimezone().tzinfo
    local_now = now.astimezone(zone)
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.I)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        ampm = match.group(3).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    else:
        match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    date_match = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(\d{4}))?",
                           text, re.I)
    if date_match:
        year = int(date_match.group(3) or local_now.year)
        try:
            return local_now.replace(year=year, month=months.index(date_match.group(1).lower()) + 1,
                                     day=int(date_match.group(2)), hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day_match = re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", text, re.I)
    if day_match:
        target = "mon tue wed thu fri sat sun".split().index(day_match.group(1).lower())
        candidate += timedelta(days=(target - local_now.weekday()) % 7)
        if candidate <= local_now:
            candidate += timedelta(days=7)
        return candidate
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate
