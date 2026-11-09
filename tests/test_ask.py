import time

import pytest

from kankan import agentcmds, faults, seats, workflow
from kankan.adapters import load_adapter
from kankan.launcher import Launcher
from kankan.seatstate import load_state, update_state
from tests.flow import agent_ctx
from tests.fakes.fake_trello import FakeTrello

READY = "────\n❯ \n"
WORKING = "✶ Working… (3s · esc to interrupt)\n"


class FakeTmux:
    def __init__(self):
        self.screen = READY
        self.pastes = []
        self.keys = []
        self.activity = None

    def capture(self, pane):
        return self.screen

    def paste(self, pane, text, submit):
        self.pastes.append(text)

    def send_keys(self, pane, keys):
        self.keys.append(keys)

    def client_activity(self, session):
        return self.activity


@pytest.fixture
def waiting(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    project.cfg.poll_seconds = 0
    card = fake.add_card(project.cfg.backlog, "c")
    lease = seats.acquire(project, "claude")
    task = workflow.tick(project, lease)
    ctx = agent_ctx(project, lease, monkeypatch)
    agentcmds.ask(ctx, "Which database?")
    tmux = FakeTmux()
    launcher = make_launcher(project, lease, tmux, task)
    launcher.working(launcher.screen())
    assert launcher.state == "waiting_human"
    return project, fake, card, launcher, tmux


def make_launcher(project, lease, tmux, task):
    launcher = Launcher(project, load_adapter(project.paths, "claude"), tmux, "claude")
    launcher.lease, launcher.pane, launcher.session, launcher.task = lease, "%1", "s", task
    launcher.state = "working"
    return launcher


def labels_of(fake, card):
    return fake.state["cards"][card["id"]]["idLabels"]


def test_ask_forward_once(waiting):
    project, fake, card, launcher, tmux = waiting
    assert project.cfg.label_question in labels_of(fake, card)
    question = [c for c in fake.card_comments(card["id"]) if "Which database?" in c]
    assert question and "@boardowner" in question[0] and "@kankanbot" not in question[0]
    fake.user_comment(card["id"], "Use PostgreSQL.")
    launcher.step()
    launcher.step()
    assert tmux.pastes == ["Answer from the user on Trello: Use PostgreSQL."]
    assert project.cfg.label_question not in labels_of(fake, card)
    assert any("forwarded to the agent" in c for c in fake.card_comments(card["id"]))
    assert launcher.state == "working"


def test_own_comments_not_forwarded(waiting):
    project, fake, card, launcher, tmux = waiting
    fake.user_comment(card["id"], "written by kankan's account", fake.me["id"])
    fake.user_comment(card["id"], "kankan note\n\n`kankan:abc`")
    launcher.step()
    launcher.step()
    assert tmux.pastes == []
    assert launcher.state == "waiting_human"


def test_forward_deferred_while_user_typing_not_lost(waiting):
    project, fake, card, launcher, tmux = waiting
    project.cfg.write_quiet_seconds = 20
    tmux.activity = time.time()
    fake.user_comment(card["id"], "answer")
    launcher.step()
    launcher.step()
    assert tmux.pastes == []
    tmux.activity = time.time() - 60
    launcher.step()
    assert tmux.pastes == ["Answer from the user on Trello: answer"]


def test_forward_paginated_comments(waiting):
    project, fake, card, launcher, tmux = waiting
    for i in range(60):
        fake.user_comment(card["id"], f"part {i}")
    launcher.step()
    assert len(tmux.pastes) == 1 and "part 0" in tmux.pastes[0] and "part 59" in tmux.pastes[0]


def test_no_inactivity_error_while_waiting(waiting):
    project, fake, card, launcher, tmux = waiting
    project.cfg.idle_ready_minutes = 0
    for _ in range(3):
        launcher.step()
    assert project.cfg.label_error not in labels_of(fake, card)


def test_tmux_reply_detected(waiting):
    project, fake, card, launcher, tmux = waiting
    tmux.screen = WORKING
    launcher.step()
    assert launcher.state == "working"
    assert project.cfg.label_question not in labels_of(fake, card)
    assert any("resumed" in c for c in fake.card_comments(card["id"]))


def test_fail_sets_error_and_waits(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card = fake.add_card(project.cfg.backlog, "c")
    lease = seats.acquire(project, "claude")
    workflow.tick(project, lease)
    agentcmds.fail(agent_ctx(project, lease, monkeypatch), "tests need a database")
    assert project.cfg.label_error in labels_of(fake, card)
    assert load_state(project.paths, "claude-1").signal["kind"] == "fail"


@pytest.mark.parametrize("point,expected", [("after_cursor_save", 1), ("before_paste", 0)])
def test_crash_between_cursor_and_paste_loses_not_duplicates(waiting, monkeypatch, point, expected):
    project, fake, card, launcher, tmux = waiting
    fake.user_comment(card["id"], "the answer")
    monkeypatch.setenv("KANKAN_FAULT", point)
    with pytest.raises(faults.SimulatedCrash):
        launcher.step()
    monkeypatch.delenv("KANKAN_FAULT")
    restarted = make_launcher(project, launcher.lease, tmux, launcher.task)
    restarted._drop_interrupted_forward()
    restarted.state = "waiting_human"
    for _ in range(3):
        restarted.step()
    assert len([p for p in tmux.pastes if "the answer" in p]) == expected


def test_mentions_card_creator_or_board_admins(kproject, fake_trello):
    """The card's creator is mentioned; for cards kankan created, the board's admins (never kankan itself)."""
    from kankan import notify
    alice = fake_trello.add_member("alice")
    by_alice = fake_trello.add_card(kproject.cfg.backlog, "a", creator=alice["id"])
    by_kankan = fake_trello.add_card(kproject.cfg.backlog, "k", creator=fake_trello.me["id"])
    assert notify.mention(kproject, by_alice["id"]) == "@alice"
    assert notify.mention(kproject, by_kankan["id"]) == "@boardowner"


def test_answers_from_any_member_but_kankan_are_forwarded(waiting):
    project, fake, card, launcher, tmux = waiting
    teammate, outsider = fake.add_member("teammate"), fake.add_member("outsider")
    fake.state["boards"][project.cfg.board_id]["memberships"].append({"idMember": teammate["id"], "memberType": "normal"})
    fake.user_comment(card["id"], "kankan's own words", fake.me["id"])
    fake.user_comment(card["id"], "rm -rf everything", outsider["id"])  # not on the board: never forwarded
    fake.user_comment(card["id"], "Use SQLite.", teammate["id"])
    launcher.step()
    launcher.step()
    assert tmux.pastes == ["Answer from the user on Trello: Use SQLite."]
