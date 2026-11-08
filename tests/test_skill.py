import re

from kankan.skill_install import skill_dir, skill_install


def test_skill_mentions_every_agent_command():
    text = (skill_dir() / "SKILL.md").read_text()
    assert text.startswith("---\nname: kankan\n")
    for command in "task note sync verify integrate done ask fail block yield".split():
        assert re.search(rf"kankan {command}\b", text), command
    assert re.search(r"Never.*refs/agent-", text, re.S)
    for status in ("needs_reconciliation", "not_owner", "on_hold", "main_moved"):
        assert status in text


def test_skill_install_symlinks(isolated_home):
    blocker = isolated_home / ".codex" / "skills" / "kankan"
    blocker.mkdir(parents=True)
    messages = skill_install()
    claude = isolated_home / ".claude" / "skills" / "kankan"
    assert claude.is_symlink() and (claude / "SKILL.md").exists()
    assert any("not a symlink" in m for m in messages)
    assert any("already installed" in m for m in skill_install())
