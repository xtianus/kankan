"""Append-only audit log ``<gcd>/kankan/log.jsonl`` (one JSON object per line)."""

from __future__ import annotations

import errno
import json
import os

from kankan import clock


def append(paths, event: str, /, *, follow: bool = True, **fields) -> None:
    """Append one record. With ``follow=False`` (``kankan destroy``) the file must be a regular file reached
    without a symbolic link, neither at ``log.jsonl`` nor at ``<gcd>/kankan``: a link, a FIFO or a file with several
    hard links raises ``OSError`` and nothing is written anywhere."""
    record = {"ts": clock.iso(clock.now()), "event": event, "pid": os.getpid(), **fields}
    line = (json.dumps(record, default=str, sort_keys=False) + "\n").encode()
    if follow:
        paths.kankan_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(paths.kankan_dir / "log.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
    else:
        from kankan.config import open_nofollow
        # neither <gcd>/kankan nor log.jsonl is followed; a FIFO without a reader fails, never blocks
        fd = open_nofollow(paths.kankan_dir / "log.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        if not follow and os.fstat(fd).st_nlink != 1:  # a hard link writes into another file too
            raise OSError(errno.EINVAL, "not a regular file with a single link", str(paths.kankan_dir / "log.jsonl"))
        os.write(fd, line)
    finally:
        os.close(fd)


def read(paths) -> list:
    path = paths.kankan_dir / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
