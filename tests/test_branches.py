"""Integration branch `kankan-agents`, user branches and merge requests (spec §12.5)."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from kankan import agentcmds, diag, integrate, seats, workflow
from kankan.config import Paths, load_config, save_config
from kankan.errors import KankanError, Status
from kankan.init import init
from kankan.refstore import card_ref
from kankan.trello import TrelloClient
from tests.conftest import git
from tests.fakes.fake_trello import KEY, TOKEN
from tests.flow import agent_ctx, commit, implement
from tests.helpers import bind_project


# ---------------------------------------------------------------- init

@pytest.fixture
def env(fake_trello, monkeypatch):
    monkeypatch.setenv("KANKAN_TRELLO_URL", fake_trello.url)
    monkeypatch.setenv("KANKAN_TRELLO_KEY", KEY)
    monkeypatch.setenv("KANKAN_TRELLO_TOKEN", TOKEN)
    return fake_trello, fake_trello.add_board("proj", "Our board.")


def run_init(repo, board, *answers):
    queue = list(answers)
    return init(repo, board["url"], prompt=lambda q: queue.pop(0) if queue else "", say=lambda s: None)


def test_init_creates_kankan_agents(git_repo, env):
    fake, board = env
    cfg, warnings = run_init(git_repo, board)
    assert cfg.integration_branch == "kankan-agents" and cfg.user_branch == "main"
    assert git(git_repo, "rev-parse", "kankan-agents") == git(git_repo, "rev-parse", "main")
    assert git(git_repo, "config", "kankan.integrationBranch") == "kankan-agents"
    assert git(git_repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert not [w for w in warnings if "checked out" in w]


def test_init_adopts_remote_integration_branch(git_repo, env, tmp_path):
    fake, board = env
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(git_repo, "remote", "add", "origin", str(origin))
    git(git_repo, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(other))
    git(other, "switch", "-q", "-c", "kankan-agents")
    commit(other, "agents.txt")
    git(other, "push", "-q", "origin", "kankan-agents")
    run_init(git_repo, board)
    assert git(git_repo, "rev-parse", "kankan-agents") == git(other, "rev-parse", "kankan-agents")


def test_init_refuses_branch_change_with_card_refs(git_repo, env):
    fake, board = env
    cfg, _ = run_init(git_repo, board)
    from kankan.project import Project
    project = Project.open(git_repo)
    fake.add_card(cfg.backlog, "c")
    lease = seats.acquire(project, "claude")
    project.reload_config()
    workflow.tick(project, lease)
    seats.release(project, lease)
    with pytest.raises(KankanError) as exc:
        run_init(git_repo, board, "", "other")
    assert exc.value.status == Status.NEEDS_ACTION
    assert load_config(Paths.discover(git_repo)).integration_branch == "kankan-agents"


def test_init_user_branch_fallback(tmp_path, env):
    fake, board = env
    repo = tmp_path / "coding" / "trunkproj"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "trunk")
    (repo / "a").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    cfg, _ = run_init(repo, board)
    assert cfg.user_branch == "trunk"
    assert git(repo, "rev-parse", "kankan-agents") == git(repo, "rev-parse", "trunk")


def test_init_warns_when_integration_branch_checked_out(git_repo, env):
    fake, board = env
    git(git_repo, "branch", "kankan-agents")
    git(git_repo, "switch", "-q", "kankan-agents")
    _, warnings = run_init(git_repo, board)
    assert any("kankan-agents is checked out" in w for w in warnings)


def test_config_keys(git_repo):
    from kankan.config import Config
    base = dict(board_id="b", board_short="s", registry_id="r", backlog="", done="", blocked="", label_hold="",
                label_question="", label_error="", notify_id="", notify_username="", test_command="")
    assert Config.from_dict({**base, "main_branch": "main"}).integration_branch == "main"
    assert Config.from_dict({**base, "main_branch": "main", "integration_branch": "x"}).integration_branch == "x"
    assert "main_branch" not in Config.from_dict({**base, "main_branch": "main"}).to_dict()


def test_pre_push_refuses_integration_branch(git_repo, env, tmp_path):
    fake, board = env
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(git_repo, "remote", "add", "origin", str(origin))
    run_init(git_repo, board)
    refused = subprocess.run(["git", "-C", str(git_repo), "push", "-q", "origin", "kankan-agents"],
                             capture_output=True, text=True)
    assert refused.returncode != 0 and "only kankan moves kankan-agents" in refused.stderr
    allowed = subprocess.run(["git", "-C", str(git_repo), "push", "-q", "origin", "kankan-agents"],
                             capture_output=True, text=True, env={**os.environ, "KANKAN_PUSH": "1"})
    assert allowed.returncode == 0, allowed.stderr
    git(git_repo, "branch", "feat1")
    git(git_repo, "push", "-q", "origin", "feat1")


# ---------------------------------------------------------------- a project integrating into kankan-agents

@pytest.fixture
def kp(git_repo, fake_trello, monkeypatch):
    from kankan import seats as seats_mod
    monkeypatch.setattr(seats_mod, "KILL_GRACE", 0.5)
    git(git_repo, "branch", "kankan-agents")
    return bind_project(git_repo, fake_trello, monkeypatch, integration_branch="kankan-agents")


def user_commit(repo, branch, name, text=None, start="main"):
    """Commit on ``branch`` through a temporary worktree, never leaving the user's checkout."""
    wt = repo.parent / f"wt-{branch}-{name}"
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False):
        git(repo, "worktree", "add", "-q", str(wt), branch)
    else:
        git(repo, "worktree", "add", "-q", "-b", branch, str(wt), start)
    sha = commit(wt, name, text)
    git(repo, "worktree", "remove", str(wt))
    return sha


def review_and_integrate(project, fake, card_id, lease, monkeypatch):
    task = workflow.tick(project, lease)
    assert task["kind"] == "review" and task["card"] == card_id, task
    ctx = agent_ctx(project, lease, monkeypatch)
    integrate.sync(ctx)
    tip = git(ctx.worktree, "rev-parse", "HEAD")
    merge = integrate.integrate(ctx, tip)["merge"]
    integrate.done_review(ctx)
    return merge


def implement_merge_card(project, fake, lease, card, monkeypatch, resolve=None):
    """The implementer follows the merge-request description: merge the SHA, commit, done."""
    task = workflow.tick(project, lease)
    assert task["card"] == card.id, task
    sha = re.search(r"commit `([0-9a-f]{40})`", project.trello.card(card.id).desc).group(1)
    ctx = agent_ctx(project, lease, monkeypatch)
    proc = subprocess.run(["git", "-C", str(ctx.worktree), "merge", "--no-edit", sha], capture_output=True, text=True)
    if proc.returncode != 0:
        assert resolve is not None, proc.stdout + proc.stderr
        resolve(ctx)
    return agentcmds.done(ctx)


def first_parent_merges(repo, branch="kankan-agents"):
    return git(repo, "rev-list", "--first-parent", "--merges", branch).split()


def test_integrate_moves_only_integration_branch(kp, fake_trello, monkeypatch):
    repo = kp.paths.repo
    main_before = git(repo, "rev-parse", "main")
    (repo / "README.md").write_text("edited by the user\n")
    claude, codex = seats.acquire(kp, "claude"), seats.acquire(kp, "codex")
    kp.reload_config()
    card = implement(kp, fake_trello, claude, "feature")
    merge = review_and_integrate(kp, fake_trello, card["id"], codex, monkeypatch)
    assert git(repo, "rev-parse", "kankan-agents") == merge
    assert git(repo, "rev-parse", "main") == main_before
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert (repo / "README.md").read_text() == "edited by the user\n"
    assert git(repo, "status", "--porcelain") == "M README.md"


def test_merge_request_card(kp, fake_trello):
    repo = kp.paths.repo
    other = fake_trello.add_card(kp.cfg.backlog, "existing")
    sha = user_commit(repo, "feat1", "user.txt")
    card = integrate.merge_request(kp, "feat1")
    assert card.name == "Merge branch feat1 into kankan-agents"
    assert "feat1" in card.desc and sha in card.desc
    backlog = sorted((c for c in fake_trello.state["cards"].values() if c["idList"] == kp.cfg.backlog),
                     key=lambda c: c["pos"])
    assert [c["id"] for c in backlog] == [card.id, other["id"]]
    for bad in ("kankan-agents", "26kankan-Ab12Cd34", "main~1", "nope"):
        with pytest.raises(KankanError) as exc:
            integrate.merge_request(kp, bad)
        assert exc.value.status == Status.USAGE_ERROR, bad
    git(repo, "branch", "old", "kankan-agents")
    with pytest.raises(KankanError) as exc:
        integrate.merge_request(kp, "old")
    assert exc.value.status == Status.NEEDS_ACTION and "nothing to merge" in exc.value.message


def test_merge_request_remote(kp, fake_trello, tmp_path):
    repo = kp.paths.repo
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    elsewhere = tmp_path / "laptop"
    git(tmp_path, "clone", "-q", str(origin), str(elsewhere))
    git(elsewhere, "switch", "-q", "-c", "feat1")
    remote_sha = commit(elsewhere, "laptop.txt")
    git(elsewhere, "push", "-q", "origin", "feat1")
    stale = user_commit(repo, "feat1", "stale.txt")
    card = integrate.merge_request(kp, "feat1", remote=True)
    assert remote_sha in card.desc and stale not in card.desc
    assert git(repo, "rev-parse", "feat1") == stale
    assert git(repo, "rev-parse", "origin/feat1") == remote_sha
    git(repo, "branch", "-D", "feat1")
    git(repo, "update-ref", "-d", "refs/remotes/origin/feat1")
    assert remote_sha in integrate.merge_request(kp, "feat1").desc


@pytest.mark.parametrize("history", ["diverged", "merged_back", "trailers"])
def test_merge_request_histories(kp, fake_trello, monkeypatch, history):
    repo = kp.paths.repo
    claude, codex = seats.acquire(kp, "claude"), seats.acquire(kp, "codex")
    kp.reload_config()
    first = implement(kp, fake_trello, claude, "agentwork")
    review_and_integrate(kp, fake_trello, first["id"], codex, monkeypatch)
    if history == "merged_back":
        user_commit(repo, "feat1", "user1.txt")
        wt = repo.parent / "wt-merge-back"
        git(repo, "worktree", "add", "-q", str(wt), "feat1")
        git(wt, "merge", "-q", "--no-edit", "kankan-agents")
        git(repo, "worktree", "remove", str(wt))
    if history != "trailers":
        user_commit(repo, "feat1", "user.txt")
    else:
        wt = repo.parent / "wt-trailers"
        git(repo, "worktree", "add", "-q", "-b", "feat1", str(wt), "main")
        commit(wt, "user.txt", message="user work\n\nKankan-Card: Zz99Zz99\nKankan-Tested: deadbeef")
        git(repo, "worktree", "remove", str(wt))
    before = first_parent_merges(repo)
    card = integrate.merge_request(kp, "feat1")
    implement_merge_card(kp, fake_trello, claude, card, monkeypatch)
    review_and_integrate(kp, fake_trello, card.id, codex, monkeypatch)
    after = first_parent_merges(repo)
    assert len(after) == len(before) + 1
    assert git(repo, "show", "kankan-agents:user.txt") == "user.txt"
    assert git(repo, "show", "kankan-agents:agentwork.txt") == "agentwork"
    assert diag.reconcile(kp) == []


def test_merge_request_conflict_block_resolve(kp, fake_trello, monkeypatch):
    repo = kp.paths.repo
    claude, codex = seats.acquire(kp, "claude"), seats.acquire(kp, "codex")
    kp.reload_config()
    first = implement(kp, fake_trello, claude, "shared", files={"shared.txt": "agents\n"})
    review_and_integrate(kp, fake_trello, first["id"], codex, monkeypatch)
    user_commit(repo, "feat1", "shared.txt", "user\n")
    card = integrate.merge_request(kp, "feat1")

    def give_up(ctx):
        integrate.block(ctx, "cannot merge shared.txt safely")

    task = workflow.tick(kp, claude)
    assert task["card"] == card.id
    sha = re.search(r"commit `([0-9a-f]{40})`", kp.trello.card(card.id).desc).group(1)
    ctx = agent_ctx(kp, claude, monkeypatch)
    proc = subprocess.run(["git", "-C", str(ctx.worktree), "merge", "--no-edit", sha], capture_output=True, text=True)
    assert proc.returncode != 0
    give_up(ctx)
    assert fake_trello.card_list(card.id) == kp.cfg.blocked
    blocked = kp.paths.blocked_worktree(card.short)
    (blocked / "shared.txt").write_text("agents\nuser\n")
    git(blocked, "add", "shared.txt")
    git(blocked, "commit", "-q", "--no-edit")
    integrate.resolve(kp, card.short, done=True)
    kp.reload_config()
    review_and_integrate(kp, fake_trello, card.id, codex, monkeypatch)
    assert git(repo, "show", "kankan-agents:shared.txt") == "agents\nuser"


def test_merge_request_nothing_new_after_concurrent_integration(kp, fake_trello, monkeypatch):
    repo = kp.paths.repo
    claude, codex = seats.acquire(kp, "claude"), seats.acquire(kp, "codex")
    kp.reload_config()
    user_commit(repo, "feat1", "user.txt")
    card = integrate.merge_request(kp, "feat1")
    task = workflow.tick(kp, claude)
    assert task["card"] == card.id
    ctx = agent_ctx(kp, claude, monkeypatch)
    sha = re.search(r"commit `([0-9a-f]{40})`", kp.trello.card(card.id).desc).group(1)
    # the same commits reach kankan-agents another way before this card's done (simulated by a fast-forward)
    git(repo, "update-ref", "refs/heads/kankan-agents", sha)
    git(ctx.worktree, "merge", "-q", "--no-edit", sha)
    with pytest.raises(KankanError) as exc:
        agentcmds.done(ctx)
    assert exc.value.status == Status.NEEDS_ACTION
    git(ctx.worktree, "commit", "-q", "--allow-empty", "-m", "Nothing new to merge from feat1")
    agentcmds.done(ctx)
    merge = review_and_integrate(kp, fake_trello, card.id, codex, monkeypatch)
    assert git(repo, "rev-parse", "kankan-agents") == merge
    assert fake_trello.card_list(card.id) == kp.cfg.done


def test_status_ahead_behind(kp, fake_trello, monkeypatch):
    repo = kp.paths.repo
    claude, codex = seats.acquire(kp, "claude"), seats.acquire(kp, "codex")
    kp.reload_config()
    card = implement(kp, fake_trello, claude, "feature")
    merge = review_and_integrate(kp, fake_trello, card["id"], codex, monkeypatch)
    commit(repo, "mine.txt")  # the user commits on main, which is checked out
    data = diag.status(kp)["branches"]
    assert (data["user"], data["ahead"], data["behind"]) == ("main", 2, 1)
    assert data["oldest_unmerged_integration"] == git(repo, "log", "-1", "--format=%cI", merge)
    assert "kankan-agents is 2 commits ahead / 1 behind main" in diag.status_text(diag.status(kp))
    cfg = load_config(kp.paths)
    cfg.user_branch = "missing"
    save_config(kp.paths, cfg)
    assert diag.status(kp)["branches"]["user"] is None


def test_create_card_top(fake_trello):
    board = fake_trello.add_board("b")
    lst = fake_trello.add_list(board["id"], "Backlog")
    first = fake_trello.add_card(lst["id"], "first")
    client = TrelloClient(KEY, TOKEN, base_url=fake_trello.url, timeout=0.5)
    top = client.create_card(lst["id"], "top", pos="top")
    cards = sorted((c for c in fake_trello.state["cards"].values() if c["idList"] == lst["id"]), key=lambda c: c["pos"])
    assert [c["id"] for c in cards] == [top.id, first["id"]]


def test_merge_request_remote_fetch_failure_refuses(kp, fake_trello, tmp_path):
    """M3: a failed fetch never falls back to a stale remote-tracking ref."""
    repo = kp.paths.repo
    git(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    stale = user_commit(repo, "feat1", "stale.txt")
    git(repo, "update-ref", "refs/remotes/origin/feat1", stale)
    with pytest.raises(KankanError) as exc:
        integrate.merge_request(kp, "feat1", remote=True)
    assert exc.value.status == Status.NETWORK_ERROR
