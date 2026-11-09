"""Project paths and configuration, credentials, project lock, registry checks (spec §5.2, §6)."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import re
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


def atomic_write_json(path: Path, obj) -> None:
    """Write JSON to a temp file in the same directory, fsync, then rename over ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        os.write(fd, (json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n").encode())
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
    def discover(cls, cwd) -> "Paths":
        proc = gitutil.run(cwd, ["rev-parse", "--path-format=absolute", "--git-common-dir"], check=False)
        if proc.returncode != 0:
            raise KankanError(Status.CONFIG_ERROR, f"{cwd} is not inside a Git repository")
        gcd = Path(proc.stdout.strip())
        first = gitutil.out(cwd, ["worktree", "list", "--porcelain"]).splitlines()[0]
        repo = Path(first.split(" ", 1)[1]) if first.startswith("worktree ") else gcd.parent
        kankan_dir = gcd / "kankan"
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
    in_progress: str
    implemented: str
    review: str


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
    attachment_max_bytes: int = 26214400  # per downloaded card attachment (spec §13.3)

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
        return cls(**data)

    def seat_lists(self) -> Dict[str, str]:
        """Map every seat list id to the seat name."""
        result = {}
        for name, seat in self.seats.items():
            for list_id in (seat.in_progress, seat.implemented, seat.review):
                result[list_id] = name
        return result

    def workflow_lists(self) -> Dict[str, str]:
        result = {self.backlog: "Backlog", self.done: "Done", self.blocked: "Blocked"}
        for name, seat in self.seats.items():
            result[seat.in_progress] = f"{name} · In Progress"
            result[seat.implemented] = f"{name} · Implemented"
            result[seat.review] = f"{name} · Review"
        return result


def load_config(paths: Paths) -> Config:
    raw = read_json(paths.kankan_dir / "config.json", None)
    if raw is None:
        raise KankanError(Status.CONFIG_ERROR, "kankan is not initialized in this repository: run `kankan init <board-url>`")
    try:
        return Config.from_dict(raw)
    except (TypeError, KeyError) as err:
        raise KankanError(Status.CONFIG_ERROR, f"invalid {paths.kankan_dir / 'config.json'}: {err}")


def save_config(paths: Paths, cfg: Config) -> None:
    atomic_write_json(paths.kankan_dir / "config.json", cfg.to_dict())


def user_credentials_path() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "kankan" / "credentials"


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
    raise KankanError(Status.CONFIG_ERROR, "Trello credentials not found: run `kankan init --reauth`")


_LOCK_DEPTH: Dict[str, list] = {}
_LOCK_GUARD = threading.Lock()


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 60, what: str = "lock") -> Iterator[None]:
    """Exclusive flock with a polling deadline; reentrant within one process. The file is never deleted."""
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


def project_lock(paths: Paths, timeout: float = 60):
    return file_lock(paths.kankan_dir / "project.lock", timeout, "project lock")


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


def check_registry(cfg: Config, desc: str) -> None:
    block = registry_block(desc)
    if block is None:
        raise KankanError(Status.CONFIG_ERROR, "the board description has no kankan-registry block: run `kankan init`")
    if block.get("registry-id") != cfg.registry_id:
        raise KankanError(Status.CONFIG_ERROR,
                          "the board is bound to another kankan registry (another clone ran `kankan init --takeover`)",
                          {"board_registry": block.get("registry-id")})


def check_lists(cfg: Config, lists: dict) -> None:
    for list_id, name in cfg.workflow_lists().items():
        lst = lists.get(list_id)
        if lst is None or lst.closed:
            raise KankanError(Status.CONFIG_ERROR, f"workflow list {name!r} ({list_id}) is archived or deleted",
                              {"list": list_id})
