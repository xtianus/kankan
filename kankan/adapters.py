"""Per-agent-type adapters and screen classification (spec §8.6, §8.7)."""

from __future__ import annotations

import copy
import json
import os
import re
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


@dataclass
class Classification:
    state: str  # known, limit, working, ready, trust, unknown
    pattern: Optional[str] = None
    answer: Optional[List[str]] = None
    resume_at: Optional[datetime] = None


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError as err:
        raise KankanError(Status.CONFIG_ERROR, f"invalid adapter overrides in {path}: {err}")


def load_adapter(paths, agent_type: str) -> Adapter:
    """Defaults, then ``~/.config/kankan/agents.json``, then ``<gcd>/kankan/agents.json``."""
    data = json.loads(resources.files("kankan").joinpath("adapters_default.json").read_text())
    user = Path(os.path.expanduser("~")) / ".config" / "kankan" / "agents.json"
    merged = data.get(agent_type, {})
    for override in (_read(user), _read(paths.kankan_dir / "agents.json") if paths is not None else {}):
        merged = _deep_merge(merged, override.get(agent_type, {}))
    if not merged:
        raise KankanError(Status.CONFIG_ERROR, f"no adapter for agent type {agent_type!r}")
    try:
        adapter = Adapter(type=agent_type, **{k: v for k, v in merged.items() if not k.startswith("_")})
    except TypeError as err:
        raise KankanError(Status.CONFIG_ERROR, f"invalid adapter for {agent_type}: {err}")
    for group in (adapter.ready, adapter.working, adapter.limit, adapter.limit_reset, adapter.trust):
        for pattern in group:
            _compile(pattern)
    return adapter


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
