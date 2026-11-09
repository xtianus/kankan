"""Identity of the kankan code being run, so launchers and agent commands can detect a mismatch."""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path
from typing import Optional

PACKAGE = Path(__file__).resolve().parent
SUFFIXES = {".py", ".json", ".service", ""}  # not the skill text: agents read it fresh anyway


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
