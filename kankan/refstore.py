"""The only module that reads and writes kankan refs (spec §10.1).

Refs under ``refs/agent-cards/`` and ``refs/agent-seats/`` point to immutable JSON
blobs. Every change is a compare-and-swap ``git update-ref`` or an atomic
``git update-ref --stdin`` transaction.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

from kankan import gitutil
from kankan.errors import KankanError, Status

ZERO = "0" * 40
CARD_PREFIX = "refs/agent-cards/"
SEAT_PREFIX = "refs/agent-seats/"

_CARD_RE = re.compile(r"^[0-9a-f]{24}$")
_SHORT_RE = re.compile(r"^[A-Za-z0-9]{8}$")
_SEAT_RE = re.compile(r"^[a-z]+-[0-9]+$")
_MISMATCH = ("cannot lock ref", "but expected", "reference already exists", "unable to resolve reference",
             "is at", "already exists")


def _validate(regex: re.Pattern, value: str, what: str) -> str:
    if not isinstance(value, str) or not regex.match(value):
        raise KankanError(Status.USAGE_ERROR, f"invalid {what}: {value!r}")
    return value


def validate_card_id(value: str) -> str:
    return _validate(_CARD_RE, value, "card id")


def validate_short_link(value: str) -> str:
    return _validate(_SHORT_RE, value, "shortLink")


def validate_seat(value: str) -> str:
    return _validate(_SEAT_RE, value, "seat")


def card_ref(card_id: str) -> str:
    return CARD_PREFIX + validate_card_id(card_id)


def seat_ref(seat: str) -> str:
    return SEAT_PREFIX + validate_seat(seat)


@dataclass
class RefValue:
    oid: str
    meta: dict


@dataclass
class Verify:
    ref: str
    oid: Optional[str]  # None: the ref must not exist


@dataclass
class Create:
    ref: str
    oid: str


@dataclass
class Update:
    ref: str
    new: str
    old: str


@dataclass
class Delete:
    ref: str
    old: str


@dataclass
class Main:
    new: str
    old: str


Op = Union[Verify, Create, Update, Delete, Main]


def _check_ref(ref: str) -> str:
    if ref.startswith(CARD_PREFIX):
        validate_card_id(ref[len(CARD_PREFIX):])
    elif ref.startswith(SEAT_PREFIX):
        validate_seat(ref[len(SEAT_PREFIX):])
    else:
        raise KankanError(Status.INTERNAL_ERROR, f"not a kankan ref: {ref}")
    return ref


class RefStore:
    def __init__(self, repo: Path, main_branch: str):
        self.repo = Path(repo)
        self.main_branch = main_branch

    def write_blob(self, meta: dict) -> str:
        meta = dict(meta)
        meta.setdefault("nonce", secrets.token_hex(16))
        text = json.dumps(meta, sort_keys=True, separators=(",", ":"))
        return gitutil.out(self.repo, ["hash-object", "-w", "--stdin"], input=text)

    def _parse(self, ref: str, oid: str, kind: str, body: str) -> RefValue:
        if kind != "blob":
            raise KankanError(Status.NEEDS_RECONCILIATION, f"{ref} points to a {kind}, not a blob", {"ref": ref})
        try:
            meta = json.loads(body)
        except ValueError:
            raise KankanError(Status.NEEDS_RECONCILIATION, f"{ref} points to a malformed blob", {"ref": ref})
        if not isinstance(meta, dict) or meta.get("schema") != 1:
            raise KankanError(Status.NEEDS_RECONCILIATION, f"{ref} has an unknown schema", {"ref": ref})
        return RefValue(oid, meta)

    def read(self, ref: str) -> Optional[RefValue]:
        _check_ref(ref)
        return self.list(ref).get(ref)

    def list(self, prefix: str) -> Dict[str, RefValue]:
        """Read every ref under ``prefix`` (or the exact ref); malformed ones raise NEEDS_RECONCILIATION."""
        raw = self.list_raw(prefix)
        return {ref: self._parse(ref, oid, kind, body) for ref, (oid, kind, body) in raw.items()}

    def list_raw(self, prefix: str) -> Dict[str, tuple]:
        """Like ``list`` but returns ``(oid, type, text)`` without validation (for diagnostics)."""
        lines = gitutil.out(self.repo, ["for-each-ref", "--format=%(refname) %(objectname) %(objecttype)", prefix])
        entries = [line.split(" ") for line in lines.splitlines() if line]
        if not prefix.endswith("/"):
            entries = [e for e in entries if e[0] == prefix]
        if not entries:
            return {}
        batch = subprocess.run(["git", "-C", str(self.repo), "cat-file", "--batch"],
                               input="".join(f"{oid}\n" for _, oid, _ in entries).encode(),
                               capture_output=True, check=True).stdout
        result = {}
        pos = 0
        for ref, oid, kind in entries:
            header_end = batch.index(b"\n", pos)
            size = int(batch[pos:header_end].split(b" ")[2])
            body = batch[header_end + 1:header_end + 1 + size]
            pos = header_end + 1 + size + 1
            result[ref] = (oid, kind, body.decode("utf-8", "replace") if kind == "blob" else "")
        return result

    def create(self, ref: str, oid: str) -> None:
        """Create ``ref``; a ref already at ``oid`` is our own earlier success, else ALREADY_CLAIMED."""
        _check_ref(ref)
        proc = gitutil.run(self.repo, ["update-ref", ref, oid, ZERO], check=False)
        if proc.returncode == 0:
            return
        current = gitutil.rev(self.repo, ref)
        if current == oid:
            return
        if current is None:
            raise KankanError(Status.INTERNAL_ERROR, f"cannot create {ref}: {proc.stderr.strip()}")
        raise KankanError(Status.ALREADY_CLAIMED, f"{ref} already exists", {"ref": ref})

    def update(self, ref: str, new: str, old: str) -> bool:
        return self.transaction([Update(ref, new, old)])

    def delete(self, ref: str, old: str) -> bool:
        return self.transaction([Delete(ref, old)])

    def transaction(self, ops: List[Op]) -> bool:
        """Apply all ops atomically; False only on a value mismatch or a locked ref."""
        lines = ["start"]
        for op in ops:
            if isinstance(op, Verify):
                lines.append(f"verify {_check_ref(op.ref)} {op.oid or ZERO}")
            elif isinstance(op, Create):
                lines.append(f"create {_check_ref(op.ref)} {op.oid}")
            elif isinstance(op, Update):
                lines.append(f"update {_check_ref(op.ref)} {op.new} {op.old}")
            elif isinstance(op, Delete):
                lines.append(f"delete {_check_ref(op.ref)} {op.old}")
            elif isinstance(op, Main):
                lines.append(f"update refs/heads/{self.main_branch} {op.new} {op.old}")
            else:
                raise KankanError(Status.INTERNAL_ERROR, f"unknown op {op!r}")
        lines += ["prepare", "commit", ""]
        proc = gitutil.run(self.repo, ["update-ref", "--stdin"], input="\n".join(lines), check=False)
        if proc.returncode == 0:
            return True
        if any(marker in proc.stderr for marker in _MISMATCH):
            return False
        raise KankanError(Status.INTERNAL_ERROR, f"ref transaction failed: {proc.stderr.strip()}")
