"""Identity of the kankan code being run, so launchers and agent commands can detect a mismatch, and the command
that starts this same kankan in another process."""

from __future__ import annotations

import functools
import hashlib
import sys
from pathlib import Path
from typing import List, Optional

from kankan import __version__

PACKAGE = Path(__file__).resolve().parent
SUFFIXES = {".py", ".json", ".service", ""}  # not the skill text: agents read it fresh anyway

# `python -m kankan` puts the current directory first on sys.path, so a directory holding a `kankan/` package
# (kankan's own repository, where the launchers and agents run) would be imported instead of this installation.
# The bootstrap drops the current directory ("") before importing kankan: the effect of `-P`, which needs 3.11.
# It holds no quotes, `$` or `%`, so it can be quoted as is for a shell or a systemd unit.
BOOTSTRAP = "import sys; sys.path[:] = [p for p in sys.path if p]; from kankan.cli import main; sys.exit(main())"


def command(*args: str) -> List[str]:
    """argv running this installation's kankan with ``args``, whatever the working directory holds."""
    return [sys.executable, "-c", BOOTSTRAP, *args]


@functools.lru_cache(maxsize=None)
def _digest(package: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in package.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                       and p.suffix in SUFFIXES):
        h.update(str(path.relative_to(package)).encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def code_id(package: Optional[Path] = None) -> str:
    """Short hash of the package files: changes with any edit, even in an editable install."""
    if package is None:
        return _digest(PACKAGE)
    _digest.cache_clear()
    return _digest(Path(package))


def identity() -> dict:
    """What the seat file records about the running kankan: the hash compares, version and location explain."""
    return {"code": code_id(), "version": __version__, "package": str(PACKAGE)}
