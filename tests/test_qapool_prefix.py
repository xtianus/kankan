"""Title prefixes of Q&A cards and ``plain_title`` (qapool F§5 "Title prefix", T§8; F test 8)."""

import pytest

from kankan import agentcmds, diag, faults, integrate, qa, qapool, seats
from kankan.adapters import ModelEffort
from tests.conftest import git
from tests.flow import agent_ctx, review_setup
from tests.helpers import add_qa_list

WARM, COLD = "\U0001F7E2", "⚪"


@pytest.fixture
def env(kproject, fake_trello):
    qa_list = add_qa_list(kproject, fake_trello)
    card = fake_trello.add_card(qa_list, "How does it work?")
    return kproject, fake_trello, card


def name_of(fake, card):
    return fake.state["cards"][card["id"]]["name"]


def card_puts(fake, card):
    return [r for r in fake.requests if r == ("PUT", f"/1/cards/{card['id']}")]


def test_desired_prefix():
    assert qapool.desired_prefix(held=False, warm=False, ended_in_qa=False, in_qa=True) is None
    assert qapool.desired_prefix(held=True, warm=True, ended_in_qa=False, in_qa=True) == "warm"
    assert qapool.desired_prefix(held=True, warm=False, ended_in_qa=False, in_qa=True) == "cold"
    assert qapool.desired_prefix(held=False, warm=False, ended_in_qa=True, in_qa=True) == "cold"
    assert qapool.desired_prefix(held=True, warm=True, ended_in_qa=True, in_qa=False) is None
    assert qapool.desired_prefix(held=False, warm=False, ended_in_qa=True, in_qa=False) is None


def test_prefix_value(kproject):
    assert qapool.prefix_value(kproject.cfg, "warm") == WARM
    assert qapool.prefix_value(kproject.cfg, "cold") == COLD
    kproject.cfg.qa_prefix_cold = "zz"
    assert qapool.prefix_value(kproject.cfg, "cold") == "zz"
    with pytest.raises(ValueError):
        qapool.prefix_value(kproject.cfg, "hot")


def test_prefix_none_before_first_answer(env):
    project, fake, card = env
    fake.requests.clear()
    assert qapool.write_prefix(project, card["id"], None) == "unchanged"
    assert name_of(fake, card) == "How does it work?"
    assert fake.requests == []  # neither read nor written
    assert qapool.recorded(project.paths) == {}


def test_prefix_warm_then_cold_after_expiry(env):
    project, fake, card = env
    assert qapool.write_prefix(project, card["id"], "warm") == "written"
    assert name_of(fake, card) == f"{WARM} How does it work?"
    assert qapool.recorded(project.paths) == {card["id"]: WARM}
    fake.requests.clear()
    assert qapool.write_prefix(project, card["id"], "warm") == "unchanged"  # no change of state: not touched
    assert fake.requests == []
    assert qapool.write_prefix(project, card["id"], "cold") == "written"
    assert name_of(fake, card) == f"{COLD} How does it work?"
    assert qapool.recorded(project.paths) == {card["id"]: COLD}
    assert len(card_puts(fake, card)) == 1


def test_prefix_removed_on_done_and_handoff(env):
    project, fake, card = env
    qapool.write_prefix(project, card["id"], "warm")
    fake.state["cards"][card["id"]]["idList"] = project.cfg.done
    assert qapool.write_prefix(project, card["id"], None) == "written"
    assert name_of(fake, card) == "How does it work?"
    assert qapool.recorded(project.paths) == {}
    other = fake.add_card(project.cfg.qa_list, "Handed off")
    qapool.write_prefix(project, other["id"], "cold")
    assert qapool.write_prefix(project, other["id"], None) == "written"
    assert name_of(fake, other) == "Handed off"


def test_prefix_of_deleted_card_drops_record(env):
    project, fake, card = env
    qapool.write_prefix(project, card["id"], "warm")
    del fake.state["cards"][card["id"]]
    assert qapool.write_prefix(project, card["id"], "cold") == "deleted"
    assert qapool.recorded(project.paths) == {}


def test_prefixes_removed_at_router_start(env):
    project, fake, card = env
    other = fake.add_card(project.cfg.qa_list, "Second")
    qapool.write_prefix(project, card["id"], "warm")
    qapool.write_prefix(project, other["id"], "cold")
    results = qapool.clear_all(project)
    assert results == {card["id"]: "written", other["id"]: "written"}
    assert name_of(fake, card) == "How does it work?" and name_of(fake, other) == "Second"
    assert qapool.recorded(project.paths) == {}


def test_graceful_stop_writes_cold_on_warm_cards(env):
    project, fake, card = env
    other = fake.add_card(project.cfg.qa_list, "Second")
    qapool.write_prefix(project, card["id"], "warm")
    qapool.write_prefix(project, other["id"], "cold")
    fake.requests.clear()
    assert qapool.cold_all(project) == {card["id"]: "written"}
    assert name_of(fake, card) == f"{COLD} How does it work?" and name_of(fake, other) == f"{COLD} Second"
    assert card_puts(fake, other) == []


def test_user_edit_dropping_prefix_untouched(env):
    project, fake, card = env
    qapool.write_prefix(project, card["id"], "warm")
    fake.state["cards"][card["id"]]["name"] = "My own title"
    fake.requests.clear()
    assert qapool.write_prefix(project, card["id"], "warm") == "unchanged"  # same state: the user's title stays
    assert name_of(fake, card) == "My own title" and fake.requests == []
    assert qapool.write_prefix(project, card["id"], None) == "kept"
    assert name_of(fake, card) == "My own title"
    assert card_puts(fake, card) == []
    assert qapool.recorded(project.paths) == {}
    assert qapool.write_prefix(project, card["id"], "cold") == "written"  # next change of state
    assert name_of(fake, card) == f"{COLD} My own title"


def test_user_edit_then_change_of_state_prefixes_user_title(env):
    project, fake, card = env
    qapool.write_prefix(project, card["id"], "warm")
    fake.state["cards"][card["id"]]["name"] = f"{WARM}Edited"  # no longer the prefix and a space
    assert qapool.write_prefix(project, card["id"], "cold") == "written"
    assert name_of(fake, card) == f"{COLD} {WARM}Edited"


def test_several_prefix_characters(env):
    project, fake, card = env
    project.cfg.qa_prefix_warm = "\U0001F7E2\U0001F525"
    qapool.write_prefix(project, card["id"], "warm")
    assert name_of(fake, card) == "\U0001F7E2\U0001F525 How does it work?"
    assert qapool.plain_title(project.paths, project.trello.card(card["id"])) == "How does it work?"
    qapool.write_prefix(project, card["id"], "cold")
    assert name_of(fake, card) == f"{COLD} How does it work?"


def test_crash_after_prefix_write_leaves_unrecorded_prefix(env, monkeypatch):
    project, fake, card = env
    monkeypatch.setenv("KANKAN_FAULT", "after_prefix_write")
    with pytest.raises(faults.SimulatedCrash):
        qapool.write_prefix(project, card["id"], "warm")
    monkeypatch.delenv("KANKAN_FAULT")
    assert name_of(fake, card) == f"{WARM} How does it work?"
    assert qapool.recorded(project.paths) == {}
    assert qapool.write_prefix(project, card["id"], "cold") == "written"  # the unrecorded prefix is the user's text
    assert name_of(fake, card) == f"{COLD} {WARM} How does it work?"


def test_plain_title(env):
    project, fake, card = env
    assert qapool.plain_title(project.paths, project.trello.card(card["id"])) == "How does it work?"
    qapool.write_prefix(project, card["id"], "warm")
    assert qapool.plain_title(project.paths, project.trello.card(card["id"])) == "How does it work?"
    fake.state["cards"][card["id"]]["name"] = f"{WARM}x"
    assert qapool.plain_title(project.paths, project.trello.card(card["id"])) == f"{WARM}x"


def test_plain_title_in_qa_task(env, monkeypatch):
    project, fake, card = env
    lease = seats.acquire_qa(project, "claude", None)
    task = qa.claim(project, lease, project.trello.card(card["id"]), ModelEffort("sonnet", "high"))
    qa.start_turn(project, lease, task, qa.Inputs([], []))
    qapool.write_prefix(project, card["id"], "warm")
    assert agentcmds.task(agent_ctx(project, lease, monkeypatch))["card"]["title"] == "How does it work?"


def test_plain_title_in_task_and_merge_commit(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch, files={"one.txt": "1\n"})
    title = fake.state["cards"][card["id"]]["name"]
    qapool.write_prefix(project, card["id"], "cold")
    assert fake.state["cards"][card["id"]]["name"] == f"{COLD} {title}"
    assert agentcmds.task(ctx)["title"] == title
    integrate.integrate(ctx, git(ctx.worktree, "rev-parse", "HEAD"))
    message = git(project.paths.repo, "log", "-1", "--format=%B", "main")
    assert message.splitlines()[0] == title


def test_plain_title_in_status(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = fake.add_card(project.cfg.backlog, "Waiting card")
    fake.state["cards"][card["id"]]["idLabels"].append(project.cfg.label_hold)  # waits: reason hold
    qapool.write_prefix(project, card["id"], "cold")
    rows = [row for row in diag.status(project)["waiting"] if row["card"] == card["id"]]
    assert [row["title"] for row in rows] == ["Waiting card"]
