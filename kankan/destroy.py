"""``kankan destroy``: remove everything kankan created for a project (destroy spec)."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

from kankan import agents_cmd, auditlog, gitutil, init, messages, seats, stopall, worktrees
from kankan.config import (Config, Paths, load_credentials, project_lock, read_json, registry_state,
                           remove_registry_block, try_load)
from kankan.errors import KankanError, Status
from kankan.labels import resume_time
from kankan.refstore import CARD_PREFIX, SEAT_PREFIX, RefStore, validate_seat
from kankan.tmux import Tmux
from kankan.trello import TrelloClient

DEFAULT_INTEGRATION_BRANCH = "kankan-agents"
SEAT_FILE_SUFFIXES = tuple(sorted((".lock", ".state.json", ".proc.json", ".state.lock", ".block.lock"),
                                  key=len, reverse=True))


# --- Context ------------------------------------------------------------------------------------------------

@dataclass
class Ctx:
    """What destroy knows about a project; duck-types ``Project`` (``paths``, ``refs``, ``cfg``) for helpers."""
    paths: Paths
    cfg: Optional[Config]
    reason: Optional[str]
    refs: RefStore
    trello: Optional[TrelloClient]
    trello_reason: Optional[str]
    # why the integration branch name is the fallback; local_plan raises it, so no command names the fallback
    integration_error: Optional[KankanError] = None
    # <gcd>/kankan is a symbolic link or not a directory: nothing was read through it; destroy raises it first
    state_error: Optional[KankanError] = None

    @classmethod
    def open(cls, cwd) -> "Ctx":
        """Never raises for configuration or credential problems (only outside a Git repository)."""
        paths = Paths.discover(Path(cwd), raw={})
        state_error = state_root_error(paths)
        if state_error is not None:
            cfg, reason = None, state_error.message  # config.json is never read through a link
        else:
            paths, cfg, reason = try_load(Path(cwd))
        branch = _configured_branch(cfg)
        integration_error = None
        if branch is None:
            proc = _read_git_config(paths)
            try:
                configured = _git_config_result(paths, proc)
            except KankanError as err:
                # a failed read or a value that is not UTF-8: the fallback is only good for the gate's helpers
                configured, integration_error = None, err
            branch = configured or DEFAULT_INTEGRATION_BRANCH
        trello, trello_reason = None, None
        if cfg is None or not cfg.board_id:
            trello_reason = reason
        else:
            try:
                key, token = load_credentials(paths, cfg)
                trello = TrelloClient(key, token, base_url=cfg.trello_base_url, timeout=cfg.trello_timeout)
            except (KankanError, OSError, ValueError, TypeError, AttributeError) as err:
                trello_reason = str(getattr(err, "message", err))
        return cls(paths=paths, cfg=cfg, reason=reason, refs=RefStore(paths.repo, branch),
                   trello=trello, trello_reason=trello_reason, integration_error=integration_error,
                   state_error=state_error)

    @property
    def usable(self) -> bool:
        """True when ``Project.open`` would succeed (a configuration naming a board and loadable credentials),
        so `kankan stop` and its siblings can run."""
        return self.trello is not None


# --- State files destroy opens -------------------------------------------------------------------------------

def state_root_error(paths: Paths) -> Optional[KankanError]:
    """Why ``<gcd>/kankan`` cannot be used without following a link: a symbolic link or not a directory (absent is
    fine). Checked before anything reads through it, the configuration included."""
    kankan_dir = paths.kankan_dir
    try:
        mode = os.lstat(kankan_dir).st_mode
    except FileNotFoundError:
        return None
    except OSError as err:
        return _unreadable(kankan_dir, err)
    if stat.S_ISLNK(mode):
        # reading through it would inventory, and offer for deletion, what it points to (e.g. ~/.config/kankan)
        return KankanError(Status.NEEDS_ACTION, messages.DESTROY_STATE_SYMLINK.format(
            path=kankan_dir, quoted=shlex.quote(str(kankan_dir))), {"path": str(kankan_dir)})
    if not stat.S_ISDIR(mode):
        return KankanError(Status.NEEDS_ACTION, messages.DESTROY_STATE_NOT_DIR.format(path=_show(kankan_dir)),
                           {"path": str(kankan_dir)})
    return None


def _check_state_paths(paths: Paths, seat_names: Callable[[], List[str]]) -> None:
    """Every file destroy opens in ``<gcd>/kankan`` must be absent or a regular file reached without a link: the
    state root, ``seats/``, ``project.lock``, each seat's liveness lock and process record (the gate opens them)
    and ``log.jsonl`` (also a single link). NEEDS_ACTION otherwise, before any question and again under the lock.
    ``seat_names`` is called once ``seats/`` is known not to be a link, since listing the seats reads it."""
    error = state_root_error(paths)
    if error is not None:
        raise error
    seats_dir = paths.seats_dir
    try:
        mode = os.lstat(seats_dir).st_mode
    except FileNotFoundError:
        mode = None
    except OSError as err:
        raise _unreadable(seats_dir, err)
    if mode is not None and not stat.S_ISDIR(mode):
        raise _unsafe(seats_dir)
    files = [paths.kankan_dir / "project.lock"]
    for seat in seat_names():
        files += [seats_dir / f"{seat}.lock", seats_dir / f"{seat}.proc.json"]
    for path in files:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            continue
        except OSError as err:
            raise _unreadable(path, err)
        if not stat.S_ISREG(mode):
            raise _unsafe(path)
    _check_log(paths)


def _unsafe(path) -> KankanError:
    return KankanError(Status.NEEDS_ACTION, messages.DESTROY_PATH_UNSAFE.format(path=_show(path), quoted=_q(path)),
                       {"path": str(path)})


# --- Gate ---------------------------------------------------------------------------------------------------

def known_seats(ctx: Ctx) -> List[str]:
    """Seats of the configuration, of the seat refs and of the files in ``seats/``."""
    names = set(ctx.cfg.seats) if ctx.cfg is not None and ctx.cfg.board_id else set()
    names.update(ref[len(SEAT_PREFIX):] for ref in ctx.refs.list_raw(SEAT_PREFIX))  # a git failure propagates
    seats_dir = ctx.paths.seats_dir
    for path in seats_dir.iterdir() if seats_dir.is_dir() else []:
        for suffix in SEAT_FILE_SUFFIXES:
            if path.name.endswith(suffix):
                names.add(path.name[:-len(suffix)])
                break
    valid = []
    for name in sorted(names):
        try:
            valid.append(validate_seat(name))
        except KankanError:
            pass
    return valid


def _project_sessions(ctx: Ctx, tmux: Tmux) -> List[str]:
    try:
        return stopall.sessions(ctx, tmux)
    except (KankanError, OSError):
        return []  # an unreachable tmux server has no session


def gate(ctx: Ctx, tmux: Tmux) -> List[dict]:
    """Running seats, project sessions and the installed unit, each as ``{"what", "remedy"}``; empty when clear.

    Creates no file and takes no project lock. The remedies are `kankan stop` when the project can be opened and the
    seat is one of its configuration (the only seats `kankan stop` knows), otherwise the underlying commands.
    """
    entries = []
    configured = set(ctx.cfg.seats) if ctx.cfg is not None else set()
    for seat in known_seats(ctx):
        reasons = seats.running(ctx.paths, seat)
        if not reasons:
            continue
        remedies = []
        lock = shlex.quote(str(ctx.paths.seats_dir / f"{seat}.lock"))
        for reason in reasons:
            if reason == seats.RUNNING_LOCK_UNREADABLE:
                remedy = messages.DESTROY_REMEDY_LOCK_UNREADABLE.format(path=lock)
            elif reason == seats.RUNNING_RECORD_UNREADABLE:
                remedy = messages.DESTROY_REMEDY_UNREADABLE.format(
                    path=shlex.quote(str(ctx.paths.seats_dir / f"{seat}.proc.json")))
            elif ctx.usable and seat in configured:
                remedy = messages.DESTROY_REMEDY_STOP  # `kankan stop` only knows the configuration's seats
            elif reason.startswith(seats.RUNNING_PID_PREFIX):
                remedy = messages.DESTROY_REMEDY_KILL.format(pid=int(reason[len(seats.RUNNING_PID_PREFIX):]))
            else:
                remedy = messages.DESTROY_REMEDY_LOCK.format(path=lock)
            if remedy not in remedies:
                remedies.append(remedy)
        unknown = all(r in (seats.RUNNING_LOCK_UNREADABLE, seats.RUNNING_RECORD_UNREADABLE) for r in reasons)
        template = messages.DESTROY_GATE_SEAT_UNKNOWN if unknown else messages.DESTROY_GATE_SEAT
        entries.append({"what": template.format(seat=seat, reasons=", ".join(reasons)),
                        "remedy": "; ".join(remedies)})
    for name in _project_sessions(ctx, tmux):
        remedy = messages.DESTROY_REMEDY_STOP if ctx.usable else \
            messages.DESTROY_REMEDY_SESSION.format(name=shlex.quote(name))
        entries.append({"what": messages.DESTROY_GATE_SESSION.format(name=name), "remedy": remedy})
    unit = agents_cmd.unit_path(ctx)
    if unit.exists():
        remedy = messages.DESTROY_REMEDY_STOP if ctx.usable else \
            messages.DESTROY_REMEDY_UNIT.format(unit=shlex.quote(agents_cmd.unit_name(ctx)), path=shlex.quote(str(unit)))
        entries.append({"what": messages.DESTROY_GATE_UNIT.format(unit=unit), "remedy": remedy})
    return entries


# --- Local inventory ----------------------------------------------------------------------------------------

UNREACHABLE_SHOWN = 10
IGNORED_SHOWN = 5
LS_REMOTE_TIMEOUT = 10
# a conflicted rebase step can leave other markers too: the rebase is checked first
_OPERATIONS = (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"),
               ("CHERRY_PICK_HEAD", "cherry-pick"))
_LOCK_DIRS = ("seats", "attachment-locks")
_OID = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass
class Worktree:
    """A worktree destroy removes: ``path`` as registered, ``admin_dir`` its ``<gcd>/worktrees/<id>``."""
    path: Path
    admin_dir: Path
    missing: bool = False
    branch: Optional[str] = None
    dirty: List[str] = field(default_factory=list)
    operation: Optional[str] = None
    unreachable: List[str] = field(default_factory=list)
    unreachable_total: int = 0
    ignored: int = 0
    ignored_examples: List[str] = field(default_factory=list)
    submodules: List[str] = field(default_factory=list)  # checked-out submodules: git removes them only by force
    head_rev: Optional[str] = None  # a missing checkout: the commit or branch its HEAD records (display only)

    @property
    def at_risk(self) -> bool:
        return bool(self.dirty or self.operation or self.unreachable_total or self.submodules)


@dataclass
class Block:
    """An interrupted ``kankan block``: its journal (``seat`` set), its snapshot directory, or both."""
    seat: Optional[str]
    short: str
    snapshot: Optional[Path]
    files: List[str]
    files_link: bool = False  # the snapshot's ``files`` entry is a symbolic link: never listed through it


@dataclass
class Integration:
    name: str
    local: bool
    origin: Union[bool, str]  # True, False or "unknown"
    is_user_branch: bool
    default: bool
    commands: List[str]


@dataclass
class LocalPlan:
    worktrees: List[Worktree]
    kept_worktrees: List[Path]
    blocks: List[Block]
    refs: Dict[str, str]
    symbolic_refs: List[str]
    hook: str  # "kankan", "shared" (kankan's, in a shared hooks directory: kept), "foreign" or "absent"
    hook_path: Path
    git_config: Optional[str]
    exclude: bool
    state: List[Path]
    integration: Optional[Integration]
    repo: Optional[Path] = None  # display only
    state_dir: Optional[Path] = None  # <gcd>/kankan, display only

    @property
    def empty(self) -> bool:
        """Nothing to remove; kept items (user worktrees, a foreign hook, the integration branch) and lock files
        do not count."""
        return not (self.worktrees or self.blocks or self.refs or self.symbolic_refs or self.hook == "kankan"
                    or self.git_config is not None or self.exclude or self.state)


def _unreadable(path, err: OSError) -> KankanError:
    return KankanError(Status.NEEDS_ACTION, messages.DESTROY_UNREADABLE.format(path=path, error=err.strerror or err),
                       {"path": str(path)})


def _admin_dirs(gcd: Path) -> Dict[str, Path]:
    """Registered worktree path → its ``<gcd>/worktrees/<id>``, from each ``gitdir`` file (``<path>/.git``)."""
    result = {}
    base = gcd / "worktrees"
    try:
        entries = sorted(base.iterdir()) if base.is_dir() else []
    except OSError as err:
        raise _unreadable(base, err)
    for admin in entries:
        try:
            gitdir = (admin / "gitdir").read_text(errors="replace").strip()
        except OSError:
            continue  # not a worktree administrative directory git can list either
        if not os.path.isabs(gitdir):
            gitdir = os.path.normpath(os.path.join(str(admin), gitdir))
        if os.path.basename(gitdir) == ".git":
            result.setdefault(os.path.dirname(gitdir), admin)
    return result


_SHORT_LINK = re.compile(r"[A-Za-z0-9]{8}")


def _valid_short(value) -> bool:
    """A Trello short link, the whole string (``validate_short_link``'s ``$`` would also accept a final newline)."""
    return isinstance(value, str) and _SHORT_LINK.fullmatch(value) is not None


def _target_name(name: str, seat_names: List[str]) -> bool:
    return name in seat_names or (name.startswith("blocked-") and _valid_short(name[len("blocked-"):]))


def _git(cwd, args: List[str], where) -> bytes:
    """Run a read-only git command for the inventory; a failure means destroy cannot tell what is at risk, so it
    stops before any question (NEEDS_ACTION)."""
    proc = gitutil.run_bytes(cwd, args, check=False)
    if proc.returncode != 0:
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_GIT_FAILED.format(
            command=args[0] if args[0] != "-c" else args[3], path=where,
            error=proc.stderr.decode("utf-8", "replace").strip()), {"path": str(where)})
    return proc.stdout


def _unreachable(target: Worktree, cwd, rev: str) -> None:
    """Commits of a detached ``rev`` that no branch, remote-tracking branch or tag reaches."""
    others = [rev, "--not", "--branches", "--remotes", "--tags"]
    target.unreachable_total = int(_git(cwd, ["rev-list", "--count", *others], target.path).strip() or 0)
    if target.unreachable_total:
        log = _git(cwd, ["log", "--abbrev=7", "--format=%h %s", f"-n{UNREACHABLE_SHOWN}", *others], target.path)
        target.unreachable = log.decode("utf-8", "replace").splitlines()


def _status(target: Worktree) -> None:
    # --no-optional-locks: status must not refresh the index; core.fsmonitor=false: no monitor hook or daemon runs
    # (the inventory writes nothing). Bytes: file names need not be UTF-8 and may hold "\r".
    raw = _git(target.path, ["-c", "core.fsmonitor=false", "--no-optional-locks", "status", "--porcelain=v1", "-z",
                             "--untracked-files=all", "--ignored"], target.path)
    ignored = []
    entries = raw.split(b"\0")
    index = 0
    while index < len(entries):
        item = entries[index]
        index += 1
        if not item:
            continue
        status, name = item[:2], os.fsdecode(item[3:])
        if status == b"!!":
            ignored.append(name)
            continue
        target.dirty.append(name)
        if status[:1] in (b"R", b"C"):
            index += 1  # skip the rename source
    target.ignored, target.ignored_examples = len(ignored), ignored[:IGNORED_SHOWN]


def _inspect(path: str, admin_dir: Path, entry: dict) -> Worktree:
    branch = entry.get("branch")
    target = Worktree(path=Path(path), admin_dir=admin_dir,
                      branch=branch[len("refs/heads/"):] if isinstance(branch, str) and
                      branch.startswith("refs/heads/") else None)
    _status(target)
    args = ["rev-parse"]
    for marker, _ in _OPERATIONS:
        args += ["--git-path", marker]
    found = [os.fsdecode(line) for line in _git(path, args, target.path).split(b"\n") if line]
    for (_, operation), marker in zip(_OPERATIONS, found):
        if os.path.lexists(os.path.join(path, marker)):
            target.operation = operation
            break
    if target.branch is None:
        _unreachable(target, path, "HEAD")
    _submodules(target)
    return target


def _submodules(target: Worktree) -> None:
    """The checks ``git worktree remove`` makes before refusing without ``--force``: a ``modules`` directory in
    the worktree's administrative directory, or a gitlink of the index checked out as a repository."""
    raw = _git(target.path, ["-c", "core.fsmonitor=false", "--no-optional-locks", "ls-files", "--stage", "-z"],
               target.path)
    for item in raw.split(b"\0"):
        if item.startswith(b"160000 ") and b"\t" in item:
            name = os.fsdecode(item.split(b"\t", 1)[1])
            if os.path.lexists(os.path.join(str(target.path), name, ".git")):
                target.submodules.append(name)
    if not target.submodules and (target.admin_dir / "modules").is_dir():
        target.submodules.append(str(target.admin_dir / "modules"))


def _inspect_missing(path: str, admin_dir: Path, repo: Path) -> Worktree:
    """A registered worktree whose checkout is gone: its HEAD, operation markers, index and submodule
    repositories live on in ``admin_dir``. That HEAD can be the only name of commits no branch reaches, the index
    can hold staged work no commit holds, and ``modules/`` can hold submodule commits no branch reaches."""
    target = Worktree(path=Path(path), admin_dir=admin_dir, missing=True)
    head_file = admin_dir / "HEAD"
    try:
        head = head_file.read_text(errors="replace").strip()
    except OSError as err:
        raise _unreadable(head_file, err)
    if head.startswith("ref: refs/heads/"):
        target.branch = head[len("ref: refs/heads/"):]
        target.head_rev = head[len("ref: "):]
    elif _OID.match(head):
        target.head_rev = head
        _unreachable(target, repo, head)
    else:
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_ADMIN_HEAD_INVALID.format(path=head_file),
                          {"path": str(head_file)})
    for marker, operation in _OPERATIONS:
        if os.path.lexists(admin_dir / marker):
            target.operation = operation
            break
    _missing_index(target, repo)
    modules = admin_dir / "modules"
    try:
        mode = os.lstat(modules).st_mode
    except FileNotFoundError:
        mode = None
    except OSError as err:
        raise _unreadable(modules, err)
    if mode is not None:
        try:
            # anything there but an empty directory may hold a submodule's only commits
            stored = not stat.S_ISDIR(mode) or bool(os.listdir(modules))
        except OSError as err:
            raise _unreadable(modules, err)
        if stored:
            target.submodules.append(str(modules))
    return target


def _missing_index(target: Worktree, repo: Path) -> None:
    """Staged work in the index a missing checkout left behind: its entries that differ from the recorded HEAD
    (every entry when that HEAD is an unborn branch). No index file means nothing staged; an index git cannot
    read stops the inventory (NEEDS_ACTION). Read only: ``--no-optional-locks`` and ``--cached`` never write it."""
    index = target.admin_dir / "index"
    try:
        mode = os.lstat(index).st_mode
    except FileNotFoundError:
        return
    except OSError as err:
        raise _unreadable(index, err)
    if not stat.S_ISREG(mode):
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_MISSING_INDEX_UNSAFE.format(path=_show(index)),
                          {"path": str(index)})
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    base = ["-c", "core.fsmonitor=false", "--no-optional-locks"]
    if gitutil.rev(repo, f"{target.head_rev}^{{commit}}") is not None:
        args = base + ["diff-index", "--cached", "--no-renames", "--name-only", "-z", target.head_rev, "--"]
    else:
        args = base + ["ls-files", "--cached", "-z"]  # an unborn branch: every entry is staged work
    proc = gitutil.run_bytes(repo, args, check=False, env=env)
    if proc.returncode != 0:
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_GIT_FAILED.format(
            command=args[3], path=index, error=proc.stderr.decode("utf-8", "replace").strip()),
            {"path": str(index)})
    target.dirty = [os.fsdecode(name) for name in proc.stdout.split(b"\0") if name]


def _same_git_dir(repo: Path, path: str, admin_dir: Path) -> bool:
    """Whether the checkout at ``path`` is the one registered with ``admin_dir`` (not a clone put in its place).
    Only the path's own ``.git`` counts: git's discovery would climb to a checkout above it."""
    dotgit = os.path.join(path, ".git")
    if not os.path.lexists(dotgit):
        return False
    proc = gitutil.run_bytes(repo, ["rev-parse", "--resolve-git-dir", dotgit], check=False)
    if proc.returncode != 0:
        return False
    return os.path.realpath(os.fsdecode(proc.stdout.rstrip(b"\n"))) == os.path.realpath(str(admin_dir))


def _worktrees(ctx: Ctx, seat_names: List[str]):
    root = os.path.abspath(str(ctx.paths.worktree_root))
    admin = _admin_dirs(ctx.paths.gcd)
    repo = ctx.paths.repo
    targets, kept = [], set()
    for index, entry in enumerate(worktrees.worktree_list(repo)):
        path = entry.get("worktree")
        if index == 0 or not isinstance(path, str) or not path.startswith(root + os.sep):
            continue  # the main worktree, and worktrees outside the root, are never reported
        parent, name = os.path.split(path)
        if parent != root or not _target_name(name, seat_names) or os.path.islink(path) or path not in admin:
            kept.add(path)
        elif not os.path.lexists(path):
            targets.append(_inspect_missing(path, admin[path], repo))
        elif _same_git_dir(repo, path, admin[path]):
            targets.append(_inspect(path, admin[path], entry))
        else:
            kept.add(path)  # something else stands at the registered path: never git inside it, never remove it
    if os.path.isdir(root):
        try:
            names = os.listdir(root)
        except OSError as err:
            raise _unreadable(root, err)
        done = {str(target.path) for target in targets}
        kept.update(os.path.join(root, name) for name in names if os.path.join(root, name) not in done)
    return sorted(targets, key=lambda t: str(t.path)), [Path(path) for path in sorted(kept)]


_DIR_NOFOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _snapshot_files(snap: Path) -> Optional[Tuple[List[str], bool]]:
    """``(files, files_link)`` of a snapshot directory, or None when ``snap`` is not a directory (a link
    included). Listed through directory descriptors opened with ``O_NOFOLLOW``, so a link swapped in after a check
    is never followed: a ``files`` entry that is a link is reported, not listed, and links inside it are listed as
    entries, never descended."""
    try:
        top = os.open(snap, _DIR_NOFOLLOW)
    except OSError as err:
        if err.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
            return None
        raise _unreadable(snap, err)
    try:
        try:
            files_fd = os.open("files", _DIR_NOFOLLOW, dir_fd=top)
        except FileNotFoundError:
            return [], False
        except OSError as err:
            if err.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise _unreadable(snap / "files", err)
            try:
                mode = os.lstat("files", dir_fd=top).st_mode
            except OSError as again:
                raise _unreadable(snap / "files", again)
            return [], stat.S_ISLNK(mode)  # a link is removed, never followed; another file holds no listing
        try:
            result: List[str] = []
            _walk_fd(files_fd, snap / "files", "", result)
            return sorted(result), False
        finally:
            os.close(files_fd)
    finally:
        os.close(top)


def _walk_fd(fd: int, where: Path, prefix: str, result: List[str]) -> None:
    try:
        with os.scandir(fd) as entries:
            found = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    except OSError as err:
        raise _unreadable(where / prefix if prefix else where, err)
    for name, is_dir in found:
        rel = os.path.join(prefix, name) if prefix else name
        if not is_dir:
            result.append(rel)
            continue
        try:
            sub = os.open(name, _DIR_NOFOLLOW, dir_fd=fd)
        except OSError as err:
            raise _unreadable(where / rel, err)
        try:
            _walk_fd(sub, where, rel, result)
        finally:
            os.close(sub)


def _relative_files(base: Path) -> List[str]:
    result = []
    for dirpath, dirnames, filenames in os.walk(base, onerror=_raise):
        rel = os.path.relpath(dirpath, base)
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            result.append(name if rel == "." else os.path.join(rel, name))
    return sorted(result)


def _raise(err: OSError) -> None:
    raise _unreadable(err.filename, err)


def _blocks(ctx: Ctx, seat_names: List[str]) -> List[Block]:
    blocks_dir = ctx.paths.kankan_dir / "blocks"
    found = []
    for seat in seat_names:
        state_file = ctx.paths.seats_dir / f"{seat}.state.json"
        try:
            mode = os.lstat(state_file).st_mode
        except FileNotFoundError:
            continue
        except OSError as err:
            raise _unreadable(state_file, err)
        try:
            data = read_json(state_file, None) if stat.S_ISREG(mode) else None  # never open a FIFO or a link
        except ValueError:
            data = None
        except OSError as err:
            raise _unreadable(state_file, err)
        if not isinstance(data, dict):
            # whether a block was interrupted cannot be told: never "nothing at risk"
            raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_SEAT_STATE_INVALID.format(path=state_file),
                              {"path": str(state_file)})
        journal = data.get("block_journal")
        if journal is None:
            continue
        short = journal.get("short") if isinstance(journal, dict) else None
        files = journal.get("files", []) if isinstance(journal, dict) else None
        if not _valid_short(short) or not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_JOURNAL_INVALID.format(path=state_file),
                              {"path": str(state_file)})
        found.append(Block(seat=seat, short=short, snapshot=None, files=list(files)))
    try:
        # a symbolic link there is reported by _state; what it points to is not kankan's snapshot
        names = sorted(os.listdir(blocks_dir)) if blocks_dir.is_dir() and not blocks_dir.is_symlink() else []
    except OSError as err:
        raise _unreadable(blocks_dir, err)
    for short in names:
        snap = blocks_dir / short
        # every directory here may hold the only copy of interrupted work, whatever its name: one whose name is
        # not a valid short link has no journal (journals hold valid ones) and is shown by its raw name
        listed = _snapshot_files(snap)
        if listed is None:
            continue  # not a snapshot kankan wrote (a link is removed, never followed); it is still listed as state
        files, link = listed
        owners = [block for block in found if block.short == short]
        for block in owners:
            block.snapshot = snap
            block.files = block.files or files
            block.files_link = link
        if not owners:
            found.append(Block(seat=None, short=short, snapshot=snap, files=files, files_link=link))
    return found


def _is_lock(parts: tuple) -> bool:
    """kankan's own lock files, which destroy keeps: ``*.lock`` directly in ``<gcd>/kankan/``, ``seats/`` and
    ``attachment-locks/``, and ``qa/<conversation>/op.lock``."""
    name = parts[-1]
    if len(parts) == 1 or (len(parts) == 2 and parts[0] in _LOCK_DIRS):
        return name.endswith(".lock")
    return len(parts) == 3 and parts[0] == "qa" and name == "op.lock"


def _state(kankan_dir: Path) -> List[Path]:
    """Every file of ``<gcd>/kankan/`` except kankan's lock files, and its empty directories."""
    if not kankan_dir.is_dir():
        return []
    result = []
    for dirpath, dirnames, filenames in os.walk(kankan_dir, onerror=_raise):
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        rel = Path(dirpath).relative_to(kankan_dir).parts
        if rel and not dirnames and not filenames:
            result.append(Path(dirpath))
        for name in filenames + links:
            if not _is_lock(rel + (name,)):
                result.append(Path(dirpath) / name)
    return sorted(result)


def _overlap(first: str, second: str) -> bool:
    return os.path.commonpath([first, second]) in (first, second)


def _check_root_overlap(paths: Paths) -> None:
    """The worktree root and ``<gcd>/kankan`` must be disjoint: the state sweep removes everything under the state
    directory, kept worktrees included, and the worktree inventory lists the state directory as a kept entry.
    Compared both as written and with links resolved; NEEDS_ACTION before any question."""
    root, state = str(paths.worktree_root), str(paths.kankan_dir)
    roots = {os.path.abspath(root), os.path.realpath(root)}
    states = {os.path.abspath(state), os.path.realpath(state)}
    if any(_overlap(first, second) for first in roots for second in states):
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_ROOT_OVERLAP.format(
            root=_show(paths.worktree_root), state=_show(paths.kankan_dir),
            config=_show(paths.kankan_dir / "config.json")), {"path": str(paths.worktree_root)})
    # whatever the root: a registered worktree left inside the state directory would be swept as plain state,
    # without the discard question
    for index, entry in enumerate(worktrees.worktree_list(paths.repo)):
        path = entry.get("worktree")
        if index == 0 or not isinstance(path, str):
            continue
        found = {os.path.abspath(path), os.path.realpath(path)}
        if any(os.path.commonpath([first, second]) == second for first in found for second in states):
            raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_WORKTREE_IN_STATE.format(
                path=_show(path), state=_show(paths.kankan_dir), quoted=_q(path)), {"path": path})


def _hook_owned(paths: Paths, hook: Path) -> bool:
    """Whether the hook ``init.hook_path`` found belongs to this repository alone: its directory, links resolved, is
    inside the repository (``<gcd>`` or the main checkout) and is either the default ``<gcd>/hooks`` (no
    ``core.hooksPath`` anywhere) or named by a ``core.hooksPath`` of the repository's own configuration file that is
    also the value git uses. A hook reached through a global or system ``core.hooksPath`` (or an include, or
    ``GIT_CONFIG``) is shared with other repositories: it is kept."""
    effective = gitutil.run_bytes(paths.repo, ["config", "--get", "core.hooksPath"], check=False)
    if effective.returncode not in (0, 1):
        return False
    if effective.returncode == 0:
        local = gitutil.run_bytes(paths.repo, ["config", "--file", str(paths.gcd / "config"), "--get",
                                               "core.hooksPath"], check=False, env=_config_env())
        if local.returncode != 0 or local.stdout != effective.stdout:
            return False
    hooks_dir = os.path.realpath(str(hook.parent))
    return any(os.path.commonpath([hooks_dir, inside]) == inside
               for inside in (os.path.realpath(str(paths.gcd)), os.path.realpath(str(paths.repo))))


def _hook_kind(hook: Path) -> str:
    if not os.path.lexists(hook):
        return "absent"
    try:
        if not hook.is_symlink() and hook.is_file() and hook.read_bytes() == init.hook_content().encode():
            return "kankan"
    except OSError:
        pass  # a hook this user cannot read is not provably kankan's: it is kept
    return "foreign"


def _exclude_pair(lines: List[bytes], index: int) -> bool:
    """Whether ``lines[index]`` starts kankan's ``# kankan`` + ``/.kankan/`` pair (lines split with keepends)."""
    return index + 1 < len(lines) and lines[index].rstrip(b"\r\n") == b"# kankan" \
        and lines[index + 1].rstrip(b"\r\n") == b"/.kankan/"


def _exclude(gcd: Path) -> bool:
    path = gcd / "info" / "exclude"
    try:
        lines = path.read_bytes().splitlines(keepends=True)  # as _remove_exclude splits them
    except FileNotFoundError:
        return False
    except OSError as err:
        raise _unreadable(path, err)
    return any(_exclude_pair(lines, index) for index in range(len(lines)))


def _origin_has(repo: Path, ref: str) -> Union[bool, str]:
    """Whether ``origin`` has ``ref`` (asked to ``origin`` itself); ``"unknown"`` when it cannot be asked."""
    proc = gitutil.run(repo, ["remote"], check=False)
    if proc.returncode != 0:
        return "unknown"
    if "origin" not in proc.stdout.split():
        return False
    try:
        # no credential helper runs (it could store or erase credentials) and nothing prompts: a remote that needs
        # credentials answers "unknown"
        proc = gitutil.run(repo, ["-c", "credential.helper=", "ls-remote", "origin", ref], check=False,
                           timeout=LS_REMOTE_TIMEOUT, env=_probe_env(repo))
    except (subprocess.TimeoutExpired, OSError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return any(line.split("\t")[1:2] == [ref] for line in proc.stdout.splitlines())


def _probe_env(repo: Path) -> dict:
    """The environment of the remote probe: no terminal prompt, an askpass that always fails, and ssh in batch mode
    (the user's ``GIT_SSH_COMMAND`` or ``core.sshCommand`` kept, with ``-o BatchMode=yes`` appended)."""
    ssh = os.environ.get("GIT_SSH_COMMAND")
    if not ssh:
        proc = gitutil.run(repo, ["config", "--get", "core.sshCommand"], check=False)
        ssh = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "ssh"
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": f"{ssh} -o BatchMode=yes"}


def _integration(ctx: Ctx, name: str, hook: str, hook_path: Path) -> Optional[Integration]:
    """The integration branch ``name`` that ``local_plan`` itself read and validated."""
    repo = ctx.paths.repo
    local = gitutil.rev(repo, f"refs/heads/{name}") is not None
    origin = _origin_has(repo, f"refs/heads/{name}")
    if not local and origin is False:
        return None
    is_user = ctx.cfg is not None and getattr(ctx.cfg, "user_branch", None) == name
    commands = []
    if not is_user:
        quoted, old = shlex.quote(name), shlex.quote(f"{name}-old")
        if local:
            commands.append(messages.DESTROY_INTEGRATION_RENAME_LOCAL.format(name=quoted, old=old))
        if origin is True:
            if local:
                commands.append(messages.DESTROY_INTEGRATION_PUSH_OLD.format(old=old))
            else:
                commands.append(messages.DESTROY_INTEGRATION_FETCH.format(name=quoted))
                commands.append(messages.DESTROY_INTEGRATION_PUSH_FETCHED.format(old=old))
            commands.append(messages.DESTROY_INTEGRATION_DELETE_REMOTE.format(name=quoted))
            if hook == "foreign":
                commands.append(messages.DESTROY_INTEGRATION_HOOK_NOTE.format(path=hook_path))
            elif hook == "shared":
                commands.append(messages.DESTROY_INTEGRATION_HOOK_NOTE_SHARED.format(path=hook_path))
    return Integration(name=name, local=local, origin=origin, is_user_branch=is_user,
                       default=name == DEFAULT_INTEGRATION_BRANCH, commands=commands)


def _symbolic_refs(paths: Paths) -> List[str]:
    """Symbolic agent refs, dangling ones included: ``for-each-ref`` skips a symbolic ref whose target is gone,
    so the loose ref files are read too (packed refs cannot be symbolic)."""
    lines = gitutil.out(paths.repo, ["for-each-ref", "--format=%(refname) %(symref)", "refs/agent-cards",
                                     "refs/agent-seats"])
    found = {line.split(" ")[0] for line in lines.splitlines() if line.split(" ")[1:2] not in ([], [""])}
    for prefix in ("agent-cards", "agent-seats"):
        base = paths.gcd / "refs" / prefix
        if not base.is_dir():
            continue
        for name in _relative_files(base):
            ref = f"refs/{prefix}/{Path(name).as_posix()}"
            path = base / name
            try:
                mode = os.lstat(path).st_mode
            except OSError as err:
                raise _unreadable(path, err)
            if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                continue  # a FIFO would block open(), and git ignores it
            if not _valid_ref_name(paths.repo, ref):
                continue  # git ignores it (e.g. a leftover "*.lock"), so it is neither read nor reported
            try:
                if stat.S_ISLNK(mode):
                    # git's old symlink form of a symbolic ref, possibly dangling: its text names the target
                    symbolic = os.readlink(path).startswith("refs/")
                else:
                    with open(path, "rb") as handle:
                        symbolic = handle.read(4) == b"ref:"
            except OSError as err:
                raise _unreadable(path, err)
            if symbolic:
                found.add(ref)
    return sorted(found)


_KANKAN_REF = re.compile(r"refs/(agent-cards/[0-9a-f]{24}|agent-seats/[a-z]+-([0-9]+|qa))")


def _valid_ref_name(repo: Path, ref: str) -> bool:
    """Whether git accepts ``ref`` as a ref name; kankan's own names are valid without asking git."""
    return bool(_KANKAN_REF.fullmatch(ref)) or gitutil.run(repo, ["check-ref-format", ref], check=False).returncode == 0


def _config_env() -> dict:
    """The environment without ``GIT_CONFIG``, which would make ``git config --file``/``--local`` refuse to run."""
    return {key: value for key, value in os.environ.items() if key != "GIT_CONFIG"}


def _ref_format(paths: Paths) -> str:
    """The repository's ref storage: ``git rev-parse --show-ref-format`` (git >= 2.45), else the
    ``extensions.refStorage`` entry of the repository's own configuration file (absent: ``files``)."""
    proc = gitutil.run_bytes(paths.repo, ["rev-parse", "--show-ref-format"], check=False)
    out, err = proc.stdout.strip(), proc.stderr.decode("utf-8", "replace")
    if proc.returncode == 0 and out and out != b"--show-ref-format":
        return out.decode("utf-8", "replace")
    old_git = (proc.returncode == 0 and out == b"--show-ref-format") or "unknown option" in err or "usage:" in err
    if not old_git:
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_GIT_FAILED.format(
            command="rev-parse", path=paths.repo, error=err.strip()), {"path": str(paths.repo)})
    # --file reads the repository's own file: GIT_CONFIG and the global configuration cannot change the answer
    proc = gitutil.run_bytes(paths.repo, ["config", "--file", str(paths.gcd / "config"), "--get",
                                          "extensions.refStorage"], check=False, env=_config_env())
    if proc.returncode == 1:
        return "files"
    if proc.returncode != 0:
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_GIT_FAILED.format(
            command="config", path=paths.gcd / "config", error=proc.stderr.decode("utf-8", "replace").strip()),
            {"path": str(paths.gcd / "config")})
    return proc.stdout.decode("utf-8", "replace").strip().lower()


def _read_git_config(paths: Paths) -> subprocess.CompletedProcess:
    """``git config --get kankan.integrationBranch`` of the repository's own configuration file: ``--file`` with
    ``GIT_CONFIG`` removed, so neither that variable nor the global configuration changes the answer."""
    return gitutil.run_bytes(paths.repo, ["config", "--file", str(paths.gcd / "config"), "--get",
                                          "kankan.integrationBranch"], check=False, env=_config_env())


def _config_value(paths: Paths, proc: subprocess.CompletedProcess) -> str:
    """The value read by ``_read_git_config``, decoded strictly: a lossy decoding could name another branch."""
    value = proc.stdout[:-1] if proc.stdout.endswith(b"\n") else proc.stdout
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        config = paths.gcd / "config"
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_INTEGRATION_BRANCH_NOT_UTF8.format(path=config),
                          {"path": str(config)}) from None


def _configured_branch(cfg: Optional[Config]) -> Optional[str]:
    """The integration branch named by ``config.json``, if it names one."""
    branch = cfg.integration_branch if cfg is not None else None
    return branch if isinstance(branch, str) and branch else None


def _git_config(paths: Paths) -> Optional[str]:
    """``kankan.integrationBranch`` of the local configuration: ``None`` when absent, ``""`` when set empty."""
    return _git_config_result(paths, _read_git_config(paths))


def _git_config_result(paths: Paths, proc: subprocess.CompletedProcess) -> Optional[str]:
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        config = paths.gcd / "config"
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_GIT_FAILED.format(
            command="config", path=config, error=proc.stderr.decode("utf-8", "replace").strip()),
            {"path": str(config)})
    return _config_value(paths, proc)


def local_plan(ctx: Ctx, seats: List[str]) -> LocalPlan:
    """What destroy removes and keeps in the clone (T§3). Read only: it creates and changes no file."""
    ref_format = _ref_format(ctx.paths)
    if ref_format != "files":
        # dangling symbolic refs and worktree HEADs cannot be read from loose files there
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_REFTABLE.format(format=ref_format))
    error = ctx.state_error or state_root_error(ctx.paths)
    if error is not None:
        raise error
    _check_root_overlap(ctx.paths)
    if ctx.integration_error is not None:
        raise ctx.integration_error  # Ctx.open fell back to another name than the configured one
    git_config = _git_config(ctx.paths)  # before anything that names the branch: it raises for a bad value
    branch = _configured_branch(ctx.cfg) or git_config or DEFAULT_INTEGRATION_BRANCH
    if branch != ctx.refs.integration_branch:
        # the configuration changed since Ctx.open: one run must not mix two names
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_INTEGRATION_BRANCH_CHANGED)
    targets, kept = _worktrees(ctx, seats)
    refs = {}
    for prefix in (CARD_PREFIX, SEAT_PREFIX):
        refs.update({ref: value[0] for ref, value in ctx.refs.list_raw(prefix).items()})  # a git failure propagates
    symbolic = _symbolic_refs(ctx.paths)
    hook_path = init.hook_path(ctx.paths)
    hook = _hook_kind(hook_path)
    if hook == "kankan" and not _hook_owned(ctx.paths, hook_path):
        hook = "shared"  # kankan's exact hook, but in a hooks directory other repositories may use
    return LocalPlan(worktrees=targets, kept_worktrees=kept, blocks=_blocks(ctx, seats), refs=refs,
                     symbolic_refs=symbolic, hook=hook, hook_path=hook_path, git_config=git_config,
                     exclude=_exclude(ctx.paths.gcd), state=_state(ctx.paths.kankan_dir),
                     integration=_integration(ctx, branch, hook, hook_path), repo=ctx.paths.repo,
                     state_dir=ctx.paths.kankan_dir)


# --- Board inventory ----------------------------------------------------------------------------------------

@dataclass
class BoardPlan:
    """What destroy deletes, archives and keeps on the board (T§3); every collection is keyed by Trello ID."""
    board_id: str
    short: str
    name: str = field(repr=False)  # never printed: only the URL built from ``short`` is shown
    has_block: bool = False
    cards: Dict[str, List[str]] = field(default_factory=dict)  # target list ID -> card IDs, open and archived
    list_names: Dict[str, str] = field(default_factory=dict)  # target list ID -> current name, display only
    seat_lists: List[str] = field(default_factory=list)  # seat lists not already archived
    labels: List[str] = field(default_factory=list)  # Resume label IDs to delete
    label_names: Dict[str, str] = field(default_factory=dict)  # Resume label ID to delete -> name, display only
    kept_labels: List[str] = field(default_factory=list)  # Resume label names a card outside the targets carries
    kept_lists: List[Tuple[str, str]] = field(default_factory=list)  # (ID, current name) of the emptied kept lists
    targets: Set[str] = field(default_factory=set)  # every target list ID of the configuration
    desc: str = field(default="", repr=False)  # the description of the nested read (it may hold the name)


_REFUSED = re.compile(r"\((40[13])\)")
_BOARD_UNREADABLE = ("401", "403")


def _unreadable_status(err: KankanError) -> Optional[str]:
    """The HTTP status for which the board counts as unreadable ("401", "403", "404"), else None."""
    data = err.data if isinstance(err.data, dict) else {}
    if err.status == Status.CONFIG_ERROR:
        # the client raises it for 401/403 with the status only in its own message (never the response body)
        status = str(data["status"]) if "status" in data else None
        if status is None:
            match = _REFUSED.search(err.message or "")
            status = match.group(1) if match else None
        return status if status in _BOARD_UNREADABLE else None
    if err.status == Status.TRELLO_ERROR and data.get("status") == 404:
        return "404"
    return None


def _transient() -> KankanError:
    return KankanError(Status.NEEDS_ACTION, messages.DESTROY_TRELLO_TRANSIENT)


def _incomplete() -> KankanError:
    return KankanError(Status.NEEDS_ACTION, messages.DESTROY_BOARD_INCOMPLETE)


def _changed() -> KankanError:
    return KankanError(Status.NEEDS_ACTION, messages.DESTROY_BOARD_CHANGED)


def _ident(value) -> bool:
    return isinstance(value, str) and bool(value)


def _rows(data: dict, key: str, valid) -> list:
    rows = data.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) and valid(row) for row in rows):
        raise _incomplete()
    return rows


@dataclass
class _BoardRead:
    """The nested board read, every field destroy relies on checked (``TrelloClient.snapshot`` fills in defaults)."""
    desc: str
    lists: Dict[str, Tuple[str, bool]]  # list ID -> (name, closed)
    cards: List[Tuple[str, str, frozenset]]  # open cards: (ID, list ID, label IDs)
    labels: Dict[str, str]  # label ID -> name


def _strict_read(data) -> _BoardRead:
    if not isinstance(data, dict) or not isinstance(data.get("desc"), str):
        raise _incomplete()
    lists = _rows(data, "lists", lambda row: _ident(row.get("id")) and isinstance(row.get("name"), str)
                  and isinstance(row.get("closed"), bool))
    cards = _rows(data, "cards", lambda row: _ident(row.get("id")) and _ident(row.get("idList"))
                  and isinstance(row.get("idLabels"), list) and all(_ident(label) for label in row["idLabels"]))
    labels = _rows(data, "labels", lambda row: _ident(row.get("id")) and isinstance(row.get("name"), str))
    return _BoardRead(desc=data["desc"], lists=_by_id((row["id"], (row["name"], row["closed"])) for row in lists),
                      cards=[(row["id"], row["idList"], frozenset(row["idLabels"])) for row in cards],
                      labels=_by_id((row["id"], row["name"]) for row in labels))


def _by_id(pairs) -> dict:
    """Index rows by ID: identical duplicate rows collapse, two different rows under one ID stop the read."""
    indexed = {}
    for ident, value in pairs:
        if indexed.setdefault(ident, value) != value:
            raise _incomplete()
    return indexed


def _target_lists(cfg: Config) -> Tuple[List[str], List[str]]:
    """(kept list IDs, seat list IDs) of the configuration, each only if set, without duplicates."""
    kept = [cfg.backlog, cfg.done, cfg.blocked, cfg.qa_list, cfg.settings_list]
    seat_ids = [list_id for seat in cfg.work_seats().values()  # every seat but the Q&A one, as kankan counts them
                for list_id in (seat.in_progress, seat.implemented, seat.review)]
    kept = [list_id for list_id in dict.fromkeys(kept) if isinstance(list_id, str) and list_id]
    seat_ids = [list_id for list_id in dict.fromkeys(seat_ids)
                if isinstance(list_id, str) and list_id and list_id not in kept]
    return kept, seat_ids


def board_plan(ctx: Ctx) -> Tuple[Optional[BoardPlan], Optional[str]]:
    """What destroy does on the board (T§3), read only (GET requests only).

    Returns ``(None, reason)`` when the board is left alone: ``no configuration``, ``unreadable: <status>``,
    ``unreadable: credentials`` or ``another clone``. Raises ``NEEDS_ACTION`` for a damaged registry block, when
    Trello does not answer, when a read lacks a field destroy relies on, and when the reads disagree (the board
    changed in between), so nothing falls back to the local state only and no evidence is guessed.
    """
    cfg = ctx.cfg
    if cfg is None or not cfg.board_id:
        return None, "no configuration"
    if ctx.trello is None:
        return None, "unreadable: credentials"
    try:
        data = ctx.trello.board(cfg.board_id)
    except KankanError as err:
        status = _unreadable_status(err)
        if status is not None:
            return None, f"unreadable: {status}"
        raise _transient() from None  # the client's message may carry the response body
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("desc"), str) or not isinstance(name, str) \
            or not name.strip():
        raise _incomplete()  # a blank name could never be typed back as the confirmation
    ownership = registry_state(data["desc"])
    state, registry_id = ownership
    if state == "malformed":
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_REGISTRY_MALFORMED)
    if state == "valid" and registry_id != cfg.registry_id:
        return None, "another clone"
    try:
        payload = ctx.trello.board_payload(cfg.board_id)
        archived = ctx.trello.closed_cards(cfg.board_id)
    except KankanError as err:
        if err.status == Status.NEEDS_ACTION:
            raise  # a full page of archived cards: its own message
        if isinstance(err.data, dict) and err.data.get("conflict"):
            raise _changed() from None  # two different rows for one archived card
        raise _transient() from None  # a 401, 403 or 404 here is not an unreadable board either
    read = _strict_read(payload)
    if registry_state(read.desc) != ownership:
        raise _changed()
    observed: Dict[str, Tuple[str, frozenset]] = {}
    for card_id, list_id, label_ids in read.cards + [(c.id, c.list_id, frozenset(c.label_ids)) for c in archived]:
        if observed.setdefault(card_id, (list_id, label_ids)) != (list_id, label_ids):
            raise _changed()  # e.g. moved out of a target list and archived between the two reads
    kept_ids, seat_ids = _target_lists(cfg)
    targets = set(kept_ids) | set(seat_ids)
    short = data.get("shortLink")
    if not _valid_short(short):
        # only a valid short link is shown: anything else could carry the name slug of the URL
        short = cfg.board_short if _valid_short(cfg.board_short) else None
    if short is None:
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_BOARD_NO_SHORT_LINK)
    plan = BoardPlan(board_id=cfg.board_id, short=short, name=data["name"], has_block=state == "valid",
                     targets=targets, desc=read.desc)
    outside = set()
    for card_id, (list_id, label_ids) in observed.items():
        if list_id in targets:
            plan.cards.setdefault(list_id, []).append(card_id)
        else:
            outside.update(label_ids)
    for label_id, label_name in read.labels.items():
        if resume_time(label_name) is None:
            continue
        if label_id in outside:
            plan.kept_labels.append(label_name)
        else:
            plan.labels.append(label_id)
            plan.label_names[label_id] = label_name
    plan.list_names = {list_id: read.lists[list_id][0] for list_id in kept_ids + seat_ids if list_id in read.lists}
    plan.seat_lists = [list_id for list_id in seat_ids if list_id in read.lists and not read.lists[list_id][1]]
    plan.kept_lists = [(list_id, read.lists[list_id][0]) for list_id in kept_ids if list_id in read.lists]
    return plan, None


# --- Questions and execution (T§4, T§5) ---------------------------------------------------------------------

DISCARD_PHRASE = "discard uncommitted work"
LOCAL_PHRASE = "remove local kankan state"
PROGRESS_EVERY = 25
RISK_FILES_SHOWN = 10
_DISCARD, _MAIN = "discard", "main"


def _show(text) -> str:
    """A name or path made printable: bytes that are not UTF-8 and control characters are escaped."""
    decoded = os.fsencode(str(text)).decode("utf-8", "backslashreplace")
    return "".join(char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
                   for char in decoded)


def _q(path) -> str:
    return shlex.quote(_show(path))


def _url(short: str) -> str:
    """The board URL without its name part, which Trello derives from the (possibly older) name; only a valid
    short link is ever shown."""
    return f"https://trello.com/b/{short}" if _valid_short(short) else "<board-url>"


def _head(target: Worktree) -> str:
    return messages.DESTROY_HEAD_BRANCH.format(branch=_show(target.branch)) if target.branch is not None \
        else messages.DESTROY_HEAD_DETACHED


def _shown_list(items: List[str], limit: int = RISK_FILES_SHOWN, total: Optional[int] = None) -> str:
    total = len(items) if total is None else total
    text = ", ".join(_show(item) for item in items[:limit])
    return text + (messages.DESTROY_RISK_MORE.format(count=total - min(limit, len(items))) if total > limit else "")


def _board_work(board: BoardPlan) -> bool:
    return bool(board.cards or board.seat_lists or board.labels or board.has_block)


def _discard_needed(local: LocalPlan) -> bool:
    return any(target.at_risk for target in local.worktrees) or bool(local.blocks)


# --- describe ---

def _board_lines(board: Optional[BoardPlan], reason: Optional[str]) -> List[str]:
    if board is None:
        if reason == "another clone":
            return [messages.DESTROY_SKIP_OTHER_CLONE]
        if reason is not None and reason.startswith("unreadable"):
            return [messages.DESTROY_SKIP_UNREADABLE.format(status=reason[len("unreadable: "):])]
        return [messages.DESTROY_SKIP_NO_CONFIG]
    lines = [messages.DESTROY_BOARD_LINE.format(url=_url(board.short))]
    total = sum(len(cards) for cards in board.cards.values())
    if total:
        per_list = ", ".join(f"{_show(board.list_names.get(list_id, list_id))} {len(cards)}"
                             for list_id, cards in board.cards.items())
        lines.append(messages.DESTROY_BOARD_CARDS.format(count=total, lists=per_list))
    else:
        lines.append(messages.DESTROY_BOARD_NO_CARDS)
    if board.seat_lists:
        lines.append(messages.DESTROY_BOARD_SEAT_LISTS.format(count=len(board.seat_lists), names=", ".join(
            _show(board.list_names.get(list_id, list_id)) for list_id in board.seat_lists)))
    if board.labels:
        lines.append(messages.DESTROY_BOARD_LABELS.format(count=len(board.labels), names=", ".join(
            _show(board.label_names.get(label_id, label_id)) for label_id in board.labels)))
    lines.append(messages.DESTROY_BOARD_REGISTRY if board.has_block else messages.DESTROY_BOARD_NO_REGISTRY)
    lines.append(messages.DESTROY_BOARD_KEEPS.format(names=", ".join(_show(name) for _, name in board.kept_lists)))
    lines += [messages.DESTROY_BOARD_KEEPS_LABEL.format(name=_show(name)) for name in board.kept_labels]
    return lines


def _local_lines(local: LocalPlan) -> List[str]:
    lines = [messages.DESTROY_CLONE_LINE.format(path=_show(local.repo))]
    for target in local.worktrees:
        if target.missing:
            lines.append(messages.DESTROY_WORKTREE_MISSING.format(path=_show(target.path)))
            continue
        ignored = messages.DESTROY_WORKTREE_IGNORED.format(
            count=target.ignored, examples=", ".join(_show(name) for name in target.ignored_examples)) \
            if target.ignored else ""
        lines.append(messages.DESTROY_WORKTREE.format(path=_show(target.path), head=_head(target), ignored=ignored))
    lines += [messages.DESTROY_WORKTREE_KEPT.format(path=_show(path)) for path in local.kept_worktrees]
    if local.refs:
        cards = sum(1 for ref in local.refs if ref.startswith(CARD_PREFIX))
        lines.append(messages.DESTROY_REFS.format(count=len(local.refs), cards=cards, seats=len(local.refs) - cards))
    if local.hook == "kankan":
        lines.append(messages.DESTROY_HOOK_REMOVE.format(path=_show(local.hook_path)))
    elif local.hook == "foreign":
        lines.append(messages.DESTROY_HOOK_KEEP.format(path=_show(local.hook_path)))
    elif local.hook == "shared":
        lines.append(messages.DESTROY_HOOK_KEEP_SHARED.format(path=_show(local.hook_path)))
    if local.git_config is not None:
        lines.append(messages.DESTROY_GIT_CONFIG.format(value=_show(local.git_config)))
    if local.exclude and local.state_dir is not None:
        lines.append(messages.DESTROY_EXCLUDE.format(path=_show(local.state_dir.parent / "info" / "exclude")))
    if local.state and local.state_dir is not None:
        lines.append(messages.DESTROY_STATE.format(path=_show(local.state_dir), count=len(local.state)))
    lines.append(messages.DESTROY_KEEPS_BRANCHES)
    return lines


def _worktree_risk(target: Worktree, repo) -> List[str]:
    if not target.at_risk:
        return []
    path = _q(target.path)
    if target.missing:
        what = []
        if target.operation:
            what.append(messages.DESTROY_RISK_MISSING_OPERATION.format(operation=target.operation))
        if target.unreachable_total:
            what.append(messages.DESTROY_RISK_MISSING_COMMITS.format(
                commits=_shown_list(target.unreachable, UNREACHABLE_SHOWN, target.unreachable_total)))
        index = target.admin_dir / "index"
        if target.dirty:
            what.append(messages.DESTROY_RISK_MISSING_STAGED.format(index=_show(index),
                                                                    files=_shown_list(target.dirty)))
        if target.submodules:
            what.append(messages.DESTROY_RISK_MISSING_MODULES.format(path=_show(target.submodules[0])))
        lines = [messages.DESTROY_RISK_MISSING.format(path=_show(target.path), admin=_show(target.admin_dir),
                                                      what="; ".join(what))]
        if target.dirty:
            lines.append(messages.DESTROY_RISK_MISSING_INSPECT.format(index=_q(index), repo=_q(repo),
                                                                      rev=_q(target.head_rev or "HEAD")))
        if target.unreachable:
            lines.append(messages.DESTROY_RISK_MISSING_BRANCH.format(repo=_q(repo),
                                                                     tip=target.unreachable[0].split(" ")[0]))
        return lines
    lines, inspect, revert, accept = [], [], [], []
    detached = target.branch is None
    if target.dirty:
        lines.append(messages.DESTROY_RISK_DIRTY.format(path=_show(target.path), head=_head(target),
                                                        files=_shown_list(target.dirty)))
        inspect += [f"git -C {path} status", f"git -C {path} diff", f"git -C {path} diff --staged"]
    if target.operation:
        lines.append(messages.DESTROY_RISK_OPERATION.format(operation=target.operation, path=_show(target.path),
                                                            head=_head(target)))
        if not target.dirty:
            inspect.append(f"git -C {path} status")
        revert.append(f"git -C {path} {target.operation} --abort")
    if target.unreachable_total:
        lines.append(messages.DESTROY_RISK_UNREACHABLE.format(
            path=_show(target.path), commits=_shown_list(target.unreachable, UNREACHABLE_SHOWN,
                                                         target.unreachable_total)))
        # the same commits the inventory counted: those of HEAD that no branch, remote branch or tag reaches
        inspect.append(f"git -C {path} log --oneline HEAD --not --branches --remotes --tags")
    if target.submodules:
        lines.append(messages.DESTROY_RISK_SUBMODULES.format(path=_show(target.path), head=_head(target),
                                                             paths=_shown_list(target.submodules)))
        inspect.append(f"git -C {path} submodule status")
        inspect += [f"git -C {_q(os.path.join(str(target.path), name))} status"
                    for name in target.submodules[:RISK_FILES_SHOWN] if not os.path.isabs(name)]
    if target.dirty:
        revert += [f"git -C {path} restore --staged --worktree .", f"git -C {path} clean -fd"]
    if target.dirty or target.operation:
        if detached:
            accept.append(f"git -C {path} switch -c <name>")
        if target.dirty:
            accept += [f"git -C {path} add -A", f"git -C {path} commit"]
    elif target.unreachable_total:
        accept.append(f"git -C {path} branch <name>")
    for template, commands in ((messages.DESTROY_RISK_INSPECT, inspect), (messages.DESTROY_RISK_REVERT, revert),
                               (messages.DESTROY_RISK_ACCEPT, accept)):
        if commands:
            lines.append(template)
            lines += [f"    {command}" for command in commands]  # one per line: ready to copy
    return lines


def _block_files(block: Block) -> str:
    shown = [_shown_list(block.files)] if block.files else []
    if block.files_link:
        shown.append(messages.DESTROY_RISK_BLOCK_FILES_LINK)
    return ", ".join(shown) or "-"


def _block_risk(block: Block) -> List[str]:
    seat = messages.DESTROY_RISK_BLOCK_SEAT.format(seat=block.seat) if block.seat is not None else ""
    snapshot = messages.DESTROY_RISK_BLOCK_SNAPSHOT.format(path=_show(block.snapshot)) \
        if block.snapshot is not None else messages.DESTROY_RISK_BLOCK_NO_SNAPSHOT
    short = block.short if _valid_short(block.short) else _show(repr(block.short))
    lines = [messages.DESTROY_RISK_BLOCK.format(short=short, seat=seat, snapshot=snapshot,
                                                files=_block_files(block))]
    if block.seat is not None:
        command = f"kankan agent {seats.seat_type(block.seat)} --seat {block.seat}"
        lines.append(messages.DESTROY_RISK_BLOCK_LAUNCH.format(command=command, short=block.short))
    return lines


def _where(local: bool, origin) -> str:
    if local:
        return {True: messages.DESTROY_WHERE_BOTH, False: messages.DESTROY_WHERE_LOCAL}.get(
            origin, messages.DESTROY_WHERE_LOCAL_UNKNOWN)
    return messages.DESTROY_WHERE_ORIGIN if origin is True else messages.DESTROY_WHERE_UNKNOWN


def _integration_lines(info: Optional[dict], hook_removed: bool) -> List[str]:
    """F§3.7: the integration branch is kept; how to reuse it or start from a clean one."""
    if not info:
        return []
    name = _show(info["name"])
    where = _where(info["local"], info["origin"])
    if info["is_user_branch"]:
        lines = [messages.DESTROY_INTEGRATION_KEPT_USER.format(name=name, where=where)]
    else:
        lines = [messages.DESTROY_INTEGRATION_KEPT.format(name=name, where=where)]
    if info["name"] != DEFAULT_INTEGRATION_BRANCH:
        lines.append(messages.DESTROY_INTEGRATION_NOT_DEFAULT.format(name=name))
    if not info["is_user_branch"]:
        text = messages.DESTROY_INTEGRATION_CLEAN.format(name=name)
        if info["commands"]:
            text += messages.DESTROY_INTEGRATION_RENAME.format(
                commands="; ".join(f"`{_show(command)}`" for command in info["commands"]))
            if hook_removed:
                text += messages.DESTROY_INTEGRATION_HOOK_GONE
        lines.append(text + ".")
    return lines


def _integration_info(integration: Optional[Integration]) -> Optional[dict]:
    if integration is None:
        return None
    return {"name": integration.name, "local": integration.local, "origin": integration.origin,
            "is_user_branch": integration.is_user_branch, "commands": list(integration.commands)}


def describe(board: Optional[BoardPlan], reason: Optional[str], local: LocalPlan) -> str:
    """The inventory (F§3.2) and the work at risk with its commands (F§3.3); never the board name."""
    lines = [messages.DESTROY_TITLE] + _board_lines(board, reason) + _local_lines(local)
    lines += _integration_lines(_integration_info(local.integration), False)  # the hook is not removed yet
    risk = [line for target in local.worktrees for line in _worktree_risk(target, local.repo)]
    risk += [line for block in local.blocks for line in _block_risk(block)]
    if risk:
        lines += [messages.DESTROY_RISK_TITLE] + risk
    return "\n".join(lines)


# --- questions ---

def _match_confirm(confirm: List[str], questions: List[Tuple[str, str, str]]) -> set:
    """The questions ``--confirm`` answers; a value matching no question, or one already answered, is a usage
    error. The values are never echoed: one could be close to the board name."""
    answered = set()
    for index, value in enumerate(confirm, 1):
        if not questions:
            raise KankanError(Status.USAGE_ERROR, messages.DESTROY_CONFIRM_NOTHING.format(index=index))
        text = value.strip() if isinstance(value, str) else None
        keys = [key for key, phrase, _ in questions if phrase == text]
        if not keys:
            raise KankanError(Status.USAGE_ERROR, messages.DESTROY_CONFIRM_UNMATCHED.format(
                index=index, questions=", ".join(f"'{shown}'" for _, _, shown in questions)))
        if keys[0] in answered:
            raise KankanError(Status.USAGE_ERROR, messages.DESTROY_CONFIRM_DUPLICATE.format(index=index))
        answered.add(keys[0])
    return answered


def _gate_error(entries: List[dict]) -> KankanError:
    lines = [messages.DESTROY_GATE_REFUSED] + [messages.DESTROY_GATE_LINE.format(**entry) for entry in entries]
    return KankanError(Status.NEEDS_ACTION, "\n".join(lines), {"running": entries})


def destroy(ctx: Ctx, *, tmux: Tmux, ask: Callable[[str], Optional[str]], say: Callable[[str], None],
            confirm: List[str]) -> dict:
    """``kankan destroy`` (T§4): gate, inventory, questions, then destruction under the project lock.

    Returns the report, ``{"nothing_to_destroy": True}`` or ``{"cancelled": True}``. Nothing is written before
    every needed phrase was given; a stop during destruction raises ``NEEDS_ACTION`` with the partial report.
    """
    if ctx.state_error is not None:
        raise ctx.state_error  # nothing is read through <gcd>/kankan, the gate's files included
    _check_state_paths(ctx.paths, lambda: known_seats(ctx))
    entries = gate(ctx, tmux)
    if entries:
        raise _gate_error(entries)
    seat_names = known_seats(ctx)
    local = local_plan(ctx, seat_names)
    if local.symbolic_refs:
        lines = [messages.DESTROY_SYMBOLIC_REFS] + [messages.DESTROY_SYMBOLIC_LINE.format(ref=shlex.quote(ref))
                                                    for ref in local.symbolic_refs]
        raise KankanError(Status.NEEDS_ACTION, "\n".join(lines), {"symbolic_refs": local.symbolic_refs})
    board, reason = board_plan(ctx)
    if local.empty and (board is None or not _board_work(board)):
        _match_confirm(confirm, [])  # any --confirm value answers no question
        return {"nothing_to_destroy": True}
    discard = _discard_needed(local)
    questions = [(_DISCARD, DISCARD_PHRASE, DISCARD_PHRASE)] if discard else []
    if board is not None:
        questions.append((_MAIN, f"destroy {board.name}".strip(), messages.DESTROY_QUESTION_BOARD))
    else:
        questions.append((_MAIN, LOCAL_PHRASE, LOCAL_PHRASE))
    say(describe(board, reason, local))
    answered = _match_confirm(confirm, questions)
    for key, phrase, _ in questions:
        if key in answered:
            continue
        if key == _DISCARD:
            prompt = messages.DESTROY_ASK_DISCARD.format(phrase=DISCARD_PHRASE)
        elif board is not None:
            prompt = messages.DESTROY_ASK_BOARD
        else:
            prompt = messages.DESTROY_ASK_LOCAL.format(phrase=LOCAL_PHRASE)
        answer = ask(prompt)
        if not isinstance(answer, str) or answer.strip() != phrase:
            return {"cancelled": True}
    with contextlib.ExitStack() as stack:
        lock = ctx.paths.kankan_dir / "project.lock"
        try:
            # neither <gcd>/kankan nor project.lock is followed: a link swapped in during the questions creates
            # nothing anywhere
            stack.enter_context(project_lock(ctx.paths, nofollow=True))
        except OSError as err:
            raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_LOCK_UNSAFE.format(
                path=_show(lock), error=_show(err.strerror or err), quoted=_q(lock)),
                {"path": str(lock)}) from None
        return _execute(ctx, tmux, say, local, board, reason, seat_names, discard)


# --- execution ---

def _new_report(ctx: Ctx, board: Optional[BoardPlan], reason: Optional[str], local: LocalPlan) -> dict:
    cfg = ctx.cfg
    kept = []
    kept_lists = []
    if board is not None:
        defaults = {cfg.backlog: "Backlog", cfg.done: "Done", cfg.blocked: "Blocked", cfg.qa_list: "Q&A",
                    cfg.settings_list: "kankan"}
        for list_id, name in board.kept_lists:
            kept_lists.append({"id": list_id, "name": name})
            default = defaults.get(list_id)
            if default is not None and name != default:
                kept.append(messages.DESTROY_REPORT_KEPT_LIST_RENAMED.format(name=_show(name), default=default))
            else:
                kept.append(messages.DESTROY_REPORT_KEPT_LIST.format(name=_show(name)))
        kept.append(messages.DESTROY_REPORT_KEPT_LABELS)
        kept += [messages.DESTROY_REPORT_KEPT_RESUME.format(name=_show(name)) for name in board.kept_labels]
    kept += [messages.DESTROY_REPORT_KEPT_WORKTREE.format(path=_show(path)) for path in local.kept_worktrees]
    if local.hook == "foreign":
        kept.append(messages.DESTROY_REPORT_KEPT_HOOK.format(path=_show(local.hook_path)))
    elif local.hook == "shared":
        kept.append(messages.DESTROY_REPORT_KEPT_HOOK_SHARED.format(path=_show(local.hook_path)))
    kept.append(messages.DESTROY_REPORT_KEPT_LOCKS.format(path=_show(ctx.paths.kankan_dir)))
    kept.append(messages.DESTROY_REPORT_KEPT_USER)
    if board is not None:
        url = _url(board.short)
    elif cfg is not None and isinstance(cfg.board_short, str) and _valid_short(cfg.board_short):
        url = _url(cfg.board_short)
    else:
        url = "<board-url>"
    return {
        "board": None if board is None else {"url": _url(board.short), "cards_deleted": {}, "cards_skipped": 0,
                                             "lists_archived": [], "labels_deleted": [], "labels_kept": [],
                                             "registry_removed": False},
        "board_skipped": None if board is not None else reason,
        "worktrees_removed": [], "refs_deleted": 0, "hook": None, "git_config": False, "exclude": False,
        "state_removed": False, "kept": kept, "kept_lists": kept_lists,
        "manual_board_cleanup": reason if board is None and reason is not None and reason.startswith("unreadable")
        else None,
        "integration_branch": _integration_info(local.integration), "next": f"kankan init {url}",
    }


class _Stop(Exception):
    """A step that cannot complete: ``message`` for the user, ``left`` what the next run still has to do."""

    def __init__(self, message: str, left: List[str]):
        super().__init__(message)
        self.message, self.left = message, left


def _stopped(stop: _Stop, report: dict) -> KankanError:
    report["left"] = stop.left
    done = report_text(report, partial=True)
    lines = [stop.message, messages.DESTROY_PARTIAL_DONE, done or "-", messages.DESTROY_PARTIAL_LEFT]
    lines += [f"  {item}" for item in stop.left]
    return KankanError(Status.NEEDS_ACTION, "\n".join(lines), report)


def _trello_failure(err: KankanError) -> str:
    """What failed, without the client's message: it can carry the response body (and so the board name)."""
    data = err.data if isinstance(err.data, dict) else {}
    status = data.get("status")
    return err.status.value + (f" HTTP {status}" if isinstance(status, int) else "")


def _recheck_board(ctx: Ctx, board: BoardPlan) -> BoardPlan:
    """Under the lock: the board is inventoried again; its name and binding must be those the user confirmed
    (T§4.4), and it may hold no card or Resume label the user did not confirm, nor a confirmed card in another of
    kankan's lists (destroy would delete what the user did not see, or strand it once the configuration is gone).
    Returns the fresh plan: what left kankan's lists, or a Resume label a card outside them now carries, is no
    longer deleted, and the report shows the current names."""
    fresh, _ = board_plan(ctx)
    changed = KankanError(Status.NEEDS_ACTION, messages.DESTROY_BOARD_CHANGED_AFTER)
    if fresh is None or fresh.name != board.name or fresh.has_block != board.has_block \
            or fresh.board_id != board.board_id:
        raise changed
    confirmed = {card_id: list_id for list_id, card_ids in board.cards.items() for card_id in card_ids}
    for list_id, card_ids in fresh.cards.items():
        if any(confirmed.get(card_id) != list_id for card_id in card_ids):
            raise changed  # a new card, or a confirmed one now in another of kankan's lists
    if set(fresh.labels) - set(board.labels):
        raise changed  # a new Resume label
    if set(fresh.seat_lists) - set(board.seat_lists):
        raise changed  # a seat list reopened since the inventory
    return fresh


def _check_log(paths: Paths) -> None:
    """``log.jsonl`` must be absent or a regular file: destroy appends to it, and a symbolic link there would carry
    the record into whatever it points to (e.g. ~/.config/kankan/agents.json)."""
    log = paths.kankan_dir / "log.jsonl"
    try:
        info = os.lstat(log)
    except FileNotFoundError:
        return
    except OSError as err:
        raise _unreadable(log, err)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:  # a hard link shares its bytes with another file
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_LOG_UNSAFE.format(
            path=_show(log), quoted=_q(log)), {"path": str(log)})


def _reinspect(ctx: Ctx, target: Worktree) -> Worktree:
    """A target inspected again right before its removal; NEEDS_ACTION when something else stands there now."""
    path = str(target.path)
    if target.missing:
        if os.path.lexists(path):
            raise _local_changed(path)
        return _inspect_missing(path, target.admin_dir, ctx.paths.repo)
    if os.path.islink(path) or not _same_git_dir(ctx.paths.repo, path, target.admin_dir):
        raise _local_changed(path)
    proc = gitutil.run_bytes(path, ["symbolic-ref", "-q", "HEAD"], check=False)
    branch = os.fsdecode(proc.stdout.strip()) if proc.returncode == 0 else None
    return _inspect(path, target.admin_dir, {"branch": branch})


def _local_changed(path) -> KankanError:
    return KankanError(Status.NEEDS_ACTION, messages.DESTROY_LOCAL_CHANGED.format(path=_show(path)),
                       {"path": str(path)})


def _recheck_local(ctx: Ctx, local: LocalPlan, seat_names: List[str], authorised: Set[str]) -> List[Worktree]:
    """Under the lock: the worktrees to remove are still those the user saw, and none holds work at risk the user
    was not asked to discard. ``authorised`` holds the paths confirmed for discard: only those may have changed
    since the inventory. The interrupted blocks and the agent refs must be exactly those shown, whether or not
    discarding was confirmed: a discard covers only the work the user saw. Returns the worktrees, freshly
    inspected."""
    fresh, _ = _worktrees(ctx, seat_names)
    seen = {(str(target.path), target.missing) for target in local.worktrees}
    now = {(str(target.path), target.missing) for target in fresh}
    if seen != now:
        raise _local_changed(sorted(seen ^ now)[0][0])
    for target in fresh:
        if target.at_risk and str(target.path) not in authorised:
            raise _local_changed(target.path)
    blocks = _blocks(ctx, seat_names)
    shown = {_block_identity(block) for block in local.blocks}
    for block in blocks:
        if _block_identity(block) not in shown:
            where = block.snapshot or ctx.paths.seats_dir / f"{block.seat}.state.json"
            raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_BLOCKS_CHANGED.format(path=_show(where)),
                              {"path": str(where)})
    if len(blocks) != len(local.blocks):
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_BLOCKS_CHANGED.format(path=_show(
            ctx.paths.kankan_dir / "blocks")))
    refs = {}
    for prefix in (CARD_PREFIX, SEAT_PREFIX):
        refs.update({ref: value[0] for ref, value in ctx.refs.list_raw(prefix).items()})
    if refs != local.refs or _symbolic_refs(ctx.paths):
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_REFS_CHANGED_BEFORE)
    return fresh


def _block_identity(block: Block) -> tuple:
    """What the user was shown of an interrupted block: its seat (journal), short link, snapshot path, files and
    whether ``files`` is a link. Any difference is unseen work."""
    return (block.seat, block.short, None if block.snapshot is None else str(block.snapshot), tuple(block.files),
            block.files_link)


def _recheck_config(ctx: Ctx, seat_names: List[str]) -> Ctx:
    """Under the lock: the configuration is loaded again and must match the one the inventory used — the binding
    (board and registry IDs, or no configuration with the same reason), the paths, the integration branch, the
    target lists and the seats kankan knows. A seat created, a configuration written by ``init`` or a list
    changed while the questions were open stops the run before any write. Returns the fresh context."""
    fresh = Ctx.open(ctx.paths.repo)

    def facts(item: Ctx) -> tuple:
        cfg = item.cfg
        binding = None if cfg is None else (cfg.board_id, cfg.registry_id, _target_lists(cfg))
        paths = item.paths
        return (binding, item.reason, None if item.state_error is None else item.state_error.message,
                (str(paths.repo), str(paths.gcd), str(paths.kankan_dir), paths.project, str(paths.worktree_root)),
                item.refs.integration_branch)

    if facts(fresh) != facts(ctx) or known_seats(fresh) != list(seat_names):
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_CONFIG_CHANGED)
    return fresh


def _board_step(ctx: Ctx, board: BoardPlan, report: dict, say: Callable[[str], None]) -> None:
    client, info = ctx.trello, report["board"]
    order = [(list_id, card_id) for list_id, cards in board.cards.items() for card_id in cards]
    archived, deleted_labels = [], []

    def left(cards_done: int) -> List[str]:
        items = []
        if len(order) > cards_done:
            items.append(messages.DESTROY_LEFT_CARDS.format(count=len(order) - cards_done))
        lists = [list_id for list_id in board.seat_lists if list_id not in archived]
        if lists:
            items.append(messages.DESTROY_LEFT_LISTS.format(
                names=", ".join(_show(board.list_names.get(list_id, list_id)) for list_id in lists)))
        labels = [label_id for label_id in board.labels if label_id not in deleted_labels]
        if labels:
            items.append(messages.DESTROY_LEFT_LABELS.format(
                names=", ".join(_show(board.label_names.get(label_id, label_id)) for label_id in labels)))
        if board.has_block and not info["registry_removed"]:
            items.append(messages.DESTROY_LEFT_REGISTRY)
        return items

    def failed(step: str, err: KankanError, cards_done: int) -> _Stop:
        return _Stop(messages.DESTROY_TRELLO_FAILED.format(step=step, error=_trello_failure(err)), left(cards_done))

    def fresh_read(step: str) -> BoardPlan:
        try:
            fresh, reason = board_plan(ctx)
        except KankanError as err:
            raise failed(step, err, len(order)) from None
        if fresh is None:
            raise _Stop(messages.DESTROY_BOARD_REREAD_LOST.format(reason=reason), left(len(order)))
        return fresh

    for index, (list_id, card_id) in enumerate(order):
        try:
            # read right before the delete: a card a user moved out of kankan's lists during the run is skipped
            place = client.card_place(card_id)
            if place is not None and place[0] not in board.targets:
                info["cards_skipped"] += 1
                continue
            if place is not None:
                client.delete_card(card_id)  # a card gone meanwhile (404) counts as deleted
        except KankanError as err:
            raise failed("delete card", err, index) from None
        name = _show(board.list_names.get(list_id, list_id))
        info["cards_deleted"][name] = info["cards_deleted"].get(name, 0) + 1
        if (index + 1) % PROGRESS_EVERY == 0 and index + 1 < len(order):
            say(messages.DESTROY_PROGRESS_CARDS.format(done=index + 1, total=len(order)))
    for list_id in board.seat_lists:
        try:
            client.archive_list(list_id)
        except KankanError as err:
            gone = isinstance(err.data, dict) and err.data.get("status") == 404
            if not gone:
                raise failed("archive list", err, len(order)) from None
        archived.append(list_id)
        info["lists_archived"].append(_show(board.list_names.get(list_id, list_id)))
    # read again before the labels: one a card outside kankan's lists carries now is kept
    deletable = set(fresh_read("delete label").labels) if board.labels else set()
    for label_id in board.labels:
        if label_id not in deletable:
            deleted_labels.append(label_id)  # nothing left to do for it
            info["labels_kept"].append(_show(board.label_names.get(label_id, label_id)))
            continue
        try:
            client.delete_label(label_id)  # 404 counts as deleted
        except KankanError as err:
            raise failed("delete label", err, len(order)) from None
        deleted_labels.append(label_id)
        info["labels_deleted"].append(_show(board.label_names.get(label_id, label_id)))
    # read again before the registry block and the local steps: a card that arrived in kankan's lists during the
    # run would be stranded once the configuration is gone; the description written back is this read's
    fresh = fresh_read("registry block")
    remaining = sum(len(cards) for cards in fresh.cards.values())
    if remaining:
        raise _Stop(messages.DESTROY_BOARD_CARDS_REMAIN.format(count=remaining),
                    [messages.DESTROY_LEFT_CARDS_REMAIN.format(count=remaining)] + left(len(order)))
    if fresh.has_block:
        try:
            client.set_board_desc(board.board_id, remove_registry_block(fresh.desc))
        except KankanError as err:
            raise failed("registry block", err, len(order)) from None
        info["registry_removed"] = True
    say(messages.DESTROY_STEP_BOARD.format(
        cards=sum(info["cards_deleted"].values()), lists=len(info["lists_archived"]),
        labels=len(info["labels_deleted"]), registry=messages.DESTROY_SHOWN_REMOVED if info["registry_removed"]
        else messages.DESTROY_SHOWN_ALREADY_ABSENT))


def _remove_worktree(ctx: Ctx, target: Worktree, force: bool = False) -> None:
    """Remove one target worktree; a registered one whose directory is gone is unregistered by deleting its own
    administrative directory (never a repository-wide prune). ``force`` only for a target whose work at risk the
    user agreed to discard: otherwise git itself refuses a worktree that became dirty. Raises KankanError with
    git's error text."""
    admin_root = ctx.paths.gcd / "worktrees"
    if target.missing:
        admin = target.admin_dir
        if admin.parent != admin_root or admin.is_symlink() or os.path.lexists(target.path):
            raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_LOCAL_CHANGED.format(path=_show(target.path)))
        _remove_tree(admin)
        return
    args = ["worktree", "remove", *(["--force"] if force else []), str(target.path)]
    proc = gitutil.run_bytes(ctx.paths.repo, args, check=False)
    if proc.returncode != 0:
        raise KankanError(Status.NEEDS_ACTION, proc.stderr.decode("utf-8", "replace").strip())


def _remove_tree(top: Path) -> None:
    """Delete a directory tree with unlink and rmdir only: symbolic links inside are removed, never followed."""
    for dirpath, dirnames, filenames in os.walk(top, topdown=False, onerror=_raise):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            os.unlink(os.path.join(dirpath, name))
        os.rmdir(dirpath)


def _rmdir_if_empty(path) -> None:
    try:
        os.rmdir(path)
    except OSError as err:
        if err.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
            raise


def _remove_exclude(path: Path) -> bool:
    """Remove kankan's ``# kankan`` + ``/.kankan/`` pair, rewriting the file atomically, every other byte kept."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return False  # already gone
    lines = data.splitlines(keepends=True)
    kept, index, removed = [], 0, False
    while index < len(lines):
        if _exclude_pair(lines, index):
            index += 2
            removed = True
            continue
        kept.append(lines[index])
        index += 1
    if not removed:
        return False
    mode = stat.S_IMODE(os.stat(path).st_mode)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".exclude.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"".join(kept))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def _git_step(ctx: Ctx, local: LocalPlan, report: dict) -> None:
    hook = local.hook_path
    if local.hook == "kankan" and _hook_kind(hook) == "kankan" and _hook_owned(ctx.paths, hook):
        os.unlink(hook)
        report["hook"] = "removed"
    elif not os.path.lexists(hook):
        report["hook"] = "absent"
    else:
        report["hook"] = "shared, kept" if local.hook == "shared" else "foreign, kept"
    if local.git_config is not None:
        config = ctx.paths.gcd / "config"
        proc = gitutil.run_bytes(ctx.paths.repo, ["config", "--file", str(config), "--unset-all",
                                                  "kankan.integrationBranch"], check=False, env=_config_env())
        if proc.returncode not in (0, 5) or _read_git_config(ctx.paths).returncode != 1:
            # exit 5 is "not set"; whatever it meant, the entry must be gone now
            raise _Stop(messages.DESTROY_REMOVE_FAILED.format(
                path=f"kankan.integrationBranch in {_show(config)}",
                error=proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"),
                [messages.DESTROY_LEFT_LOCAL])
        report["git_config"] = True
    if local.exclude:
        report["exclude"] = _remove_exclude(ctx.paths.gcd / "info" / "exclude")


def _remove_state(kankan_dir: Path) -> None:
    """Every state entry (T§3), freshly listed, then the directories left empty, then ``log.jsonl`` and, last,
    ``config.json``. kankan's lock files and the directories holding them stay."""
    if kankan_dir.is_symlink():
        raise KankanError(Status.NEEDS_ACTION, messages.DESTROY_STATE_SYMLINK.format(
            path=kankan_dir, quoted=shlex.quote(str(kankan_dir))), {"path": str(kankan_dir)})
    last = [kankan_dir / "log.jsonl", kankan_dir / "config.json"]
    for entry in _state(kankan_dir):
        if entry in last:
            continue
        try:
            mode = os.lstat(entry).st_mode
        except FileNotFoundError:
            continue  # already gone
        if stat.S_ISDIR(mode):
            _rmdir_if_empty(entry)
        else:
            _unlink(entry)
    for dirpath, _, _ in os.walk(kankan_dir, topdown=False, onerror=_raise):
        if Path(dirpath) != kankan_dir:
            _rmdir_if_empty(dirpath)  # a directory holding lock files is not empty and stays
    for path in last:
        _unlink(path)


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _os_error(path, err: OSError) -> str:
    return messages.DESTROY_REMOVE_FAILED.format(path=_show(getattr(err, "filename", None) or path),
                                                 error=err.strerror or err)


def _execute(ctx: Ctx, tmux: Tmux, say: Callable[[str], None], local: LocalPlan, board: Optional[BoardPlan],
             reason: Optional[str], seat_names: List[str], discard: bool) -> dict:
    """T§4.4, under the project lock: re-check, then board → worktrees → refs → hook/config/exclude → state."""
    report = _new_report(ctx, board, reason, local)
    # the only targets git may remove with --force, and the only ones that may have changed since the inventory:
    # those whose work at risk the user agreed to discard
    authorised = {str(target.path) for target in local.worktrees if target.at_risk} if discard else set()
    try:
        _check_state_paths(ctx.paths, lambda: seat_names)
        ctx = _recheck_config(ctx, seat_names)
        entries = gate(ctx, tmux)
        if entries:
            raise _gate_error(entries)
        if board is not None:
            board = _recheck_board(ctx, board)
        targets = _recheck_local(ctx, local, seat_names, authorised)
    except KankanError as err:
        # nothing changed yet: the report says so
        data = dict(report, **err.data) if isinstance(err.data, dict) else report
        raise KankanError(err.status, err.message, data) from None
    report = _new_report(ctx, board, reason, local)  # from the accepted fresh inventory: current names
    local_left = [messages.DESTROY_LEFT_LOCAL]
    try:
        if board is not None:
            try:
                _board_step(ctx, board, report, say)
            except _Stop as stop:
                stop.left = stop.left + _rest_left(targets, local.refs)
                raise
        for index, target in enumerate(targets):
            force = str(target.path) in authorised
            try:
                current = _reinspect(ctx, target)
            except KankanError as err:
                raise _Stop(err.message, _rest_left(targets[index:], local.refs)) from None
            except OSError as err:
                raise _Stop(messages.DESTROY_WORKTREE_FAILED.format(path=_show(target.path),
                                                                   error=_show(err.strerror or err)),
                            _rest_left(targets[index:], local.refs)) from None
            if current.at_risk and not force:
                # work appeared after the confirmation (an editor, a build): never removed without the phrase
                raise _Stop(messages.DESTROY_LOCAL_CHANGED_DURING.format(path=_show(target.path)),
                            _rest_left(targets[index:], local.refs))
            try:
                _remove_worktree(ctx, current, force=force)
            except KankanError as err:
                error = err.message
            except OSError as err:
                error = err.strerror or str(err)
            else:
                report["worktrees_removed"].append(str(target.path))
                continue
            raise _Stop(messages.DESTROY_WORKTREE_FAILED.format(path=_show(target.path), error=error),
                        _rest_left(targets[index:], local.refs))
        try:
            _rmdir_if_empty(ctx.paths.worktree_root)
        except OSError as err:
            raise _Stop(_os_error(ctx.paths.worktree_root, err),
                        [messages.DESTROY_LEFT_ROOT.format(path=_show(ctx.paths.worktree_root))]
                        + _rest_left([], local.refs)) from None
        say(messages.DESTROY_STEP_WORKTREES.format(count=len(report["worktrees_removed"])))
        if local.refs:
            try:
                # one transaction, each ref with its inventoried value as the expected old value, never followed
                error = ctx.refs.delete_exact(local.refs)
            except OSError as err:
                raise _Stop(messages.DESTROY_REFS_FAILED.format(error=_show(err.strerror or err)),
                            _rest_left([], local.refs)) from None
            if error is not None:
                raise _Stop(messages.DESTROY_REFS_CHANGED.format(error=_show(error)), _rest_left([], local.refs))
        report["refs_deleted"] = len(local.refs)
        say(messages.DESTROY_STEP_REFS.format(count=len(local.refs)))
        try:
            _git_step(ctx, local, report)
        except OSError as err:
            raise _Stop(_os_error(ctx.paths.gcd, err), local_left) from None
        say(messages.DESTROY_STEP_GIT.format(hook=messages.DESTROY_SHOWN_HOOK[report["hook"]], config=_yes(report["git_config"]),
                                             exclude=_yes(report["exclude"])))
        try:
            auditlog.append(ctx.paths, "destroy", cards=sum(report["board"]["cards_deleted"].values())
                            if report["board"] else 0, lists=len(report["board"]["lists_archived"])
                            if report["board"] else 0, labels=len(report["board"]["labels_deleted"])
                            if report["board"] else 0, worktrees=len(report["worktrees_removed"]),
                            refs=report["refs_deleted"], state=len(local.state), follow=False)
            _remove_state(ctx.paths.kankan_dir)
        except OSError as err:
            raise _Stop(_os_error(ctx.paths.kankan_dir, err), [messages.DESTROY_LEFT_STATE]) from None
        except KankanError as err:
            raise _Stop(err.message, [messages.DESTROY_LEFT_STATE]) from None
    except _Stop as stop:
        raise _stopped(stop, report) from None
    report["state_removed"] = True
    say(messages.DESTROY_STEP_STATE.format(path=_show(ctx.paths.kankan_dir)))
    return report


def _yes(value: bool) -> str:
    return messages.DESTROY_SHOWN_YES if value else messages.DESTROY_SHOWN_NO


def _rest_left(targets: List[Worktree], refs: Dict[str, str]) -> List[str]:
    items = []
    if targets:
        items.append(messages.DESTROY_LEFT_WORKTREES.format(paths=", ".join(_show(t.path) for t in targets)))
    if refs:
        items.append(messages.DESTROY_LEFT_REFS.format(count=len(refs)))
    return items + [messages.DESTROY_LEFT_LOCAL]


# --- report ---

def report_text(report: dict, partial: bool = False) -> str:
    """The report (F§3.7); with ``partial`` only what was done, for a run that stopped."""
    if report.get("cancelled"):
        return messages.DESTROY_REPORT_CANCELLED
    if report.get("nothing_to_destroy"):
        return messages.DESTROY_REPORT_NOTHING
    lines = []
    board = report.get("board")
    if board is not None:
        cards = sum(board["cards_deleted"].values())
        per_list = ", ".join(f"{name} {count}" for name, count in board["cards_deleted"].items())
        if cards or board["lists_archived"] or board["labels_deleted"] or board["registry_removed"] \
                or not partial:
            lines.append(messages.DESTROY_REPORT_BOARD.format(
                url=board["url"], cards=cards, per_list=f" ({per_list})" if per_list else "",
                lists=", ".join(board["lists_archived"]) or messages.DESTROY_SHOWN_NONE,
                labels=", ".join(board["labels_deleted"]) or messages.DESTROY_SHOWN_NONE,
                registry=messages.DESTROY_SHOWN_REMOVED if board["registry_removed"]
                else messages.DESTROY_SHOWN_NOT_REMOVED))
        if board.get("cards_skipped"):
            lines.append(messages.DESTROY_REPORT_SKIPPED.format(count=board["cards_skipped"]))
        if board.get("labels_kept"):
            lines.append(messages.DESTROY_REPORT_LABELS_KEPT.format(names=", ".join(board["labels_kept"])))
    elif not partial:
        skipped = report.get("board_skipped")
        if report.get("manual_board_cleanup"):
            lines.append(messages.DESTROY_REPORT_MANUAL.format(
                status=report["manual_board_cleanup"][len("unreadable: "):]))
        elif skipped == "another clone":
            lines.append(messages.DESTROY_REPORT_OTHER_CLONE)
        else:
            lines.append(messages.DESTROY_REPORT_NO_CONFIG)
    if report["worktrees_removed"] or not partial:
        lines.append(messages.DESTROY_REPORT_WORKTREES.format(
            paths=", ".join(_show(path) for path in report["worktrees_removed"]) or messages.DESTROY_SHOWN_NONE))
    if report["refs_deleted"] or not partial:
        lines.append(messages.DESTROY_REPORT_REFS.format(count=report["refs_deleted"]))
    if report.get("hook") is not None:
        lines.append(messages.DESTROY_REPORT_HOOK.format(hook=messages.DESTROY_SHOWN_HOOK[report["hook"]]))
        lines.append(messages.DESTROY_REPORT_GIT_CONFIG.format(
            value=messages.DESTROY_SHOWN_YES if report["git_config"] else messages.DESTROY_SHOWN_NOT_SET))
        lines.append(messages.DESTROY_REPORT_EXCLUDE.format(
            value=messages.DESTROY_SHOWN_YES if report["exclude"] else messages.DESTROY_SHOWN_NONE_FOUND))
    if partial:
        return "\n".join(lines)
    lines.append(messages.DESTROY_REPORT_STATE.format(value=_yes(report["state_removed"])))
    if report["kept"]:
        lines.append(messages.DESTROY_REPORT_KEPT)
        lines += [f"  {item}" for item in report["kept"]]
    lines += _integration_lines(report.get("integration_branch"), report.get("hook") == "removed")
    lines.append(messages.DESTROY_REPORT_NEXT.format(command=report["next"]))
    return "\n".join(lines)
