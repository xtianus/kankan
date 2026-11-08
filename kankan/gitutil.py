"""Thin wrapper around the git command line (argument lists only, never a shell)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Union

from kankan.errors import KankanError, Status

PathLike = Union[str, Path]


def run(repo: PathLike, args: List[str], input: Optional[str] = None, check: bool = True,
        env: Optional[dict] = None, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Run ``git -C repo args``; on failure with ``check`` raise INTERNAL_ERROR with git's stderr."""
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True, text=True,
                          env=env, timeout=timeout)
    if check and proc.returncode != 0:
        raise KankanError(Status.INTERNAL_ERROR, f"git {args[0]} failed: {proc.stderr.strip()}",
                          {"args": args, "stderr": proc.stderr.strip()})
    return proc


def out(repo: PathLike, args: List[str], **kwargs) -> str:
    return run(repo, args, **kwargs).stdout.strip()


def rev(repo: PathLike, name: str) -> Optional[str]:
    """Resolve ``name`` to a full object id, or None if it does not exist."""
    proc = run(repo, ["rev-parse", "--verify", "-q", name], check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_ancestor(repo: PathLike, ancestor: str, descendant: str) -> bool:
    return run(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode == 0
