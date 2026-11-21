"""Project paths and configuration, credentials, project lock, registry checks (spec §5.2, §6)."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import json
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

from kankan import clock, faults, gitutil
from kankan.errors import KankanError, Status
from kankan.properties import PropertiesFile

REGISTRY_START = "--- kankan-registry ---"
REGISTRY_END = "--- end kankan-registry ---"


def atomic_write_json(path: Path, obj, *, sort_keys: bool = True) -> None:
    """Write JSON to a temp file in the same directory, fsync, then rename over ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        os.write(fd, (json.dumps(obj, indent=2, sort_keys=sort_keys, default=str) + "\n").encode())
        os.fsync(fd)
        os.close(fd)
        fd = -1
        faults.point("before_replace")
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


@dataclass
class Paths:
    repo: Path
    gcd: Path
    kankan_dir: Path
    project: str
    worktree_root: Path

    @classmethod
    def discover(cls, cwd, raw: Optional[dict] = None) -> "Paths":
        proc = gitutil.run(cwd, ["rev-parse", "--path-format=absolute", "--git-common-dir"], check=False)
        if proc.returncode != 0:
            raise KankanError(Status.CONFIG_ERROR, f"{cwd} is not inside a Git repository")
        gcd = Path(proc.stdout.strip())
        first = gitutil.out(cwd, ["worktree", "list", "--porcelain"]).splitlines()[0]
        repo = Path(first.split(" ", 1)[1]) if first.startswith("worktree ") else gcd.parent
        kankan_dir = gcd / "kankan"
        if raw is None:
            raw = read_json(kankan_dir / "config.json", {}) or {}
        project = raw.get("project_name") or repo.name
        root = raw.get("worktree_root")
        worktree_root = Path(root) if root else Path(os.path.expanduser("~")) / "coding" / ".kankan" / project
        return cls(repo=repo, gcd=gcd, kankan_dir=kankan_dir, project=project, worktree_root=worktree_root)

    @property
    def seats_dir(self) -> Path:
        return self.kankan_dir / "seats"

    def seat_worktree(self, seat: str) -> Path:
        return self.worktree_root / seat

    def blocked_worktree(self, short: str) -> Path:
        return self.worktree_root / f"blocked-{short}"


@dataclass
class SeatConfig:
    type: str
    in_progress: str = ""
    implemented: str = ""
    review: str = ""
    role: str = "work"  # "work", or "qa" for the Q&A seat (it has no lists)


@dataclass
class Config:
    board_id: str
    board_short: str
    registry_id: str
    backlog: str
    done: str
    blocked: str
    label_hold: str
    label_question: str
    label_error: str
    test_command: str
    test_timeout: int = 1800
    integration_branch: str = "kankan-agents"
    user_branch: str = "main"
    push_main: bool = False
    credentials_path: Optional[str] = None
    credentials_keys: Tuple[str, str] = ("kankan.trello.key", "kankan.trello.token")
    seat_cap: int = 4
    poll_seconds: int = 120
    tail_lines: int = 30
    idle_ready_minutes: int = 10
    write_quiet_seconds: int = 20
    idle_exit_minutes: Optional[int] = None
    main_moved_retries: int = 5
    seats: Dict[str, SeatConfig] = field(default_factory=dict)
    project_name: Optional[str] = None
    worktree_root: Optional[str] = None
    trello_base_url: str = "https://api.trello.com"
    trello_timeout: float = 5.0
    settle_seconds: float = 3.0
    new_conversation: Dict[str, str] = field(default_factory=dict)
    kankan_member_id: str = ""  # kankan's own Trello account (the token owner)
    kankan_username: str = ""
    settings_list: Optional[str] = None
    settings_poll_seconds: int = 30
    attachment_max_bytes: int = 26214400  # per downloaded card attachment (spec §13.3)
    qa_list: Optional[str] = None  # the `Q&A` list; missing or archived is never a config_error
    qa_poll_seconds: int = 30

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["credentials_keys"] = list(self.credentials_keys)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        data = dict(data)
        if "main_branch" in data:  # pre-1.2 key: the agents integrated into this branch
            data.setdefault("integration_branch", data["main_branch"])
        known = {f.name for f in dataclasses.fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        data["seats"] = {name: SeatConfig(**seat) for name, seat in (data.get("seats") or {}).items()}
        if "credentials_keys" in data:
            data["credentials_keys"] = tuple(data["credentials_keys"])
        cfg = cls(**data)
        if isinstance(cfg.settings_poll_seconds, bool) or not isinstance(cfg.settings_poll_seconds, int) or cfg.settings_poll_seconds <= 0:
            raise ValueError("settings_poll_seconds must be a positive integer")
        if isinstance(cfg.qa_poll_seconds, bool) or not isinstance(cfg.qa_poll_seconds, int) or cfg.qa_poll_seconds <= 0:
            raise ValueError("qa_poll_seconds must be a positive integer")
        limit = cfg.attachment_max_bytes
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"attachment_max_bytes must be a positive integer, not {limit!r}")
        return cfg

    def seat_lists(self) -> Dict[str, str]:
        """Map every seat list id to the seat name."""
        result = {}
        for name, seat in self.work_seats().items():
            for list_id in (seat.in_progress, seat.implemented, seat.review):
                result[list_id] = name
        return result

    def work_seats(self) -> Dict[str, SeatConfig]:
        return {name: seat for name, seat in self.seats.items() if seat.role != "qa"}

    def qa_seat(self) -> Optional[str]:
        """Name of the registered Q&A seat, if any (at most one is registered)."""
        return next((name for name, seat in self.seats.items() if seat.role == "qa"), None)

    def workflow_lists(self) -> Dict[str, str]:
        result = {self.backlog: "Backlog", self.done: "Done", self.blocked: "Blocked"}
        for name, seat in self.work_seats().items():
            result[seat.in_progress] = f"{name} · In Progress"
            result[seat.implemented] = f"{name} · Implemented"
            result[seat.review] = f"{name} · Review"
        if self.qa_list:
            result[self.qa_list] = "Q&A"
        return result

    def required_lists(self) -> Dict[str, str]:
        """The workflow lists whose absence is a ``config_error`` (every one except ``Q&A``)."""
        return {list_id: name for list_id, name in self.workflow_lists().items() if list_id != self.qa_list}


def load_config(paths: Paths) -> Config:
    try:
        raw = read_json(paths.kankan_dir / "config.json", None)
    except ValueError as err:  # malformed JSON
        raise KankanError(Status.CONFIG_ERROR, f"invalid {paths.kankan_dir / 'config.json'}: {err}")
    if raw is None:
        raise KankanError(Status.CONFIG_ERROR, "kankan is not initialized in this repository: run `kankan init <board-url>`")
    try:
        return Config.from_dict(raw)
    except (TypeError, KeyError, ValueError) as err:
        raise KankanError(Status.CONFIG_ERROR, f"invalid {paths.kankan_dir / 'config.json'}: {err}")


def save_config(paths: Paths, cfg: Config) -> None:
    atomic_write_json(paths.kankan_dir / "config.json", cfg.to_dict())


def user_credentials_path() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "kankan" / "credentials"


class CredentialsNotFound(KankanError):
    """No complete Trello credential pair was found."""


def load_credentials(paths: Paths, cfg: Optional[Config]) -> Tuple[str, str]:
    """Credentials lookup (spec §6.2): environment, configured .properties file, user file."""
    key, token = os.environ.get("KANKAN_TRELLO_KEY"), os.environ.get("KANKAN_TRELLO_TOKEN")
    if key and token:
        return key, token
    key_name, token_name = cfg.credentials_keys if cfg else ("kankan.trello.key", "kankan.trello.token")
    sources = []
    if cfg and cfg.credentials_path:
        sources.append(Path(cfg.credentials_path))
    sources.append(user_credentials_path())
    for source in sources:
        if source.exists():
            props = PropertiesFile.load(source)
            key, token = props.get(key_name), props.get(token_name)
            if key and token:
                return key, token
    raise CredentialsNotFound(Status.CONFIG_ERROR, "Trello credentials not found: run `kankan init --reauth`")


_LOCK_DEPTH: Dict[str, list] = {}
_LOCK_GUARD = threading.Lock()


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 60, what: str = "lock", nofollow: bool = False) -> Iterator[None]:
    """Exclusive flock with a polling deadline; reentrant within one process. The file is never deleted.

    With ``nofollow`` (``kankan destroy``) neither the lock file nor its directory may be a symbolic link and the
    lock must be a regular file: both are opened with ``O_NOFOLLOW``, the file relative to its directory's
    descriptor, and anything else raises ``OSError`` before a file is created anywhere."""
    key = str(path)
    with _LOCK_GUARD:
        entry = _LOCK_DEPTH.get(key)
        if entry is not None and entry[1] == threading.get_ident():
            entry[2] += 1
            reentered = True
        else:
            reentered = False
    if reentered:
        try:
            yield
        finally:
            with _LOCK_GUARD:
                _LOCK_DEPTH[key][2] -= 1
        return
    if nofollow:
        fd = open_nofollow(Path(path), os.O_RDWR | os.O_CREAT)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    deadline = clock.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock.monotonic() >= deadline:
                    raise KankanError(Status.INTERNAL_ERROR, f"timed out waiting for the {what} ({path})")
                import time
                time.sleep(0.05)
        with _LOCK_GUARD:
            _LOCK_DEPTH[key] = [fd, threading.get_ident(), 1]
        try:
            yield
        finally:
            with _LOCK_GUARD:
                del _LOCK_DEPTH[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def open_nofollow(path: Path, flags: int) -> int:
    """Open ``path`` without following a symbolic link at its last two components: the parent directory is created
    if absent (never through a link) and opened with ``O_DIRECTORY | O_NOFOLLOW``, the file relative to it with
    ``O_NOFOLLOW | O_NONBLOCK`` (a FIFO never blocks). Anything but a regular file raises ``OSError``."""
    path = Path(path)
    try:
        os.mkdir(path.parent, 0o700)
    except FileExistsError:
        pass
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def project_lock(paths: Paths, timeout: float = 60, nofollow: bool = False):
    return file_lock(paths.kankan_dir / "project.lock", timeout, "project lock", nofollow=nofollow)


def registry_block(desc: str) -> Optional[dict]:
    match = re.search(re.escape(REGISTRY_START) + r"\n(.*?)\n?" + re.escape(REGISTRY_END), desc or "", re.S)
    if not match:
        return None
    result = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            result[name.strip()] = value.strip()
    return result


def write_registry_block(desc: str, fields: dict) -> str:
    """Replace (or append) only the delimited registry block in a board description."""
    block = REGISTRY_START + "\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + REGISTRY_END
    pattern = re.compile(re.escape(REGISTRY_START) + r".*?" + re.escape(REGISTRY_END), re.S)
    if pattern.search(desc or ""):
        return pattern.sub(lambda _: block, desc, count=1)
    desc = desc or ""
    if desc and not desc.endswith("\n"):
        desc += "\n"
    if desc:
        desc += "\n"
    return desc + block + "\n"


def registry_state(desc: str) -> Tuple[str, Optional[str]]:
    """Classify the registry block of a board description: absent, valid (with its registry-id) or malformed."""
    desc = desc or ""
    starts, ends = desc.count(REGISTRY_START), desc.count(REGISTRY_END)
    if starts == 0 and ends == 0:
        return "absent", None
    if starts != 1 or ends != 1 or desc.index(REGISTRY_START) > desc.index(REGISTRY_END):
        return "malformed", None
    match = re.search(re.escape(REGISTRY_START) + r"\n(.*?)\n?" + re.escape(REGISTRY_END), desc, re.S)
    ids = [line.split(":", 1)[1].strip() for line in (match.group(1).splitlines() if match else [])
           if ":" in line and line.split(":", 1)[0].strip() == "registry-id"]
    # one registry-id line, never two (even equal): a second one makes the owner ambiguous
    return ("valid", ids[0]) if len(ids) == 1 and ids[0] else ("malformed", None)


def remove_registry_block(desc: str) -> str:
    """Inverse of ``write_registry_block``: drop the block and, for a block at the end, the separator it added.

    A block in the middle is removed alone, leaving the text around it byte for byte.
    """
    desc = desc or ""
    match = re.search(re.escape(REGISTRY_START) + r".*?" + re.escape(REGISTRY_END), desc, re.S)
    if not match:
        return desc
    before, after = desc[:match.start()], desc[match.end():]
    if after not in ("", "\n"):
        return before + after
    return before[:-2] if before.endswith("\n\n") else before


def check_registry(cfg: Config, desc: str) -> None:
    block = registry_block(desc)
    if block is None:
        raise KankanError(Status.CONFIG_ERROR, "the board description has no kankan-registry block: run `kankan init`")
    if block.get("registry-id") != cfg.registry_id:
        raise KankanError(Status.CONFIG_ERROR,
                          "the board is bound to another kankan registry (another clone ran `kankan init --takeover`)",
                          {"board_registry": block.get("registry-id")})


def check_lists(cfg: Config, lists: dict) -> None:
    for list_id, name in cfg.required_lists().items():
        lst = lists.get(list_id)
        if lst is None or lst.closed:
            raise KankanError(Status.CONFIG_ERROR, f"workflow list {name!r} ({list_id}) is archived or deleted",
                              {"list": list_id})


def qa_list_open(cfg: Config, lists: dict) -> bool:
    """Whether ``qa_list`` is configured and open on the board (``lists``: id -> TList)."""
    lst = lists.get(cfg.qa_list) if cfg.qa_list else None
    return lst is not None and not lst.closed


def _check_fields(raw: dict) -> None:
    """Reject wrong types in the fields ``try_load`` consumes before any of them is acted on."""
    from kankan import messages

    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    checks = {"board_id": lambda v: isinstance(v, str), "trello_base_url": lambda v: isinstance(v, str),
              "trello_timeout": number, "worktree_root": lambda v: v is None or isinstance(v, str),
              "project_name": lambda v: v is None or isinstance(v, str)}
    for field, valid in checks.items():
        if field in raw and not valid(raw[field]):
            if field == "board_id":
                raise ValueError(messages.CONFIG_BOARD_ID_TYPE)
            raise ValueError(messages.CONFIG_FIELD_INVALID.format(field=field))
    # every list ID destroy deletes cards from or archives (destroy T§3): a string, or absent/null
    for field in ("backlog", "done", "blocked", "qa_list", "settings_list"):
        if field in raw and not _optional_str(raw[field]):
            raise ValueError(messages.CONFIG_FIELD_INVALID.format(field=field))
    seats = raw.get("seats")
    if seats is None:
        return
    if not isinstance(seats, dict):
        raise ValueError(messages.CONFIG_FIELD_INVALID.format(field="seats"))
    for name, seat in seats.items():
        if not isinstance(seat, dict):
            raise ValueError(messages.CONFIG_FIELD_INVALID.format(field=f"seats.{name}"))
        for key in ("in_progress", "implemented", "review", "role"):
            if key in seat and not _optional_str(seat[key]):
                raise ValueError(messages.CONFIG_FIELD_INVALID.format(field=f"seats.{name}.{key}"))


def _optional_str(value) -> bool:
    return value is None or isinstance(value, str)


def try_load(cwd) -> Tuple[Paths, Optional[Config], Optional[str]]:
    """Paths and configuration of the repository at ``cwd`` without ever failing on ``config.json``.

    Returns ``(paths, cfg, reason)``: ``cfg`` is None with the reason when the file is missing, unreadable,
    not an object or malformed; the paths then use their defaults, and the file's ``project_name`` and
    ``worktree_root`` apply only once the configuration has validated. A configuration without a ``board_id``
    is returned with the reason ``messages.CONFIG_NO_BOARD``; every other required field keeps the normal
    loader's requirements. Only a ``cwd`` outside a Git repository raises.
    """
    from kankan import messages
    paths = Paths.discover(cwd, raw={})
    path = paths.kankan_dir / "config.json"
    try:
        raw = read_json(path, None)
    except (ValueError, OSError) as err:
        return paths, None, messages.CONFIG_INVALID.format(path=path, error=err)
    if raw is None:
        return paths, None, messages.CONFIG_NOT_INITIALIZED
    try:
        if not isinstance(raw, dict):
            raise ValueError(messages.CONFIG_NOT_OBJECT)
        _check_fields(raw)
        data = dict(raw)
        if not data.get("board_id"):
            required = {f.name: "" for f in dataclasses.fields(Config)
                        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
            data = {**required, **raw}
        cfg = Config.from_dict(data)
        resolved = Paths.discover(cwd, raw=raw)
    except (ValueError, TypeError, AttributeError, KeyError) as err:
        return paths, None, messages.CONFIG_INVALID.format(path=path, error=err)
    return resolved, cfg, None if cfg.board_id else messages.CONFIG_NO_BOARD
