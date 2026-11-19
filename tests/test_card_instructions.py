"""Card instructions: delivery in `kankan task`, the check at claim and the cycle (card instructions F§3–4, T§3–4)."""

from kankan import agentcmds, seats, workflow
from tests.flow import agent_ctx, implement
from tests.test_qa_cmds import conv  # noqa: F401  (fixture)

SPEC = "Implement feature X: the specification.  \n\n"
EXAMPLE = (SPEC + "## agent: claude review\nConsult your advisor.\n"
           "## agent: all implementation\nWrite the commit messages in Italian.\n")


def _impl_task(project, fake, monkeypatch, agent_type, desc):
    lease = seats.acquire(project, agent_type)
    project.reload_config()
    card = fake.add_card(project.cfg.backlog, "c", desc=desc, pos=0)
    task = workflow.tick(project, lease)
    assert task["card"] == card["id"] and task["kind"] == "impl", task
    return card, lease, agentcmds.task(agent_ctx(project, lease, monkeypatch))


def _review_task(project, fake, monkeypatch, implementer_type, desc):
    implementer = seats.acquire(project, implementer_type)
    reviewer = seats.acquire(project, "codex" if implementer_type == "claude" else "claude")
    project.reload_config()
    card = implement(project, fake, implementer, "feature")
    fake.state["cards"][card["id"]]["desc"] = desc
    task = workflow.tick(project, reviewer)
    assert task["kind"] == "review" and task["card"] == card["id"], task
    return card, reviewer, agentcmds.task(agent_ctx(project, reviewer, monkeypatch))


def test_task_without_headings(kproject, fake_trello, monkeypatch):
    _, _, data = _impl_task(kproject, fake_trello, monkeypatch, "claude", "plain spec\n")
    assert data["desc"] == "plain spec\n"
    assert data["card_instructions"] is None and data["card_instructions_error"] is None


def test_task_without_headings_review(kproject, fake_trello, monkeypatch):
    _, _, data = _review_task(kproject, fake_trello, monkeypatch, "claude", "plain spec\n")
    assert data["desc"] == "plain spec\n"
    assert data["card_instructions"] is None and data["card_instructions_error"] is None


def test_task_routes_sections_implementers(kproject, fake_trello, monkeypatch):
    for agent_type in ("claude", "codex"):
        _, lease, data = _impl_task(kproject, fake_trello, monkeypatch, agent_type, EXAMPLE)
        assert data["desc"] == SPEC
        assert data["card_instructions"] == "## agent: all implementation\nWrite the commit messages in Italian."


def test_task_routes_sections_claude_reviewer(kproject, fake_trello, monkeypatch):
    _, _, data = _review_task(kproject, fake_trello, monkeypatch, "codex", EXAMPLE)
    assert data["desc"] == SPEC
    assert data["card_instructions"] == "## agent: claude review\nConsult your advisor."


def test_task_routes_sections_codex_reviewer(kproject, fake_trello, monkeypatch):
    _, _, data = _review_task(kproject, fake_trello, monkeypatch, "claude", EXAMPLE)
    assert data["desc"] == SPEC
    assert data["card_instructions"] is None


def test_task_keeps_spec_exact(kproject, fake_trello, monkeypatch):
    desc = "line one  \r\nline two\r\n\r\n## agent: claude\r\n\r\n"
    _, _, data = _impl_task(kproject, fake_trello, monkeypatch, "claude", desc)
    assert data["desc"] == "line one  \r\nline two\r\n\r\n"
    assert data["card_instructions"] == "## agent: claude"


def test_task_heading_first_gives_empty_spec(kproject, fake_trello, monkeypatch):
    _, _, data = _impl_task(kproject, fake_trello, monkeypatch, "claude", "## agent: all\nhow\n")
    assert data["desc"] == "" and data["card_instructions"] == "## agent: all\nhow"


def test_task_invalid_mid_task(kproject, fake_trello, monkeypatch):
    card, lease, _ = _impl_task(kproject, fake_trello, monkeypatch, "claude", SPEC + "## agent: claude\nmine\n")
    broken = SPEC + "## agent: cluade\nsecret one\n## agent: codex\nsecret two\n## agent: claude\nmine\n"
    fake_trello.state["cards"][card["id"]]["desc"] = broken
    data = agentcmds.task(agent_ctx(kproject, lease, monkeypatch))
    assert data["desc"] == SPEC and data["card_instructions"] is None
    assert data["card_instructions_error"].startswith("line 3: ")
    assert "secret" not in data["card_instructions_error"] + data["desc"]
    assert kproject.cfg.label_error not in fake_trello.state["cards"][card["id"]]["idLabels"]
    fake_trello.state["cards"][card["id"]]["desc"] = SPEC + "## agent: claude\nmine\n"
    data = agentcmds.task(agent_ctx(kproject, lease, monkeypatch))
    assert data["card_instructions"] == "## agent: claude\nmine" and data["card_instructions_error"] is None


def test_hold_wip_spec_only(kproject, fake_trello, monkeypatch):
    lease = seats.acquire(kproject, "claude")
    card = fake_trello.add_card(kproject.cfg.backlog, "c", desc=EXAMPLE, pos=0)
    workflow.tick(kproject, lease)
    (kproject.paths.seat_worktree(lease.seat) / "wip.txt").write_text("work in progress")
    fake_trello.user_label(card["id"], kproject.cfg.label_hold)
    assert workflow.tick(kproject, lease)["kind"] == "hold_wip"
    ctx = agent_ctx(kproject, lease, monkeypatch)
    for desc in (EXAMPLE, SPEC + "## agent: cluade\nx\n"):
        fake_trello.state["cards"][card["id"]]["desc"] = desc
        data = agentcmds.task(ctx)
        assert data["desc"] == SPEC
        assert "card_instructions" not in data and "card_instructions_error" not in data


def test_qa_task_returns_description_whole(conv):
    desc = "how does it work?\n## agent: claude\nx\n"
    conv.fake.state["cards"][conv.card["id"]]["desc"] = desc
    data = agentcmds.task(conv.ctx)
    assert data["card"]["desc"] == desc
    assert "card_instructions" not in data
