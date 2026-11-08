import pytest

from kankan import agentcmds, cards, faults, integrate, seats, workflow
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref, seat_ref
from kankan.seatstate import load_state
from tests.conftest import git
from tests.flow import agent_ctx, commit, implement, review_setup


@pytest.fixture
def review(kproject, fake_trello, monkeypatch):
    card, codex, ctx = review_setup(kproject, fake_trello, monkeypatch, files={"one.txt": "1\n"})
    return kproject, fake_trello, card, codex, ctx


def tip(ctx):
    return git(ctx.worktree, "rev-parse", "HEAD")


def main_oid(project):
    return git(project.paths.repo, "rev-parse", "refs/heads/main")


def test_integrate_and_done(review):
    project, fake, card, codex, ctx = review
    result = integrate.integrate(ctx, tip(ctx))
    assert main_oid(project) == result["merge"]
    msg = git(project.paths.repo, "log", "-1", "--format=%B", "main")
    assert f"Kankan-Card: {card['shortLink']}" in msg and f"Kankan-Tested: {tip(ctx)}" in msg and card["url"] in msg
    assert integrate.integrate(ctx, tip(ctx)) == result
    out = integrate.done_review(ctx)
    assert out["phase"] == "done"
    assert fake.card_list(card["id"]) == project.cfg.done
    assert project.refs.read(card_ref(card["id"])) is None
    assert git(ctx.worktree, "symbolic-ref", "-q", "HEAD", check=False) == ""


def test_merge_tree_equals_tested(review):
    project, fake, card, codex, ctx = review
    tested = tip(ctx)
    merge = integrate.integrate(ctx, tested)["merge"]
    assert git(project.paths.repo, "rev-parse", f"{merge}^{{tree}}") == git(project.paths.repo, "rev-parse", f"{tested}^{{tree}}")
    assert git(project.paths.repo, "rev-parse", f"{merge}^2") == tested


def test_rejects_untested_or_wrong_tip(review):
    project, fake, card, codex, ctx = review
    old = tip(ctx)
    commit(ctx.worktree, "two.txt")
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, old)
    assert exc.value.status == Status.NEEDS_ACTION and "tip" in exc.value.message
    ctx.project.cfg.test_command = "true"
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, tip(ctx))
    assert "verify" in exc.value.message
    integrate.verify(ctx, tip(ctx))
    assert integrate.integrate(ctx, tip(ctx))["tested"] == tip(ctx)


def test_on_hold_refused(review):
    project, fake, card, codex, ctx = review
    fake.user_label(card["id"], project.cfg.label_hold)
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, tip(ctx))
    assert exc.value.status == Status.ON_HOLD
    assert main_oid(project) != tip(ctx)


def test_two_reviewers_one_main_moved_first_parent_chain(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    claude1, claude2 = seats.acquire(project, "claude"), seats.acquire(project, "claude")
    codex1, codex2 = seats.acquire(project, "codex"), seats.acquire(project, "codex")
    project.reload_config()
    card1 = implement(project, fake, claude1, "card1", {"one.txt": "1\n"})
    card2 = implement(project, fake, claude2, "card2", {"two.txt": "2\n"})
    t1, t2 = workflow.tick(project, codex1), workflow.tick(project, codex2)
    assert {t1["card"], t2["card"]} == {card1["id"], card2["id"]}
    ctx1 = agent_ctx(project, codex1, monkeypatch)
    ctx2 = agent_ctx(project, codex2, monkeypatch)
    merge_first = integrate.integrate(ctx1, tip(ctx1))["merge"]
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx2, tip(ctx2))
    assert exc.value.status == Status.MAIN_MOVED
    integrate.sync(ctx2)
    merge_second = integrate.integrate(ctx2, tip(ctx2))["merge"]
    chain = git(project.paths.repo, "rev-list", "--first-parent", "main").split()
    assert chain[:2] == [merge_second, merge_first]
    first_card = t1["card"]
    first_file = "one.txt" if first_card == card1["id"] else "two.txt"
    other_file = "two.txt" if first_file == "one.txt" else "one.txt"
    check = project.paths.worktree_root / "revert-check"
    git(project.paths.repo, "worktree", "add", "-q", "--detach", str(check), "main")
    git(check, "revert", "--no-edit", "-m", "1", merge_first)
    assert not (check / first_file).exists() and (check / other_file).exists()


def test_half_applied_commit_repaired(review):
    project, fake, card, codex, ctx = review
    tested = tip(ctx)
    repo = project.paths.repo
    old = main_oid(project)
    tree = git(repo, "rev-parse", f"{tested}^{{tree}}")
    message = f"t\n\nu\n\nKankan-Card: {card['shortLink']}\nKankan-Tested: {tested}\n"
    import subprocess
    merge = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-p", old, "-p", tested, "-F", "-"],
                           input=message, capture_output=True, text=True, check=True).stdout.strip()
    git(repo, "update-ref", "refs/heads/main", merge, old)
    result = integrate.integrate(ctx, tested)
    assert result["merge"] == merge and result.get("repaired")
    assert main_oid(project) == merge
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "integrated"


def test_done_after_convergence_deleted_ref_is_success(review):
    project, fake, card, codex, ctx = review
    integrate.integrate(ctx, tip(ctx))
    other = seats.acquire(project, "claude")
    cards.converge(project, other)
    assert project.refs.read(card_ref(card["id"])) is None
    assert integrate.done_review(ctx)["phase"] == "done"


def test_crash_after_main_no_second_merge(review, monkeypatch):
    project, fake, card, codex, ctx = review
    tested = tip(ctx)
    monkeypatch.setenv("KANKAN_FAULT", "after_main")
    with pytest.raises(faults.SimulatedCrash):
        integrate.integrate(ctx, tested)
    monkeypatch.delenv("KANKAN_FAULT")
    merge = main_oid(project)
    assert integrate.integrate(ctx, tested)["merge"] == merge
    cards.converge(project, ctx.lease)
    assert fake.card_list(card["id"]) == project.cfg.done
    assert project.refs.read(card_ref(card["id"])) is None
    assert git(project.paths.repo, "rev-list", "--count", "--merges", "main") == "1"


def test_integrate_after_release_force_does_not_advance_main(review, monkeypatch):
    project, fake, card, codex, ctx = review
    before = main_oid(project)
    real_point = faults.point

    def replace_seat(name):
        if name == "before_integrate_tx":
            other = project.refs.write_blob({"schema": 1, "kind": "seat", "session": "admin"})
            assert project.refs.update(seat_ref("codex-1"), other, ctx.lease.seat_oid)
        real_point(name)
    monkeypatch.setattr(faults, "point", replace_seat)
    with pytest.raises(KankanError) as exc:
        integrate.integrate(ctx, tip(ctx))
    assert exc.value.status == Status.NOT_OWNER
    assert main_oid(project) == before


def test_main_moved_limit_sets_error(review):
    project, fake, card, codex, ctx = review
    ctx.project.cfg.main_moved_retries = 1
    repo = project.paths.repo
    for name in ("m1", "m2"):
        git(repo, "checkout", "-q", "--detach")
        wt = project.paths.worktree_root / f"ed-{name}"
        git(repo, "worktree", "add", "-q", str(wt), "main")
        commit(wt, name)
        git(repo, "worktree", "remove", str(wt))
        with pytest.raises(KankanError):
            integrate.integrate(ctx, tip(ctx))
    assert project.cfg.label_error in fake.state["cards"][card["id"]]["idLabels"]
