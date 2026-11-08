"""Append-only audit log ``<gcd>/kankan/log.jsonl`` (one JSON object per line)."""

from __future__ import annotations

import json
import os

from kankan import clock


def append(paths, event: str, **fields) -> None:
    record = {"ts": clock.iso(clock.now()), "event": event, "pid": os.getpid(), **fields}
    line = (json.dumps(record, default=str, sort_keys=False) + "\n").encode()
    paths.kankan_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.kankan_dir / "log.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def read(paths) -> list:
    path = paths.kankan_dir / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
