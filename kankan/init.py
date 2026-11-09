"""``kankan init``: bind a clone to a Trello board (spec §6)."""

from __future__ import annotations

import os
import re
import secrets
import socket
import sys
from importlib import resources
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from kankan import auditlog, clock, gitutil, seats, worktrees
from kankan.refstore import CARD_PREFIX, RefStore
from kankan.config import (Config, Paths, load_config, load_credentials, project_lock, registry_block, save_config,
                           user_credentials_path, write_registry_block)
from kankan.errors import KankanError, Status
from kankan.properties import PropertiesFile
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
        except KankanError as err:
            if err.status != Status.CONFIG_ERROR:
                raise
            say(f"Trello credentials missing or invalid: {err.message}")
    key = prompt("Trello API key (from https://trello.com/apps/admin): ").strip()
    say("Open this URL, allow access and copy the token:\n  " + AUTH_URL.format(key=key))
    token = prompt("Trello token: ").strip()
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
                  label_question="", label_error="", notify_id="", notify_username="", test_command="")


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


def init(cwd, url: Optional[str], credentials: Optional[Path] = None, notify: Optional[str] = None,
         takeover: bool = False, reauth: bool = False, prompt: Callable[[str], str] = input,
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
        _bind_registry(paths, cfg, client, board, takeover, say)
        snap = client.snapshot(cfg.board_id)
        _ensure_lists(cfg, cfg_old, client, snap, prompt)
        ensure_labels(cfg, client, snap)
        if notify:
            member = client.member(notify.lstrip("@"))
            if member is None:
                raise KankanError(Status.USAGE_ERROR, f"Trello user {notify!r} not found")
            cfg.notify_id, cfg.notify_username = member["id"], member["username"]
        elif not cfg.notify_id:
            cfg.notify_id, cfg.notify_username = me["id"], me["username"]
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
    return cfg, warnings


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
    gitutil.run(repo, ["config", "kankan.integrationBranch", name])
    return warnings


def _bind_registry(paths: Paths, cfg: Config, client: TrelloClient, board: dict, takeover: bool,
                   say: Callable[[str], None]) -> None:
    block = registry_block(board.get("desc", ""))
    if block and block.get("registry-id") == cfg.registry_id and cfg.registry_id:
        return
    if block and block.get("registry-id") and not takeover:
        raise KankanError(Status.CONFIG_ERROR,
                          f"the board is already bound to another kankan registry (host {block.get('host')}, "
                          f"path {block.get('path')}); stop that clone's agents, then use --takeover",
                          {"registry": block.get("registry-id")})
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


def ensure_labels(cfg: Config, client: TrelloClient, snap) -> None:
    for attr, name, color in LABELS:
        current = getattr(cfg, attr)
        if current and current in snap.labels:
            continue
        matches = [l for l in snap.labels.values() if l.name == name]
        setattr(cfg, attr, matches[0].id if matches else client.create_label(cfg.board_id, name, color).id)
