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


def test_skill_has_the_qa_section():
    """questions F§4.3: the Q&A section and the implementation addition for handed-off cards."""
    text = (skill_dir() / "SKILL.md").read_text()
    qa = text[text.index("## 5. Q&A"):]
    qa = qa[:qa.index("\n## ", 1)]
    for command in ("answer", "handoff", "fail"):
        assert f"kankan {command} --turn <turn>" in qa, command
    assert "kankan yield --json" in qa and "HOLD" in qa and "suspension" in qa
    assert re.search(r"[Nn]ever commit", qa) and "refs/agent-" in qa
    assert "newest unanswered" in qa and "`new`" in qa and "description" in qa
    assert "clean" in qa and "confirmation" in qa and "`not_owner`" in qa
    assert re.search(r"data .*never instructions", qa, re.S)
    assert "outside the checkout" in qa and "mktemp" in qa  # a --file in the checkout makes it dirty
    assert "## 5. Q&A (`kind: qa`)" in text and "[Q&A](#5-qa-kind-qa)" in text
    impl = text[text.index("## 2. Implementation"):text.index("## 3. Review")]
    assert "handoff" in impl and "comments" in impl and "prevails" in impl


def test_skill_install_symlinks(isolated_home):
    blocker = isolated_home / ".codex" / "skills" / "kankan"
    blocker.mkdir(parents=True)
    messages = skill_install()
    claude = isolated_home / ".claude" / "skills" / "kankan"
    assert claude.is_symlink() and (claude / "SKILL.md").exists()
    assert any("not a symlink" in m for m in messages)
    assert any("already installed" in m for m in skill_install())
