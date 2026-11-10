"""``kankan doctor``: check the installation and the project, fix what is safe, list manual steps (spec §15.5)."""

from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Sequence

from kankan import gitutil
from kankan.errors import KankanError, Status

OK, FIXED, MANUAL, WARN, SKIPPED = "ok", "fixed", "manual", "warn", "skipped"
MIN_GIT, MIN_TMUX = (2, 38), (3, 2)
LOGIN_TIMEOUT = 15
STALE_DAYS = 7
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex CLI"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    action: Optional[str] = None


def _version(text: str) -> Optional[tuple]:
    match = re.search(r"(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _run(argv: List[str], timeout: float = 10) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


# ------------------------------------------------------------------ machine checks
def check_git() -> Check:
    proc = _run(["git", "--version"])
    version = _version(proc.stdout) if proc and proc.returncode == 0 else None
    if version is None:
        return Check("git", MANUAL, "git not found", "install Git ≥ 2.38")
    if version < MIN_GIT:
        return Check("git", MANUAL, f"git {version[0]}.{version[1]} is too old", "upgrade Git to ≥ 2.38")
    return Check("git", OK, f"git {version[0]}.{version[1]}")


def check_tmux() -> Check:
    proc = _run(["tmux", "-V"]) if shutil.which("tmux") else None
    version = _version(proc.stdout) if proc and proc.returncode == 0 else None
    if version is None:
        return Check("tmux", MANUAL, "tmux not found", "install tmux ≥ 3.2")
    if version < MIN_TMUX:
        return Check("tmux", MANUAL, f"tmux {version[0]}.{version[1]} is too old", "upgrade tmux to ≥ 3.2")
    return Check("tmux", OK, f"tmux {version[0]}.{version[1]}")


def _shebang(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            first = handle.readline().decode(errors="replace").strip()
    except OSError:
        return None
    return first[2:].split()[0] if first.startswith("#!") and first[2:].split() else None


def check_kankan_path() -> Check:
    found = shutil.which("kankan")
    here = Path(sys.executable).parent
    if found is None:
        return Check("kankan-path", MANUAL, "kankan is not on your login PATH (Codex runs commands with that PATH)",
                     "install kankan so that it is on your login PATH: pipx install /path/to/kankan (then "
                     "`pipx ensurepath`), or link an existing kankan command into ~/.local/bin")
    interpreter = _shebang(found)
    if interpreter and os.path.realpath(interpreter) != os.path.realpath(sys.executable):
        return Check("kankan-path", WARN, f"{found} belongs to another installation ({interpreter})",
                     f"remove the other installation or put {here} first on PATH")
    return Check("kankan-path", OK, found)


def _adapter(paths, agent_type: str):
    from kankan.adapters import load_adapter
    return load_adapter(paths, agent_type)


def check_agents(paths, types: Sequence[str]) -> List[Check]:
    checks, present = [], {}
    for agent_type in types:
        try:
            adapter = _adapter(paths, agent_type)
        except KankanError as err:
            checks.append(Check(f"agent:{agent_type}", MANUAL, err.message, "fix the JSON of the agents.json named"))
            present[agent_type] = None
            continue
        present[agent_type] = adapter if shutil.which(adapter.command[0]) else None
    any_present = any(present.values())
    for agent_type in types:
        if any(c.name == f"agent:{agent_type}" for c in checks):
            continue
        adapter = present[agent_type]
        if adapter is not None:
            checks.append(Check(f"agent:{agent_type}", OK, shutil.which(adapter.command[0])))
        else:
            name = AGENT_NAMES.get(agent_type, agent_type)
            checks.append(Check(f"agent:{agent_type}", WARN if any_present else MANUAL, f"{name} not found on PATH",
                                f"install {name} (or ignore it if you use only the other agent type)"))
    for agent_type in types:
        adapter = present.get(agent_type)
        if adapter is None:
            continue
        missing = [arg for arg in adapter.full_access if arg not in adapter.command]
        if missing:
            checks.append(Check(f"full-access:{agent_type}", MANUAL,
                                f"{AGENT_NAMES.get(agent_type, agent_type)} would run with permission prompts or a "
                                f"sandbox: its command `{' '.join(adapter.command)}` lacks {' '.join(missing)}",
                                f"add {' '.join(missing)} to the {agent_type} command in agents.json (kankan's agents "
                                "need full-access mode: git, Trello and the network, without prompts)"))
        else:
            checks.append(Check(f"full-access:{agent_type}", OK, " ".join(adapter.full_access) or "no flag needed"))
    for agent_type in types:
        adapter = present.get(agent_type)
        if adapter is None or not adapter.login_check:
            checks.append(Check(f"login:{agent_type}", SKIPPED, "cannot verify the login"))
            continue
        proc = _run(adapter.login_check, timeout=LOGIN_TIMEOUT)
        if proc is None:
            checks.append(Check(f"login:{agent_type}", SKIPPED, f"`{' '.join(adapter.login_check)}` did not answer"))
        elif proc.returncode == 0:
            checks.append(Check(f"login:{agent_type}", OK, "logged in"))
        else:
            checks.append(Check(f"login:{agent_type}", WARN, f"`{' '.join(adapter.login_check)}` failed",
                                f"start `{adapter.command[0]}` once and check that you are logged in"))
    return checks


def check_skill(fix: bool) -> Check:
    from kankan.skill_install import skill_dir, skill_install, targets
    source = str(skill_dir())
    real_dirs = [t for t in targets() if t.exists() and not t.is_symlink()]
    if real_dirs:
        return Check("skill", MANUAL, f"{real_dirs[0]} is a real directory, not kankan's link",
                     f"move {real_dirs[0]} away, then run `kankan doctor`")
    stale = [t for t in targets() if not t.is_symlink() or os.readlink(t) != source]
    if not stale:
        return Check("skill", OK, f"linked to {source}")
    if not fix:
        return Check("skill", MANUAL, f"not installed or stale: {', '.join(map(str, stale))}", "kankan skill install --force")
    skill_install(force=True)
    return Check("skill", FIXED, f"linked to {source}")


def _ensure_skill_links(types: Sequence[str]) -> Check:
    """Preflight: link the skill only where it is missing for the types being started; never re-point a link."""
    from kankan.skill_install import skill_dir, targets
    by_type = dict(zip(("claude", "codex"), targets()))
    created = []
    for agent_type in types:
        target = by_type.get(agent_type)
        if target is not None and not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(skill_dir(), target_is_directory=True)
            created.append(str(target))
    return Check("skill", FIXED if created else OK, f"linked {', '.join(created)}" if created else "present")


def check_logind() -> Check:
    proc = _run(["busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
                 "org.freedesktop.login1.Manager", "KillUserProcesses"]) if shutil.which("busctl") else None
    if proc is None or proc.returncode != 0:
        return Check("logind", SKIPPED, "cannot read KillUserProcesses")
    if proc.stdout.strip() == "b false":
        return Check("logind", OK, "KillUserProcesses=no")
    return Check("logind", WARN, "KillUserProcesses=yes: agents die when you log out",
                 "set KillUserProcesses=no in /etc/systemd/logind.conf")


def _units() -> List[Path]:
    unit_dir = Path(os.path.expanduser("~")) / ".config" / "systemd" / "user"
    return sorted(unit_dir.glob("kankan-up@*.service")) if unit_dir.exists() else []


def check_linger() -> Check:
    if not _units():
        return Check("linger", SKIPPED, "no autostart unit")
    proc = _run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"]) if shutil.which("loginctl") else None
    if proc is None or proc.returncode != 0:
        return Check("linger", SKIPPED, "cannot read Linger")
    if proc.stdout.strip() == "Linger=yes":
        return Check("linger", OK, "enabled")
    return Check("linger", MANUAL, "disabled: autostarted agents start only after you log in",
                 "loginctl enable-linger (or sudo loginctl enable-linger $USER)")


def machine_checks(fix: bool, types: Sequence[str], paths=None) -> List[Check]:
    return [check_git(), check_tmux(), check_kankan_path(), *check_agents(paths, types), check_skill(fix),
            check_logind(), check_linger()]


# ------------------------------------------------------------------ project checks
def _project_checks(cwd: Path, fix: bool, online: bool, types: Sequence[str]) -> List[Check]:
    from kankan.config import Paths, load_config
    try:
        paths = Paths.discover(cwd)
    except KankanError:
        return [Check("project", SKIPPED, f"{cwd} is not inside a Git clone: machine checks only")]
    if not (paths.kankan_dir / "config.json").exists():
        return [Check("project", MANUAL, f"{paths.repo} is not bound to a board", "kankan init <board-url>")]
    cfg = load_config(paths)
    checks = [Check("project", OK, f"{paths.repo} (board {cfg.board_short})")]
    checks.append(_check_adapters(paths, types))
    checks.append(_check_hook(paths, fix))
    checks.append(_check_exclude(paths, fix))
    checks += _check_branches(paths, cfg, fix)
    checks.append(_check_autostart(paths, fix, types))
    if not online:
        return checks + [Check(n, SKIPPED, "offline") for n in ("credentials", "registry", "lists", "labels", "reconcile")]
    return checks + _online_checks(cwd, paths, cfg, fix)


def _check_adapters(paths, types) -> Check:
    for agent_type in types:
        try:
            _adapter(paths, agent_type)
        except KankanError as err:
            return Check("adapters", MANUAL, err.message, "fix the JSON in the agents.json named")
    return Check("adapters", OK, "valid")


def _check_hook(paths, fix: bool) -> Check:
    from kankan.init import HOOK_MARKER, hook_content, hook_path, install_hook
    hook = hook_path(paths)
    current = hook.read_text(errors="replace") if hook.exists() else None
    if current is not None and HOOK_MARKER not in current:
        return Check("hook", MANUAL, f"a foreign pre-push hook exists at {hook}",
                     "add a check that refuses refs/agent-* to it (see kankan/hooks/pre-push)")
    if current == hook_content() and os.access(hook, os.X_OK):
        return Check("hook", OK, str(hook))
    if not fix:
        return Check("hook", MANUAL, f"{hook} missing, outdated or not executable", "kankan doctor (without --no-fix)")
    install_hook(paths)
    return Check("hook", FIXED, str(hook))


def _check_exclude(paths, fix: bool) -> Check:
    from kankan.init import add_excludes
    exclude = paths.gcd / "info" / "exclude"
    if exclude.exists() and "/.kankan/" in exclude.read_text().splitlines():
        return Check("exclude", OK, str(exclude))
    if not fix:
        return Check("exclude", MANUAL, f"/.kankan/ missing from {exclude}", "kankan doctor (without --no-fix)")
    add_excludes(paths)
    return Check("exclude", FIXED, str(exclude))


def _check_branches(paths, cfg, fix: bool) -> List[Check]:
    from kankan import diag, worktrees
    repo, branch = paths.repo, cfg.integration_branch
    if not gitutil.rev(repo, f"refs/heads/{branch}"):
        return [Check("integration-branch", MANUAL, f"{branch} does not exist", "kankan init (creates or adopts it)"),
                Check("branches", SKIPPED, f"{branch} does not exist")]
    checks = []
    from kankan.init import set_hook_branch
    configured = gitutil.run(repo, ["config", "--get", "kankan.integrationBranch"], check=False).stdout.strip()
    expected = branch if branch != cfg.user_branch else ""  # never block pushes of the user's own branch
    checked_out = worktrees.main_checked_out(repo, branch)
    if configured != expected and fix:
        set_hook_branch(paths, cfg)
        checks.append(Check("integration-branch", FIXED, f"{branch} (git config kankan.integrationBranch "
                            f"{'set' if expected else 'removed: it is your branch too'})"))
    elif configured != expected:
        checks.append(Check("integration-branch", MANUAL, "git config kankan.integrationBranch does not match",
                            f"git config kankan.integrationBranch {branch}" if expected else
                            "git config --unset kankan.integrationBranch"))
    if checked_out:
        checks.append(Check("integration-branch", WARN, f"{branch} is checked out in {checked_out[0]}",
                            f"switch {checked_out[0]} to one of your branches: only kankan may move {branch}"))
    elif not checks:
        checks.append(Check("integration-branch", OK, branch))
    if branch == cfg.user_branch:
        checks.append(Check("branches", SKIPPED, f"the agents integrate into your branch {branch}"))
        return checks
    data = diag.branch_divergence(SimpleNamespace(cfg=cfg, paths=paths))
    if data["user"] is None:
        checks.append(Check("branches", SKIPPED, f"your branch {cfg.user_branch} does not exist"))
        return checks
    detail = f"{branch} is {data['ahead']} commits ahead / {data['behind']} behind {data['user']}"
    oldest = data["oldest_unmerged_integration"]
    if oldest:
        age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(oldest)
        if age.days >= STALE_DAYS:
            checks.append(Check("branches", WARN, f"{detail}; the oldest integration waiting is {age.days} days old",
                                f"git merge {branch} on {data['user']}"))
            return checks
    checks.append(Check("branches", OK, detail))
    return checks


def _check_autostart(paths, fix: bool, types: Sequence[str]) -> Check:
    from kankan import agents_cmd
    holder = SimpleNamespace(paths=paths)
    unit = agents_cmd.unit_path(holder)
    if not unit.exists():
        return Check("autostart", SKIPPED, "no autostart unit for this project")
    text = unit.read_text()
    match = re.search(r"^Environment=PATH=(.*)$", text, re.M)
    unit_path = match.group(1).split(os.pathsep) if match else []
    needed = {str(Path(p).parent) for p in [shutil.which("kankan")] + [shutil.which(t) for t in types] if p}
    missing = sorted(d for d in needed if d not in unit_path)
    if not missing:
        return Check("autostart", OK, str(unit))
    up = re.search(r" -m kankan up (.*)$", text, re.M)
    wanted = agents_cmd.parse_wanted(up.group(1).split()) if up else {}
    strip = lambda t: re.sub(r"^Environment=PATH=.*$", "", t, flags=re.M)  # noqa: E731
    generated = up is not None and strip(text) == strip(agents_cmd.unit_content(holder, wanted))
    if not generated:
        return Check("autostart", MANUAL, f"{unit} was edited and its PATH lacks {', '.join(missing)}",
                     f"add {os.pathsep.join(missing)} to its Environment=PATH=")
    if not fix:
        return Check("autostart", MANUAL, f"the unit's PATH lacks {', '.join(missing)}", "kankan doctor (without --no-fix)")
    unit.write_text(agents_cmd.unit_content(holder, wanted))
    _run(["systemctl", "--user", "daemon-reload"])
    return Check("autostart", FIXED, f"{unit}: PATH updated (enabled state unchanged)")


def _online_checks(cwd: Path, paths, cfg, fix: bool) -> List[Check]:
    from kankan import diag
    from kankan.config import project_lock, registry_block, save_config
    from kankan.init import ensure_labels
    from kankan.project import Project
    rest = ("registry", "lists", "labels", "reconcile")
    try:
        project = Project.open(cwd)
        project.trello.me()
    except KankanError as err:
        return [Check("credentials", MANUAL, err.message, "kankan init --reauth")] + \
            [Check(n, SKIPPED, "needs credentials") for n in rest]
    checks = [Check("credentials", OK, "valid")]
    from kankan.init import account_warnings
    cfg.kankan_member_id = cfg.kankan_member_id or project.trello.me()["id"]
    cfg.kankan_username = cfg.kankan_username or project.trello.me()["username"]
    problems = account_warnings(project.trello, cfg)
    if problems:
        checks.append(Check("notifications", MANUAL, problems[0],
                            "give kankan its own Trello account (user manual §3.1), invite it to the board, then "
                            "kankan init --reauth"))
    else:
        checks.append(Check("notifications", OK, f"kankan acts as @{cfg.kankan_username}; it mentions each card's "
                            "creator, or the board's admins"))
    snap = project.trello.snapshot(cfg.board_id)
    block = registry_block(snap.desc) or {}
    if block.get("registry-id") != cfg.registry_id:
        checks.append(Check("registry", MANUAL, f"the board is bound to {block.get('host')}:{block.get('path')}",
                            "stop the other clone's agents, then kankan init --takeover"))
        return checks + [Check(n, SKIPPED, "board bound to another clone") for n in rest[1:]]
    checks.append(Check("registry", OK, "this clone"))
    closed = [name for list_id, name in cfg.workflow_lists().items()
              if list_id not in snap.lists or snap.lists[list_id].closed]
    checks.append(Check("lists", MANUAL, f"archived or deleted: {', '.join(closed)}",
                        f"unarchive list {closed[0]} on Trello") if closed else Check("lists", OK, "open"))
    missing = [name for attr, name in (("label_hold", "HOLD"), ("label_question", "Question"), ("label_error", "Error"))
               if getattr(cfg, attr) not in snap.labels]
    if not missing:
        checks.append(Check("labels", OK, "present"))
    elif not fix:
        checks.append(Check("labels", MANUAL, f"missing: {', '.join(missing)}", "kankan doctor (without --no-fix)"))
    else:
        from kankan.config import load_config
        with project_lock(paths):
            fresh = load_config(paths)  # a launcher may have registered a seat meanwhile
            ensure_labels(fresh, project.trello, snap)
            save_config(paths, fresh)
        checks.append(Check("labels", FIXED, f"recreated {', '.join(missing)}"))
        checks.append(Check("labels-restart", WARN, "running launchers read label IDs at startup; a recreated HOLD "
                            "does not restore which cards were held",
                            "restart the agents (kankan stop <seat>, then kankan up …)"))
    checks += settings_checks(project, fix=fix, online=True)
    issues = diag.reconcile(project, record=fix)
    checks.append(Check("reconcile", WARN, f"{len(issues)} issue(s)", "kankan reconcile, then kankan reconcile --fix "
                        "if appropriate") if issues else Check("reconcile", OK, "consistent"))
    return checks


# ------------------------------------------------------------------ entry points
def run(cwd: Optional[Path], fix: bool = True, online: bool = True,
        types: Sequence[str] = ("claude", "codex")) -> List[Check]:
    paths = None
    if cwd is not None:
        from kankan.config import Paths
        try:
            paths = Paths.discover(cwd)
        except KankanError:
            paths = None
    checks = machine_checks(fix, types, paths)
    if cwd is not None:
        checks += _project_checks(Path(cwd), fix, online, types)
    if paths is not None and not online:
        try:
            from kankan.project import Project
            from kankan.config import load_config
            checks += settings_checks(SimpleNamespace(paths=paths, cfg=load_config(paths)), fix=False, online=False)
        except KankanError as err:
            checks.append(Check("settings", MANUAL, err.message, "kankan doctor"))
    return checks


def settings_checks(project, *, fix: bool, online: bool) -> List[Check]:
    from kankan import boardconfig, boardsettings, model_catalog, seeds, settings_setup
    from kankan.adapters import adapter_types
    types = adapter_types(project.paths)
    checks = []
    try:
        if online and fix:
            for agent_type in types:
                catalog = model_catalog.refresh(project.paths, agent_type, force=True)
                checks.append(Check(f"catalog:{agent_type}", OK if catalog['source'] in ('command', 'override') else WARN, catalog['source']))
        adapters = seeds.ensure(project.paths, types, interactive=online and fix, say=lambda text: print(text, file=sys.stderr))
    except KankanError as err:
        checks.append(Check("settings", MANUAL, err.message, "kankan doctor in a terminal, or set seeds in agents.json"))
        if err.status != Status.NEEDS_ACTION:
            return checks
        adapters = boardsettings.adapters_for(project.paths)
    if online:
        try:
            if fix:
                reports = settings_setup.ensure(project, adapters, say=lambda text: None)
                checks.extend(Check("settings-created", FIXED, report) for report in reports)
            read = boardsettings.from_snapshot(project, project.trello.snapshot(project.cfg.board_id))
            result = boardsettings.apply_read(project, read, adapters, write=fix)
            if fix:
                boardsettings.drain_actions(project)
            values = result.values
        except KankanError as err:
            if err.status == Status.TRELLO_ERROR and (err.data or {}).get('status') in (400, 404):
                checks.append(Check("settings-backend", WARN, "backend lacks settings checklists; using seeds"))
                values = boardconfig._values(boardconfig.load(project.paths), boardconfig.schema(adapters))
            else:
                raise
    else:
        values = boardconfig._values(boardconfig.load(project.paths), boardconfig.schema(adapters))
    from kankan import modelcheck
    for role, value in values.items():
        checks.append(Check(f"settings:{role}", OK, boardconfig.display(value)))
        for agent_type, pair in value.items():
            result = modelcheck.check(adapters[agent_type], pair, read_only=not (online and fix))
            checks.append(Check(f"model:{role}:{agent_type}", OK if result.status == 'passed' else WARN,
                                f"{pair.name}: {result.status}" + (f" ({result.message})" if result.message else '')))
            if online and fix:
                state = boardconfig.load(project.paths)
                request = boardconfig.CheckRequest(role, agent_type, pair, state.settings.get(role, {}).get('revision', 0))
                boardsettings.record_check(project, request, result)
    if online and fix:
        boardsettings.drain_actions(project)
    return checks


def preflight(types: Sequence[str], paths=None) -> None:
    """Offline checks before starting agents of ``types``: a missing CLI, kankan or skill stops the start."""
    from kankan import seeds
    seeds.preflight(paths, types)
    if os.environ.get("KANKAN_SKIP_PREFLIGHT") == "1":
        return
    checks = [check_kankan_path(),
              *[c for c in check_agents(paths, types) if c.name.startswith(("agent:", "full-access:"))],
              _ensure_skill_links(types)]
    manual = [c for c in checks if c.status == MANUAL or (c.name.startswith("agent:") and c.status == WARN)]
    if manual:
        raise KankanError(Status.CONFIG_ERROR, "cannot start the agents:\n" + "\n".join(
            f"- {c.detail}: {c.action}" for c in manual), {"checks": [asdict(c) for c in manual]})


def report_text(checks: List[Check]) -> str:
    label = {OK: "ok", FIXED: "fixed", MANUAL: "MANUAL", WARN: "warn", SKIPPED: "skipped"}
    lines = [f"  {label[c.status]:<8} {c.name:<20} {c.detail}" for c in checks]
    steps = [c for c in checks if c.status == MANUAL]
    if steps:
        lines.append("Manual steps:")
        lines += [f"  {i}. {c.action}" for i, c in enumerate(steps, 1)]
    warns = [c for c in checks if c.status == WARN and c.action]
    if warns:
        lines.append("Suggestions:")
        lines += [f"  - {c.action}" for c in warns]
    return "\n".join(lines)
