"""``kankan init``: bind a clone to a Trello board (spec §6)."""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from kankan import auditlog, clock, gitutil, messages, seats, worktrees
from kankan.refstore import CARD_PREFIX, RefStore
from kankan.config import (Config, CredentialsNotFound, Paths, load_config, load_credentials, project_lock, registry_block, save_config,
                           user_credentials_path, write_registry_block)
from kankan.errors import KankanError, Status
from kankan.properties import PropertiesFile
from kankan.trello import pos_after
from kankan.trello import TrelloClient

DEFAULT_URL = "https://api.trello.com"
LIST_NAMES = (("backlog", "Backlog"), ("done", "Done"), ("blocked", "Blocked"))
LABELS = (("label_hold", "HOLD", "black"), ("label_question", "Question", "blue"), ("label_error", "Error", "red"))
AUTH_URL = "https://trello.com/1/authorize?expiration=never&scope=read,write&response_type=token&name=kankan&key={key}"
HOOK_MARKER = "kankan pre-push hook"


def parse_board_url(url: str) -> str:
    match = re.match(r"^(?:https?://trello\.com/b/)?([A-Za-z0-9]{8})(?:[/?#].*)?$", url.strip())
    if not match:
        raise KankanError(Status.USAGE_ERROR, f"not a Trello board URL: {url!r}")
    return match.group(1)


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _yes(answer: str, default: bool = True) -> bool:
    answer = (answer or "").strip().lower()
    return default if not answer else answer in ("y", "yes", "s", "si", "sì")


def _credentials_target(paths: Paths, cfg_path: Optional[str]) -> Tuple[Path, Tuple[str, str]]:
    return (Path(cfg_path) if cfg_path else user_credentials_path()), ("kankan.trello.key", "kankan.trello.token")


def acquire_credentials(paths: Paths, cfg: Config, base_url: str, prompt: Callable[[str], str],
                        say: Callable[[str], None], force: bool) -> Tuple[TrelloClient, dict, List[str]]:
    """Check the credentials (§6.2); if missing or invalid, guide the user and save them."""
    warnings: List[str] = []
    if not force:
        try:
            key, token = load_credentials(paths, cfg)
            client = TrelloClient(key, token, base_url=base_url, timeout=cfg.trello_timeout)
            return client, client.me(), warnings
        except CredentialsNotFound:
            pass
        except KankanError as err:
            if err.status != Status.CONFIG_ERROR:
                raise
            say(f"Invalid Trello credentials: {err.message}")
    key = prompt("Trello API key (from https://trello.com/apps/admin, logged in as kankan's own Trello account): ").strip()
    token = prompt(f"Trello Token (if needed, get it from {AUTH_URL.format(key=key)}): ").strip()
    client = TrelloClient(key, token, base_url=base_url, timeout=cfg.trello_timeout)
    me = client.me()
    target = Path(cfg.credentials_path) if cfg.credentials_path else user_credentials_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    props = PropertiesFile.load(target, missing_ok=True)
    key_name, token_name = cfg.credentials_keys
    props.set(key_name, key)
    props.set(token_name, token)
    warning = props.save()
    if warning:
        warnings.append(warning)
    say(f"Credentials saved in {target}")
    return client, me, warnings


def _blank_config(cfg_old: Optional[Config]) -> Config:
    if cfg_old is not None:
        return cfg_old
    return Config(board_id="", board_short="", registry_id="", backlog="", done="", blocked="", label_hold="",
                  label_question="", label_error="", test_command="")


def hook_path(paths: Paths) -> Path:
    hooks_path = gitutil.run(paths.repo, ["config", "--get", "core.hooksPath"], check=False).stdout.strip()
    hooks_dir = Path(hooks_path) if hooks_path else paths.gcd / "hooks"
    if not hooks_dir.is_absolute():
        hooks_dir = paths.repo / hooks_dir
    return hooks_dir / "pre-push"


def hook_content() -> str:
    return resources.files("kankan").joinpath("hooks/pre-push").read_text()


def install_hook(paths: Paths) -> Optional[str]:
    hook = hook_path(paths)
    hooks_dir = hook.parent
    content = hook_content()
    if hook.exists():
        if HOOK_MARKER in hook.read_text(errors="replace"):
            if hook.read_text() != content:
                hook.write_text(content)
            if not os.access(hook, os.X_OK):
                os.chmod(hook, 0o755)
            return None
        return (f"a pre-push hook already exists at {hook}; it was not changed. Add a check that refuses "
                f"refs/agent-* (see kankan/hooks/pre-push).")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook.write_text(content)
    os.chmod(hook, 0o755)
    return None


def add_excludes(paths: Paths) -> None:
    exclude = paths.gcd / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if "/.kankan/" not in text.splitlines():
        if text and not text.endswith("\n"):
            text += "\n"
        exclude.write_text(text + "# kankan\n/.kankan/\n")


def init(cwd, url: Optional[str], credentials: Optional[Path] = None, takeover: bool = False, reauth: bool = False, prompt: Callable[[str], str] = input,
         say: Callable[[str], None] = _say, base_url: Optional[str] = None) -> Tuple[Config, List[str]]:
    """Run the §6.1 steps under the project lock; returns the configuration and warnings."""
    paths = Paths.discover(Path(cwd))
    try:
        cfg_old: Optional[Config] = load_config(paths)
    except KankanError:
        cfg_old = None
    cfg = _blank_config(cfg_old)
    base_url = base_url or os.environ.get("KANKAN_TRELLO_URL") or (cfg_old.trello_base_url if cfg_old else DEFAULT_URL)
    cfg.trello_base_url = base_url
    if credentials is not None:
        cfg.credentials_path = str(Path(credentials).expanduser().resolve())
    warnings: List[str] = []
    with project_lock(paths):
        client, me, cred_warnings = acquire_credentials(paths, cfg, base_url, prompt, say, force=reauth)
        warnings += cred_warnings
        if reauth:
            if cfg_old is not None:
                save_config(paths, cfg)
            return cfg, warnings
        if not url:
            if cfg_old is None:
                raise KankanError(Status.USAGE_ERROR, "the board URL is required: kankan init <board-url>")
            url = cfg_old.board_short
        short = parse_board_url(url)
        board = client.board(short)
        if cfg_old is not None and cfg_old.board_id and cfg_old.board_id != board["id"]:
            raise KankanError(Status.CONFIG_ERROR, "this clone is already bound to another board")
        cfg.board_id, cfg.board_short = board["id"], board.get("shortLink", short)
        stranded = _bind_registry(paths, cfg, client, board, takeover, say, prompt, cfg_old)
        if stranded:
            warnings.append(f"{stranded} card(s) were left without a claim by the other clone: run "
                            "`kankan reconcile --fix` to move them back to Backlog")
        snap = client.snapshot(cfg.board_id)
        _ensure_lists(cfg, cfg_old, client, snap, prompt)
        ensure_qa_list(cfg, client, snap, prompt)
        ensure_labels(cfg, client, snap)
        cfg.kankan_member_id, cfg.kankan_username = me["id"], me["username"]
        warnings += account_warnings(client, cfg)
        current = cfg.test_command
        hint = f" [{current}]" if current else " (empty for none)"
        answer = prompt(f"Test command run by `kankan verify`{hint}: ").strip()
        if answer:
            cfg.test_command = answer
        warnings += _ensure_integration_branch(paths, cfg, cfg_old, prompt, say)
        save_config(paths, cfg)
        hook_warning = install_hook(paths)
        if hook_warning:
            warnings.append(hook_warning)
        add_excludes(paths)
        for path in worktrees.main_checked_out(paths.repo, cfg.integration_branch):
            warnings.append(f"{cfg.integration_branch} is checked out in {path}: only kankan moves it, so switch that "
                            f"worktree to one of your branches (its files fall behind when {cfg.integration_branch} "
                            "advances)")
        auditlog.append(paths, "init", board=cfg.board_id, registry=cfg.registry_id)
    from kankan import boardconfig, boardsettings, model_catalog, seeds, settings_setup
    from kankan.adapters import adapter_types
    from kankan.project import Project
    types = adapter_types(paths)
    for agent_type in types:
        model_catalog.refresh(paths, agent_type)
    adapters = seeds.ensure(paths, types, interactive=True, say=say, roles=boardconfig.roles(paths))
    say(messages.INIT_POPULATING_BOARD)
    project = Project.open(paths.repo)
    if not project.cfg.settings_list:
        snapshot = client.snapshot(cfg.board_id)
        matches = [lst for lst in snapshot.lists.values() if lst.name == "kankan" and not lst.closed]
        if matches and _yes(prompt("A list named 'kankan' exists on the board: adopt it? [Y/n] ")):
            with project_lock(paths):
                fresh = load_config(paths)
                fresh.settings_list = matches[0].id
                save_config(paths, fresh)
            project.reload_config()
        elif matches:
            # Declining adoption creates a distinct list, whose ID is authoritative.
            ident = client.create_list(cfg.board_id, 'kankan', exclude={lst.id for lst in snapshot.lists.values()}).id
            with project_lock(paths):
                fresh = load_config(paths)
                fresh.settings_list = ident
                save_config(paths, fresh)
            project.reload_config()
    settings_setup.ensure(project, adapters, say=say)
    return project.reload_config(), warnings


def account_warnings(client: TrelloClient, cfg: Config) -> List[str]:
    """kankan must act through its own Trello account, invited to boards that people administer (spec §14.2)."""
    admins = client.board_admins(cfg.board_id)
    if any(member_id == cfg.kankan_member_id for member_id, _ in admins):
        u = cfg.kankan_username
        return [f"the Trello token belongs to @{u}, an admin of this board: kankan treats every comment of its token's "
                f"owner as its own, so @{u}'s answers would never reach the agents and @{u} would get no notifications. "
                "Give kankan its own Trello account (user manual §3.1), then `kankan init --reauth`"]
    if not admins:
        return ["the board has no admin other than kankan: nobody is notified about the cards kankan creates"]
    return []


def _current_branch(repo: Path) -> Optional[str]:
    proc = gitutil.run(repo, ["symbolic-ref", "-q", "--short", "HEAD"], check=False)
    return proc.stdout.strip() or None


def _ensure_integration_branch(paths: Paths, cfg: Config, cfg_old: Optional[Config], prompt: Callable[[str], str],
                               say: Callable[[str], None]) -> List[str]:
    """§6.1: choose, create or adopt the branch the agents integrate into; returns warnings."""
    warnings: List[str] = []
    repo = paths.repo
    if cfg_old is None or not gitutil.rev(repo, f"refs/heads/{cfg.user_branch}"):
        if gitutil.rev(repo, "refs/heads/main"):
            cfg.user_branch = "main"
        else:
            current = _current_branch(repo)
            cfg.user_branch = current or prompt("Your main branch: ").strip() or "main"
    old = cfg_old.integration_branch if cfg_old is not None else None
    default = old or "kankan-agents"
    name = prompt(f"Branch where the agents integrate [{default}]: ").strip() or default
    if gitutil.run(repo, ["check-ref-format", "--branch", name], check=False).returncode != 0:
        raise KankanError(Status.USAGE_ERROR, f"{name!r} is not a valid branch name")
    if old is not None and name != old:
        held = seats.any_held(paths)
        refs = RefStore(repo, old).list_raw(CARD_PREFIX)
        if held or refs:
            raise KankanError(Status.NEEDS_ACTION,
                              f"the agents integrate into {old}: stop the agents and finish or release their cards "
                              f"before changing it (alive: {', '.join(held) or 'none'}; cards in progress: {len(refs)})")
    cfg.integration_branch = name
    if not gitutil.rev(repo, f"refs/heads/{name}"):
        remotes = gitutil.out(repo, ["remote"], check=False).split()
        if "origin" in remotes:
            gitutil.run(repo, ["fetch", "-q", "origin", f"+refs/heads/{name}:refs/remotes/origin/{name}"], check=False)
        if gitutil.rev(repo, f"refs/remotes/origin/{name}"):
            gitutil.run(repo, ["branch", "--no-track", name, f"refs/remotes/origin/{name}"])
            say(f"Branch {name} created from origin/{name}")
        else:
            start = f"refs/heads/{cfg.user_branch}" if gitutil.rev(repo, f"refs/heads/{cfg.user_branch}") else "HEAD"
            gitutil.run(repo, ["branch", "--no-track", name, start])
            say(f"Branch {name} created from {cfg.user_branch if start != 'HEAD' else 'HEAD'}")
    if name == cfg.user_branch:
        warnings.append(f"the agents integrate into your branch {name}: never keep it checked out while they work")
    set_hook_branch(paths, cfg)
    return warnings


def set_hook_branch(paths: Paths, cfg: Config) -> None:
    """The pre-push hook protects the integration branch, unless it is also the user's branch (pre-1.2 setup)."""
    if cfg.integration_branch != cfg.user_branch:
        gitutil.run(paths.repo, ["config", "kankan.integrationBranch", cfg.integration_branch])
    else:
        gitutil.run(paths.repo, ["config", "--unset", "kankan.integrationBranch"], check=False)


def takeover_problems(paths: Paths, cfg_old: Optional[Config], snap) -> Tuple[List[str], int]:
    """What a takeover would strand (takeover F§2): text lines and the number of stranded cards."""
    from kankan.diag import SEAT_LIST_RE
    lines: List[str] = []
    count = 0
    blocked_ids = {cfg_old.blocked} if cfg_old is not None and cfg_old.blocked else \
        {lst.id for lst in snap.lists.values() if lst.name == "Blocked"}
    for lst in sorted(snap.lists.values(), key=lambda l: l.pos):
        if lst.closed or not (SEAT_LIST_RE.match(lst.name) or lst.id in blocked_ids):
            continue
        open_cards = [c for c in snap.cards.values() if c.list_id == lst.id and not c.closed]
        if open_cards:
            count += len(open_cards)
            lines.append(f"  {len(open_cards)} card(s) in {lst.name}: " + ", ".join(c.short for c in open_cards))
    if lines:
        lines.insert(0, "Cards held by the other clone's agents: their work and claims stay in that clone "
                        "(finish them first with `kankan stop --graceful` there):")
    branch = cfg_old.integration_branch if cfg_old is not None and cfg_old.integration_branch else "kankan-agents"
    remotes = gitutil.out(paths.repo, ["remote"], check=False).split()
    code = 2  # git ls-remote --exit-code: 2 = no such branch
    if "origin" in remotes:
        try:
            code = gitutil.run(paths.repo, ["ls-remote", "--exit-code", "--heads", "origin", branch], check=False,
                               env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), timeout=30).returncode
        except subprocess.TimeoutExpired:
            code = -1
    if code == 2:
        lines.append(f"origin has no branch {branch}: the cards the other clone integrated would be missing here "
                     f"(push its {branch} to origin first)")
    elif code != 0:
        lines.append(f"could not check whether origin has the branch {branch}: if it does not, the cards the other "
                     f"clone integrated would be missing here (push its {branch} to origin first)")
    return lines, count


def _bind_registry(paths: Paths, cfg: Config, client: TrelloClient, board: dict, takeover: bool,
                   say: Callable[[str], None], prompt: Callable[[str], str] = input,
                   cfg_old: Optional[Config] = None) -> int:
    """Bind the board to this clone; returns the number of stranded cards a takeover accepted."""
    block = registry_block(board.get("desc", ""))
    if block and block.get("registry-id") == cfg.registry_id and cfg.registry_id:
        return 0
    if block and block.get("registry-id") and not takeover:
        raise KankanError(Status.CONFIG_ERROR,
                          f"the board is already bound to another kankan registry (host {block.get('host')}, "
                          f"path {block.get('path')}); stop that clone's agents, then use --takeover",
                          {"registry": block.get("registry-id")})
    stranded = 0
    if block and block.get("registry-id"):
        lines, stranded = takeover_problems(paths, cfg_old, client.snapshot(cfg.board_id))
        if lines:
            say("\n".join(lines))
            try:
                answer = prompt("Take over anyway? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                raise KankanError(Status.NEEDS_ACTION, "takeover cancelled: the board is unchanged")
    cfg.registry_id = secrets.token_hex(8)
    fields = {"registry-id": cfg.registry_id, "host": socket.gethostname(), "path": str(paths.repo)}
    client.set_board_desc(cfg.board_id, write_registry_block(board.get("desc", ""), fields))
    if cfg.settle_seconds:
        clock.sleep(cfg.settle_seconds)
    reread = registry_block(client.board(cfg.board_id).get("desc", ""))
    if not reread or reread.get("registry-id") != cfg.registry_id:
        raise KankanError(Status.CONFIG_ERROR, "another kankan init wrote the board registry at the same time; "
                          "stop the other clone and retry")
    say(f"Board bound to this clone (registry {cfg.registry_id})")
    return stranded


def _ensure_lists(cfg: Config, cfg_old: Optional[Config], client: TrelloClient, snap, prompt) -> None:
    for attr, name in LIST_NAMES:
        current = getattr(cfg, attr)
        if current:
            lst = snap.lists.get(current)
            if lst is None or lst.closed:
                raise KankanError(Status.CONFIG_ERROR, f"the {name} list ({current}) is archived or deleted: "
                                  "unarchive it on Trello", {"list": current})
            continue
        matches = sorted((l for l in snap.lists.values() if l.name == name and not l.closed), key=lambda l: l.pos)
        if matches and _yes(prompt(f"A list named {name!r} exists on the board: adopt it? [Y/n] ")):
            setattr(cfg, attr, matches[0].id)
        else:
            setattr(cfg, attr, client.create_list(cfg.board_id, name).id)


def ensure_qa_list(cfg: Config, client: TrelloClient, snap, prompt: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """Find, adopt, restore or create the ``Q&A`` list; returns what was done, or None when it was open.

    Unlike the other workflow lists, an archived or deleted ``Q&A`` list is never a ``config_error``: an
    archived one is unarchived, a deleted one is replaced. Without ``prompt`` (``doctor``) an open list
    named ``Q&A`` is adopted without asking.
    """
    configured = bool(cfg.qa_list)
    current = snap.lists.get(cfg.qa_list) if configured else None
    if current is not None and not current.closed:
        return None
    if current is not None:
        client.unarchive_list(current.id)
        return "unarchived"
    matches = sorted((l for l in snap.lists.values() if l.name == "Q&A" and not l.closed), key=lambda l: l.pos)
    if matches and (prompt is None or _yes(prompt("A list named 'Q&A' exists on the board: adopt it? [Y/n] "))):
        cfg.qa_list = matches[0].id
        return "adopted"
    cfg.qa_list = client.create_list(cfg.board_id, "Q&A", pos=pos_after(snap.lists, cfg.blocked),
                                     exclude={l.id for l in snap.lists.values()}).id
    return "recreated" if configured else "created"



def ensure_labels(cfg: Config, client: TrelloClient, snap) -> None:
    for attr, name, color in LABELS:
        current = getattr(cfg, attr)
        if current and current in snap.labels:
            continue
        matches = [l for l in snap.labels.values() if l.name == name]
        setattr(cfg, attr, matches[0].id if matches else client.create_label(cfg.board_id, name, color).id)
