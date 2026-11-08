import pytest

from kankan import cards, integrate, seats, workflow
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref
from kankan.seatstate import load_state
from tests.conftest import git
from tests.flow import agent_ctx, commit, review_setup
from tests.helpers import bind_project
from tests.launcher_helpers import agent_events, launcher_state, start_agent, wait_for


@pytest.fixture
def board(kproject, fake_trello):
    lease = seats.acquire(kproject, "claude")
    kproject.reload_config()
    return kproject, fake_trello, lease


def test_hold_choosing_new_card_rechecked_after_claim(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    snap = project.trello.snapshot(project.cfg.board_id)
    choice = workflow.choose(project, lease, snap, {})
    fake.user_label(card["id"], project.cfg.label_hold)
    assert workflow.claim_impl(project, lease, choice) is None
    assert fake.card_list(card["id"]) == project.cfg.backlog
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "parked"
    assert workflow.tick(project, lease) is None


def test_hold_resuming_clean_kankan_yields(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    workflow.tick(project, lease)
    fake.user_label(card["id"], project.cfg.label_hold)
    assert workflow.tick(project, lease) is None
    assert fake.card_list(card["id"]) == project.cfg.backlog
    assert project.refs.read(card_ref(card["id"])).meta["cause"] == "yield"
    assert git(project.paths.seat_worktree("claude-1"), "symbolic-ref", "-q", "HEAD", check=False) == ""
    assert project.cfg.label_hold in fake.state["cards"][card["id"]]["idLabels"]


def test_hold_resuming_dirty_assigns_wip_task(board, monkeypatch):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    workflow.tick(project, lease)
    wt = project.paths.seat_worktree("claude-1")
    (wt / "wip.txt").write_text("work in progress")
    fake.user_label(card["id"], project.cfg.label_hold)
    task = workflow.tick(project, lease)
    assert task["kind"] == "hold_wip"
    ctx = agent_ctx(project, lease, monkeypatch)
    from kankan import agentcmds
    assert agentcmds.task(ctx)["hold"] is True
    with pytest.raises(KankanError) as exc:
        agentcmds.yield_(ctx)
    assert exc.value.status == Status.NEEDS_ACTION
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "WIP")
    agentcmds.yield_(ctx)
    assert fake.card_list(card["id"]) == project.cfg.backlog
    assert agentcmds.yield_(ctx)["phase"] == "parked"  # idempotent


def test_hold_removed_card_restarts_from_branch(board):
    project, fake, lease = board
    card = fake.add_card(project.cfg.backlog, "c")
    first = workflow.tick(project, lease)
    wt = project.paths.seat_worktree("claude-1")
    commit(wt, "a.txt")
    fake.user_label(card["id"], project.cfg.label_hold)
    workflow.tick(project, lease)
    fake.user_label(card["id"], project.cfg.label_hold, add=False)
    again = workflow.tick(project, lease)
    assert again["card"] == card["id"] and again["branch"] == first["branch"]
    assert (wt / "a.txt").exists()


def test_hold_review_yield_goes_to_implementer(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    from kankan import agentcmds
    agentcmds.yield_(ctx)
    assert fake.card_list(card["id"]) == project.reload_config().seats["claude-1"].implemented
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "implemented"


def test_hold_on_integrate(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    fake.user_label(card["id"], project.cfg.label_hold)
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    assert exc.value.status == Status.ON_HOLD


def test_hold_after_integration_goes_done_with_comment(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    merge = integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))["merge"]
    fake.user_label(card["id"], project.cfg.label_hold)
    cards.converge(project, ctx.lease)
    assert fake.card_list(card["id"]) == project.cfg.done
    assert any(f"git revert -m 1 {merge}" in c for c in fake.card_comments(card["id"]))
    assert project.cfg.label_hold in fake.state["cards"][card["id"]]["idLabels"]


@pytest.mark.tmux
@pytest.mark.slow
def test_hold_during_work_queues_never_escape(git_repo, fake_trello, monkeypatch, agent_path, tmux_server):
    project = bind_project(git_repo, fake_trello, monkeypatch, poll_seconds=1, write_quiet_seconds=0,
                           idle_ready_minutes=1)
    fake = fake_trello
    card = fake.add_card(project.cfg.backlog, "c")
    wip = "echo wip > wip.txt && git add wip.txt && git commit -qm WIP && kankan yield --json"
    _, log = start_agent(tmux_server, project, scenario={"steps": [{"sleep": 6}, {"shell": wip}]})
    assert wait_for(lambda: launcher_state(project, "claude-1") == "working", timeout=30)
    fake.user_label(card["id"], project.cfg.label_hold)
    assert wait_for(lambda: fake.card_list(card["id"]) == project.cfg.backlog, timeout=40)
    inputs = [e["line"] for e in agent_events(log) if e["event"] == "input"]
    assert sum("must be suspended" in line for line in inputs) == 1
    assert not any("\x1b" in line for line in inputs)
    branch = project.refs.read(card_ref(card["id"])).meta["branch"]
    assert git(project.paths.repo, "log", "-1", "--format=%s", branch) == "WIP"
    top = min((c for c in fake.state["cards"].values() if c["idList"] == project.cfg.backlog), key=lambda c: c["pos"])
    assert top["id"] == card["id"]
