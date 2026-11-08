"""Time source, monkeypatched in tests."""

import time
from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def monotonic() -> float:
    return time.monotonic()


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
