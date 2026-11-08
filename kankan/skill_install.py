"""``kankan skill install``: link the shared skill into Claude Code and Codex skill directories."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import List


def skill_dir() -> Path:
    return Path(str(resources.files("kankan").joinpath("skill")))


def targets() -> List[Path]:
    home = Path(os.path.expanduser("~"))
    return [home / ".claude" / "skills" / "kankan", home / ".codex" / "skills" / "kankan"]


def skill_install(force: bool = False) -> List[str]:
    """Symlink the skill directory; existing non-symlinks are left alone with a warning."""
    source = skill_dir()
    messages = []
    for target in targets():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            if os.readlink(target) == str(source) and not force:
                messages.append(f"{target}: already installed")
                continue
            target.unlink()
        elif target.exists():
            messages.append(f"warning: {target} exists and is not a symlink; left unchanged")
            continue
        target.symlink_to(source, target_is_directory=True)
        messages.append(f"{target} -> {source}")
    return messages
