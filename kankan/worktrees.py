"""Branches and worktrees (spec §11)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from kankan import clock, gitutil
from kankan.errors import KankanError, Status
from kankan.refstore import validate_short_link


def branch_name(short: str, year: Optional[int] = None) -> str:
    year = clock.now().year if year is None else year
    return f"{year % 100:02d}kankan-{validate_short_link(short)}"


CARD_BRANCH = re.compile(r"^\d\dkankan-[A-Za-z0-9]{8}$")


def reserved_branch(cfg, name: str) -> bool:
    """The integration branch and the card branches belong to kankan; every other branch is the user's."""
    return name == cfg.integration_branch or bool(CARD_BRANCH.match(name))


def find_branches(repo, short: str) -> List[str]:
    pattern = f"refs/heads/??kankan-{validate_short_link(short)}"
    out = gitutil.out(repo, ["for-each-ref", "--format=%(refname:short)", pattern])
    return sorted(line for line in out.splitlines() if line)


def prepare_branch(repo, short: str, main: str, year: Optional[int] = None) -> str:
    """Reuse the single ``??kankan-<short>`` branch, or create one from ``main``."""
    found = find_branches(repo, short)
    if len(found) > 1:
        raise KankanError(Status.NEEDS_RECONCILIATION, f"more than one branch for card {short}: {', '.join(found)}",
                          {"branches": found})
    if found:
        return found[0]
    name = branch_name(short, year)
    gitutil.run(repo, ["branch", name, main])
    return name


def current_branch(worktree) -> Optional[str]:
    proc = gitutil.run(worktree, ["symbolic-ref", "-q", "--short", "HEAD"], check=False)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def checkout(worktree, branch: str) -> None:
    if current_branch(worktree) == branch:
        return
    gitutil.run(worktree, ["checkout", "-q", branch])


def dirty_files(worktree) -> List[str]:
    """Modified tracked files and untracked files not ignored (§11 "clean worktree")."""
    raw = gitutil.run(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"]).stdout
    result = []
    entries = raw.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        result.append(path)
        if status[0] in "RC":
            index += 1  # skip the rename source
    return result


def detach(worktree) -> None:
    gitutil.run(worktree, ["checkout", "-q", "--detach"])


def worktree_list(repo) -> List[dict]:
    result, current = [], {}
    for line in gitutil.out(repo, ["worktree", "list", "--porcelain"]).splitlines() + [""]:
        if not line:
            if current:
                result.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value or True
    return result


def where_checked_out(repo, branch: str) -> Optional[Path]:
    for entry in worktree_list(repo):
        if entry.get("branch") == f"refs/heads/{branch}":
            return Path(entry["worktree"])
    return None


def merge_in_progress(worktree) -> bool:
    return gitutil.rev(worktree, "MERGE_HEAD") is not None


def main_checked_out(repo, main: str) -> List[Path]:
    return [Path(e["worktree"]) for e in worktree_list(repo) if e.get("branch") == f"refs/heads/{main}"]


def commits_ahead(repo, branch: str, main: str) -> int:
    return int(gitutil.out(repo, ["rev-list", "--count", f"{main}..{branch}"]))


def reflog_subjects(worktree) -> List[str]:
    """The subjects of the worktree's own ``HEAD`` reflog, newest first (empty when it has none), e.g.
    ``commit (amend): …``. An entry may have an empty subject (the one of ``git worktree add``)."""
    proc = gitutil.run(worktree, ["reflog", "show", "--format=>%gs", "HEAD"], check=False)
    return [line[1:] for line in proc.stdout.splitlines() if line.startswith(">")] if proc.returncode == 0 else []


def reflog_length(worktree) -> int:
    """The number of entries in the worktree's own ``HEAD`` reflog (0 when it has none)."""
    return len(reflog_subjects(worktree))


def reset_detached(worktree, commit: str) -> Tuple[str, int]:
    """Discard everything in a worktree that holds no user work (the Q&A seat's, questions T§2): reset tracked
    files, remove untracked and ignored files, and detach ``HEAD`` at ``commit``. Returns the commit and the
    length of the ``HEAD`` reflog afterwards."""
    gitutil.run(worktree, ["reset", "-q", "--hard"])
    gitutil.run(worktree, ["clean", "-q", "-ffdx"])  # -ff: also nested repositories
    gitutil.run(worktree, ["checkout", "-q", "--detach", commit])
    return gitutil.out(worktree, ["rev-parse", "HEAD"]), reflog_length(worktree)
