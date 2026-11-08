"""Java ``.properties`` files edited in place, preserving every byte kankan does not own.

kankan never rewrites a properties file from scratch: it keeps comments, order,
foreign keys and line endings, replaces only the logical lines of its own keys
and appends missing keys at the end (spec §6.2).
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from kankan import faults

_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}


@dataclass
class _Entry:
    key: str
    value: str
    first: int  # index of the first physical line
    last: int  # index of the last physical line (inclusive)


def _split_lines(text: str) -> List[str]:
    """Split keeping line terminators (\\r\\n, \\n or \\r)."""
    return text.splitlines(keepends=True)


def _strip_eol(line: str) -> str:
    return line.rstrip("\r\n")


def _continues(content: str) -> bool:
    count = len(content) - len(content.rstrip("\\"))
    return count % 2 == 1


def _unescape(raw: str) -> str:
    out, i = [], 0
    while i < len(raw):
        char = raw[i]
        if char == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "u" and i + 6 <= len(raw):
                try:
                    out.append(chr(int(raw[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _split_key(logical: str):
    """Split a logical line into (raw key, raw value)."""
    i = 0
    while i < len(logical):
        char = logical[i]
        if char == "\\":
            i += 2
            continue
        if char in "=: \t\f":
            break
        i += 1
    key = logical[:i]
    rest = logical[i:]
    rest = rest.lstrip(" \t\f")
    if rest[:1] in ("=", ":"):
        rest = rest[1:].lstrip(" \t\f")
    return key, rest


def _escape_value(value: str) -> str:
    out = []
    for index, char in enumerate(value):
        if char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif char == " " and index == 0:
            out.append("\\ ")
        else:
            out.append(char)
    return "".join(out)


class PropertiesFile:
    def __init__(self, path: Path, lines: List[str]):
        self.path = Path(path)
        self.lines = lines
        self.newline = "\n"
        for line in lines:
            if line.endswith("\r\n"):
                self.newline = "\r\n"
                break
            if line.endswith("\n"):
                break
        self.entries: List[_Entry] = []
        self._parse()

    @classmethod
    def load(cls, path, missing_ok: bool = False) -> "PropertiesFile":
        path = Path(path)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            if not missing_ok:
                raise
            data = b""
        return cls(path, _split_lines(data.decode("utf-8", "surrogateescape")))

    def _parse(self) -> None:
        self.entries = []
        index = 0
        while index < len(self.lines):
            content = _strip_eol(self.lines[index]).lstrip(" \t\f")
            if not content or content[0] in "#!":
                index += 1
                continue
            first = index
            logical = ""
            while True:
                piece = _strip_eol(self.lines[index]).lstrip(" \t\f")
                if _continues(piece) and index + 1 < len(self.lines):
                    logical += piece[:-1]
                    index += 1
                    continue
                if _continues(piece):
                    piece = piece[:-1]
                logical += piece
                break
            key, value = _split_key(logical)
            self.entries.append(_Entry(_unescape(key), _unescape(value), first, index))
            index += 1

    def get(self, key: str) -> Optional[str]:
        found = None
        for entry in self.entries:
            if entry.key == key:
                found = entry.value
        return found

    def set(self, key: str, value: str) -> None:
        line = f"{key}={_escape_value(value)}"
        matches = [e for e in self.entries if e.key == key]
        if matches:
            entry = matches[-1]
            eol = self.lines[entry.last][len(_strip_eol(self.lines[entry.last])):]
            self.lines[entry.first:entry.last + 1] = [line + eol]
        else:
            if self.lines and not self.lines[-1].endswith(("\n", "\r")):
                self.lines[-1] += self.newline
            self.lines.append(line + self.newline)
        self._parse()

    def text(self) -> str:
        return "".join(self.lines)

    def save(self) -> Optional[str]:
        """Write atomically keeping mode and owner; return a warning if others can read it."""
        data = self.text().encode("utf-8", "surrogateescape")
        try:
            st = os.stat(self.path)
            mode, uid, gid = stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid
        except FileNotFoundError:
            mode, uid, gid = 0o600, None, None
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=f".{self.path.name}.")
        try:
            os.write(fd, data)
            os.fchmod(fd, mode)
            if uid is not None:
                try:
                    os.fchown(fd, uid, gid)
                except PermissionError:
                    pass
            os.fsync(fd)
            os.close(fd)
            fd = -1
            faults.point("before_replace")
            os.replace(tmp, self.path)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)
        if mode & 0o077:
            return f"{self.path} is readable by other users (mode {mode:o}); consider chmod 600"
        return None
