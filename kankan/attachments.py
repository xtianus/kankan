"""Card attachments as task inputs (spec §13.3): classification, URL validation, local copies, pruning.

Policy only: ``trello.TrelloClient`` lists the attachments and transfers the bytes. The local copies live in
``<gcd>/kankan/attachments/<shortLink>/<attachment-id>/<sanitized file name>``. Every change inside a card's
directory happens under the card attachment lock (``<gcd>/kankan/attachment-locks/<shortLink>.lock``, never
deleted), taken after the project lock when both are needed; ``task`` never takes the project lock. Nothing
here follows a symlink: the directories and the cached file are checked with ``os.lstat`` before any use, and
deletion unlinks a symlink instead of descending into it.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from kankan import auditlog, clock, config, deps, notify
from kankan.config import Paths
from kankan.errors import KankanError, Status
from kankan.trello import Attachment, Card, DownloadTooLarge, _origin

DOWNLOAD_BUDGET_SECONDS = 60  # no download starts later than this after the start of `for_task`
LOCK_MARGIN_SECONDS = 30  # a waiting call gives up on the card attachment lock after budget + margin

_SHORT = re.compile(r"[A-Za-z0-9]{8}")
_ATT_ID = re.compile(r"[0-9a-f]{24}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TRELLO_ORIGIN = ("https", "trello.com", 443)
_KEYS = ("id", "name", "kind", "url", "mime_type", "bytes", "path", "status", "message")

NOT_MEMBER = "added by someone who is not a board member (or by kankan)"
UNEXPECTED_URL = "the URL is not Trello's download URL for this card's attachment: not fetched"
NOT_YET = "not downloaded yet: run `kankan task` again"
UNSAFE = "unsafe attachment path"
INVALID_ID = "invalid attachment id"
NOT_HELD = "the card of the current task is no longer held by this seat"


def root(paths: Paths) -> Path:
    return paths.kankan_dir / "attachments"


def _valid_short(short) -> bool:
    return isinstance(short, str) and _SHORT.fullmatch(short) is not None


def card_dir(paths: Paths, short: str) -> Path:
    if not _valid_short(short):
        raise KankanError(Status.INTERNAL_ERROR, f"invalid card shortLink {short!r}")
    return root(paths) / short


def card_lock(paths: Paths, short: str, timeout: float):
    """The card attachment lock: a context manager; raises ``KankanError`` when not taken within ``timeout``."""
    card_dir(paths, short)  # validates ``short`` before it becomes a file name
    return config.file_lock(paths.kankan_dir / "attachment-locks" / f"{short}.lock", timeout, "card attachment lock")


def sanitize(name: str) -> str:
    """A local file name from Trello's: control characters, path separators, ``..`` and leading dots replaced."""
    name = _CONTROL.sub("_", name)
    name = name.replace("/", "_").replace("\\", "_")
    name = name.replace("..", "_")
    stripped = name.lstrip(".")
    name = "_" * (len(name) - len(stripped)) + stripped
    return name or "file"


def download_path(base_url: str, url: str, card_id: str, att_id: str) -> Optional[str]:
    """The API path to download ``url`` from, or None when it is not this attachment's Trello download URL.

    Accepted: ``https://trello.com`` or the exact origin of ``base_url``, no user info, query or fragment, and
    the path ``/1/cards/<card_id>/attachments/<att_id>/download/<one segment>`` (returned still percent-encoded).
    """
    try:
        parts = urllib.parse.urlsplit(url or "")
        has_userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        return None
    origin = _origin(url or "")
    if origin is None or origin not in (_TRELLO_ORIGIN, _origin(base_url)):
        return None
    if has_userinfo or parts.query or parts.fragment:
        return None
    segments = parts.path.split("/")
    if segments[:-1] != ["", "1", "cards", card_id, "attachments", att_id, "download"]:
        return None
    if segments[-1] in ("", ".", ".."):
        return None
    return parts.path


def directories(paths: Paths) -> List[str]:
    """The names under the attachments root (none when it is missing or not a real directory)."""
    try:
        top = _lstat(root(paths))
    except OSError:
        return []
    if top is None or not stat.S_ISDIR(top.st_mode):
        return []
    try:
        return sorted(os.listdir(root(paths)))
    except OSError as err:  # unreadable, or replaced by a non-directory meanwhile
        auditlog.append(paths, "attachments_root_unreadable", path=str(root(paths)), error=_os_message(err))
        return []


def discard(paths: Paths, short: str) -> bool:
    """Delete a card's attachment directory without waiting: False (and a log entry) when the card attachment
    lock is busy, ``short`` is invalid, or the deletion fails. A busy lock means a ``task`` is downloading; it
    deletes the directory itself when it finds the card ref gone."""
    if not _valid_short(short):
        auditlog.append(paths, "attachments_discard_invalid", short=str(short))
        return False
    try:
        with card_lock(paths, short, 0):
            return _remove_card_dir(paths, short)
    except KankanError:
        auditlog.append(paths, "attachments_discard_busy", short=short)
    except OSError as err:
        auditlog.append(paths, "attachments_discard_failed", short=short, error=_os_message(err))
    return False


def for_task(project, card: Card, still_held: Callable[[], bool]) -> List[dict]:
    """The ``attachments`` field of ``kankan task``: one entry per non-dependency attachment, in the card's order.

    Reads the attachments and the board members without a lock (their failures raise), then, under the card
    attachment lock and between two ``still_held()`` checks, serves cached copies, downloads the missing files
    and prunes every copy that is not a file with status ``ok``. When ``still_held()`` is False the card's
    directory is deleted and ``KankanError(INELIGIBLE)`` raised.
    """
    budget = DOWNLOAD_BUDGET_SECONDS
    deadline = clock.monotonic() + budget
    max_bytes = project.cfg.attachment_max_bytes
    short_ok = _valid_short(card.short)
    members = None
    result: List[dict] = []
    files = []  # (entry, attachment, api path) still to serve from the cache or download
    listed_at = time.time()  # wall clock, comparable with st_mtime: copies published later are not pruned
    for att in project.trello.attachments(card.id):
        if deps.attachment_short(att.url):
            continue
        entry = {"id": att.id, "name": att.name, "kind": "file" if att.is_upload else "link", "url": att.url}
        if att.is_upload:
            entry.update(mime_type=att.mime_type, bytes=att.bytes)
        result.append(entry)
        if members is None:
            members = notify.input_members(project)
        if att.member_id not in members:
            del entry["url"]
            _set(entry, "not_member", NOT_MEMBER)
        elif not att.is_upload:
            _set(entry, "ok")
        elif not short_ok or not isinstance(att.id, str) or not _ATT_ID.fullmatch(att.id):
            _set(entry, "error", INVALID_ID)
        else:
            api_path = download_path(project.trello.base_url, att.url, card.id, att.id)
            if api_path is None:
                _set(entry, "unexpected_url", UNEXPECTED_URL)
            else:
                files.append((entry, att, api_path))
    if short_ok:
        with card_lock(project.paths, card.short, budget + LOCK_MARGIN_SECONDS):
            _require_held(project, card.short, still_held)
            for entry, att, api_path in files:
                _serve(project, card.short, entry, att, api_path, max_bytes, deadline)
            _prune(project, card.short, {e["id"]: Path(e["path"]).name for e in result if "path" in e},
                   {att.id for _, att, _ in files}, listed_at)
            _require_held(project, card.short, still_held)
    return [{key: entry[key] for key in _KEYS if key in entry} for entry in result]


# ---------------------------------------------------------------- internals

def _set(entry: dict, status: str, message: Optional[str] = None) -> None:
    entry["status"] = status
    if message is not None:
        entry["message"] = message


def _too_large(max_bytes: int) -> str:
    return f"larger than attachment_max_bytes ({max_bytes} bytes): not downloaded"


def _os_message(err: OSError) -> str:
    return f"cannot write the local copy: {err.strerror or type(err).__name__}"


def _lstat(path: Path) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _real_dirs(*dirs: Path) -> bool:
    """True when each of ``dirs`` (outermost first) is missing or a real directory, never a symlink."""
    for path in dirs:
        info = _lstat(path)
        if info is None:
            return True
        if not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _remove(path: Path) -> None:
    """Delete ``path`` without following symlinks: a real directory with ``shutil.rmtree`` (which unlinks the
    symlinks inside it), anything else with ``os.unlink``."""
    info = _lstat(path)
    if info is None:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _remove_card_dir(paths: Paths, short: str) -> bool:
    if not _real_dirs(root(paths)):
        auditlog.append(paths, "attachments_unsafe_root", path=str(root(paths)))
        return False
    path = card_dir(paths, short)
    if _lstat(path) is not None:
        _remove(path)
        auditlog.append(paths, "attachments_discarded", short=short)
    return True


def _require_held(project, short: str, still_held: Callable[[], bool]) -> None:
    if still_held():
        return
    try:
        _remove_card_dir(project.paths, short)
    except OSError as err:
        project.log("attachments_discard_failed", short=short, error=_os_message(err))
    raise KankanError(Status.INELIGIBLE, NOT_HELD)


def _serve(project, short: str, entry: dict, att: Attachment, api_path: str, max_bytes: int,
           deadline: float) -> None:
    """Give ``entry`` its cached copy, or download it; failures become the entry's status and are logged."""
    try:
        path = _cached_or_downloaded(project, short, att, api_path, max_bytes, deadline)
        entry["path"] = str(path)
        _set(entry, "ok")
    except _NotServed as err:
        _set(entry, "error", str(err))
    except DownloadTooLarge:
        _set(entry, "too_large", _too_large(max_bytes))
    except KankanError as err:
        _set(entry, "error", err.message)
    except OSError as err:
        _set(entry, "error", _os_message(err))
    if entry["status"] != "ok":
        project.log("attachment_not_served", card=short, attachment=att.id, status=entry["status"],
                    error=entry["message"])


class _NotServed(Exception):
    """A file left without a local copy for a reason other than the download itself."""


def _cached_or_downloaded(project, short: str, att: Attachment, api_path: str, max_bytes: int,
                          deadline: float) -> Path:
    card_path = card_dir(project.paths, short)
    att_path = card_path / att.id
    final = att_path / sanitize(att.file_name or att.name)
    if not _real_dirs(root(project.paths), card_path, att_path):
        raise _NotServed(UNSAFE)
    info = _lstat(final)
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise _NotServed(UNSAFE)
    if att.bytes is not None and att.bytes > max_bytes:
        raise DownloadTooLarge()
    if info is not None:
        reusable = info.st_size == att.bytes if att.bytes is not None else info.st_size <= max_bytes
        if reusable:
            return final
    if clock.monotonic() >= deadline:
        raise _NotServed(NOT_YET)
    os.makedirs(att_path, mode=0o700, exist_ok=True)
    _download(project, api_path, att, att_path, final, max_bytes, deadline)
    return final


def _download(project, api_path: str, att: Attachment, att_path: Path, final: Path, max_bytes: int,
              deadline: float) -> None:
    """Download into ``.<random>.part`` beside ``final`` and rename it over ``final``; the temporary file is
    removed on every failure."""
    fd, tmp = tempfile.mkstemp(dir=str(att_path), prefix=".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out:
            size = project.trello.download(api_path, out, max_bytes, deadline)
        if att.bytes is not None and size != att.bytes:
            raise KankanError(Status.TRELLO_ERROR, f"downloaded {size} bytes where Trello reports {att.bytes}")
        os.replace(tmp, final)
        tmp = None
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _prune(project, short: str, keep: Dict[str, str], served: Set[str], listed_at: float) -> None:
    """Remove from the card's directory every ``<att-id>`` directory not in ``keep`` (attachment id → file name)
    and, inside the kept ones, every entry but the file. Failures are logged, never raised.

    A directory neither in ``keep`` nor ``served`` (handled by this call under the lock) but changed after
    ``listed_at`` (the wall-clock time before this call listed the attachments and members) is left alone: a
    call with a newer listing published that copy and may have returned its path; the next call prunes it if
    it is still stale."""
    card_path = card_dir(project.paths, short)
    try:
        if not _real_dirs(root(project.paths), card_path) or _lstat(card_path) is None:
            return
        names = os.listdir(card_path)
    except OSError as err:
        project.log("attachments_prune_failed", card=short, path=str(card_path), error=_os_message(err))
        return
    for name in names:
        path = card_path / name
        try:
            if name not in keep:
                info = _lstat(path)
                if name not in served and info is not None and info.st_mtime > listed_at:
                    continue
                _remove(path)
                continue
            for child in os.listdir(path):
                if child != keep[name]:
                    _remove(path / child)
        except OSError as err:
            project.log("attachments_prune_failed", card=short, path=str(path), error=_os_message(err))
