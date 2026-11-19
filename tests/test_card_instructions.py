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


# ---------------------------------------------------------------- check at claim (F§4, T§3)

from kankan import cardsections_feedback as feedback  # noqa: E402
from kankan.refstore import card_ref  # noqa: E402

BAD = "spec\n## agent: cluade review\nx\n"


def _sections_comments(project, card_id):
    return [c for c in project.trello.comments(card_id) if "kankan:sections-" in c.text]


def _has_error(project, fake, card_id):
    return project.cfg.label_error in fake.state["cards"][card_id]["idLabels"]


def test_invalid_card_skipped_next_claimed(kproject, fake_trello):
    project, fake = kproject, fake_trello
    bad = fake.add_card(project.cfg.backlog, "bad", desc=BAD, pos=1)
    good = fake.add_card(project.cfg.backlog, "good", pos=2)
    first = seats.acquire(project, "claude")
    assert workflow.tick(project, first)["card"] == good["id"]
    assert _has_error(project, fake, bad["id"]) and len(_sections_comments(project, bad["id"])) == 1
    assert "cluade" in _sections_comments(project, bad["id"])[0].text
    assert project.refs.read(card_ref(bad["id"])) is None
    fake.state["cards"][bad["id"]]["desc"] = "spec\n## agent: claude review\nx\n"
    second = seats.acquire(project, "codex")
    project.reload_config()
    assert workflow.tick(project, second)["card"] == bad["id"]
    assert not _has_error(project, fake, bad["id"])
    assert feedback.load(project.paths)["cards"] == {}


def test_reported_once_across_cycles_and_seats(kproject, fake_trello):
    project, fake = kproject, fake_trello
    bad = fake.add_card(project.cfg.backlog, "bad", desc=BAD, pos=1)
    user = fake.add_card(project.cfg.backlog, "user", desc=BAD, pos=2)
    fake.user_label(user["id"], project.cfg.label_error)
    leases = [seats.acquire(project, "claude"), seats.acquire(project, "codex")]
    project.reload_config()
    for _ in range(3):
        for lease in leases:
            assert workflow.tick(project, lease) is None
    assert len(_sections_comments(project, bad["id"])) == 1 and len(_sections_comments(project, user["id"])) == 1
    fake.state["cards"][bad["id"]]["desc"] = "spec\n## agent: claude deploy\nx\n"
    workflow.tick(project, leases[1])
    assert len(_sections_comments(project, bad["id"])) == 2
    for card in (bad, user):
        fake.state["cards"][card["id"]]["desc"] = "fixed spec\n"
    workflow.tick(project, leases[1])  # claims the top card, clearing its label in the claim
    workflow.tick(project, leases[0])
    assert not _has_error(project, fake, bad["id"])
    assert _has_error(project, fake, user["id"])  # the user's own Error stays


def test_review_claim_refused(kproject, fake_trello):
    project, fake = kproject, fake_trello
    claude = seats.acquire(project, "claude")
    codex = seats.acquire(project, "codex")
    project.reload_config()
    card = implement(project, fake, claude, "feature")
    implemented = fake.state["cards"][card["id"]]["idList"]
    fake.state["cards"][card["id"]]["desc"] = BAD
    assert workflow.tick(project, codex) is None
    assert _has_error(project, fake, card["id"]) and len(_sections_comments(project, card["id"])) == 1
    assert fake.state["cards"][card["id"]]["idList"] == implemented
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "implemented"


def test_edit_between_reads_clears_in_claim(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card = fake.add_card(project.cfg.backlog, "c", desc=BAD)
    lease = seats.acquire(project, "claude")
    assert workflow.tick(project, lease) is None
    assert _has_error(project, fake, card["id"])
    original = type(project.trello).snapshot
    calls = []
    def snapshot(self, board_id):
        calls.append(1)
        if len(calls) == 2:  # the claim's in-lock read sees the fix
            fake.state["cards"][card["id"]]["desc"] = "fixed\n"
        return original(self, board_id)
    monkeypatch.setattr(type(project.trello), "snapshot", snapshot)
    task = workflow.tick(project, lease)
    assert task["card"] == card["id"]
    assert not _has_error(project, fake, card["id"])


def test_adapter_change_reports_again(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card = fake.add_card(project.cfg.backlog, "c", desc=BAD)
    lease = seats.acquire(project, "claude")
    workflow.tick(project, lease)
    for module in (workflow, feedback):  # a real adapter change reaches every caller
        monkeypatch.setattr(module, "adapter_types", lambda paths=None: ["claude", "codex", "gemini"])
    workflow.tick(project, lease)
    assert len(_sections_comments(project, card["id"])) == 2


def test_periodic_board_check_clears_and_survives_failures(kproject, fake_trello):
    from tests.test_ask import FakeTmux, make_launcher
    project, fake = kproject, fake_trello
    bad = fake.add_card(project.cfg.backlog, "bad", desc=BAD, pos=1)
    work = fake.add_card(project.cfg.backlog, "work", pos=2)
    lease = seats.acquire(project, "claude")
    task = workflow.tick(project, lease)
    assert task["card"] == work["id"] and _has_error(project, fake, bad["id"])
    launcher = make_launcher(project, lease, FakeTmux(), task)
    fake.state["cards"][bad["id"]]["desc"] = "fixed\n"
    launcher.next_board_check = 0.0
    launcher.periodic_board_check()
    assert not _has_error(project, fake, bad["id"])
    fake.state["cards"][bad["id"]]["desc"] = BAD
    with project_lock_ctx(project):
        feedback.record_invalid_locked(project, project.trello.card(bad["id"]), "zz", ())
    fake.inject("GET", r"/1/cards/" + bad["id"], "500", times=100)
    fake.user_label(work["id"], project.cfg.label_hold)
    launcher.next_board_check = 0.0
    launcher.periodic_board_check()
    from kankan.seatstate import load_state
    assert load_state(project.paths, lease.seat).suspend_requested["reason"] == "hold"


def project_lock_ctx(project):
    from kankan.config import project_lock
    return project_lock(project.paths)
