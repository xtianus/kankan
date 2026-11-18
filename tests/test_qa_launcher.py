"""The Q&A launcher (questions T§4–T§5, F§3; acceptance tests F§8 1, 2, 3, 6, 7, 8, 12, 14): the conversation loop,
the delivery of follow-ups, signals per turn and the ends of a conversation, with the real supervisor → launcher →
fake agent chain in a private tmux server."""

import json
import os
import re
import signal
import time
from types import SimpleNamespace

import pytest

from kankan import agentcmds, auditlog, boardconfig, cli, faults, messages, qa, qa_answer, seats, workflow
from kankan.adapters import Classification, ModelEffort, load_adapter
from kankan.config import atomic_write_json
from kankan.errors import KankanError, Status
from kankan.qa_launcher import QaLauncher
from kankan.refstore import card_ref
from kankan.seatstate import load_state, update_state
from tests.flow import agent_ctx
from tests.helpers import add_qa_list, add_questions_seed, bind_project
from tests.launcher_helpers import (agent_events, agent_inputs, launcher_state, proc_info, seat_session, start_qa_agent,
                                    wait_for)

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=0.05, qa_poll_seconds=1)
SEAT = "claude-qa"
TOKEN = re.compile(r"turn `([0-9a-f]{16})`")


@pytest.fixture
def chain(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    project = bind_project(git_repo, fake_trello, monkeypatch, **FAST)
    add_questions_seed()
    env = SimpleNamespace(project=project, fake=fake_trello, tmux=tmux_server, tmp=tmp_path)
    env.qa_list = add_qa_list(project, fake_trello)
    return env


def answer(text):
    return {"do": [["kankan", "answer", "--turn", "{turn}", "--file", "{file}"]], "text": text}


def start(env, steps):
    _, log = start_qa_agent(env.tmux, env.project, scenario={"steps": steps}, tmp_path=env.tmp)
    return log


def labels_of(env, card):
    return env.fake.state["cards"][card["id"]]["idLabels"]


def with_text(env, card, needle):
    return [text for text in env.fake.card_comments(card["id"]) if needle in text]


def ref_meta(env, card):
    value = env.project.refs.read(card_ref(card["id"]))
    return value.meta if value else None


def state(env):
    return launcher_state(env.project, SEAT)


def prompts(log):
    """The prompts the agent received (not the new-conversation commands)."""
    return [line for line in agent_inputs(log) if line not in ("/clear", "/new")]


def tokens(log):
    return [m.group(1) for line in agent_inputs(log) if (m := TOKEN.search(line))]


def audit(env, event):
    return [e for e in auditlog.read(env.project.paths) if e["event"] == event]


def pane_writes(env):
    return [e["text"] for e in audit(env, "pane_write")]


def task_json(path):
    return json.loads(path.read_text())


def comment_new(task, comment_id):
    return next(c["new"] for c in task["comments"] if c["id"] == comment_id)


# ---------------------------------------------------------------------- conversation

def test_first_answer_leads_to_qa_waiting_without_labels(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [answer("It works like this.")])
    assert wait_for(lambda: with_text(env, card, "It works like this."), timeout=40)
    assert wait_for(lambda: state(env) == "qa_waiting", timeout=10)
    assert labels_of(env, card) == []
    assert env.fake.card_list(card["id"]) == env.qa_list
    assert ref_meta(env, card)["phase"] == "qa"
    first = agent_inputs(log)[:2]
    assert first[0] == "/clear" and first[1] == messages.QA_PROMPT.format(turn=tokens(log)[0])


def test_follow_up_is_delivered_once_with_forwarded(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [answer("first answer"), answer("second answer")])
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=40)
    env.fake.user_comment(card["id"], "and what about X?")
    # delivered within about one qa_poll_seconds (1 s here), plus the screen poll
    assert wait_for(lambda: len(prompts(log)) == 2, timeout=6)
    assert prompts(log)[1] == messages.QA_FOLLOWUP.format(counts="1 comment", turn=tokens(log)[1])
    assert wait_for(lambda: with_text(env, card, "second answer") and state(env) == "qa_waiting", timeout=20)
    time.sleep(3)
    assert len(prompts(log)) == 2  # delivered once
    assert len(with_text(env, card, messages.FORWARDED)) == 1
    assert tokens(log)[0] != tokens(log)[1]


def test_comment_during_working_is_delivered_after_the_answer(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    out = env.tmp / "task2.json"
    log = start(env, [
        {"shell": "sleep 4 && kankan answer --turn {turn} --file {file}", "text": "first answer"},
        {"shell": f"kankan task --json > {out} && kankan answer --turn {{turn}} --file {{file}}",
         "text": "second answer"}])
    assert wait_for(lambda: state(env) == "working" and prompts(log), timeout=40)
    late = env.fake.user_comment(card["id"], "one more thing")
    assert wait_for(lambda: with_text(env, card, "first answer"), timeout=20)
    assert wait_for(lambda: with_text(env, card, "second answer"), timeout=20)
    assert prompts(log)[1] == messages.QA_FOLLOWUP.format(counts="1 comment", turn=tokens(log)[1])
    data = task_json(out)["data"]
    assert comment_new(data, late["id"]) is True
    assert data["turn"] == tokens(log)[1]


def test_attachment_only_follow_up_is_named_in_the_prompt(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [answer("first answer"), answer("about the screenshot")])
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=40)
    env.fake.upload(card["id"], "screenshot.png", b"\x89PNG", "image/png")
    assert wait_for(lambda: len(prompts(log)) == 2, timeout=10)
    assert prompts(log)[1] == messages.QA_FOLLOWUP.format(counts="1 attachment (`screenshot.png`)",
                                                          turn=tokens(log)[1])
    assert wait_for(lambda: with_text(env, card, "about the screenshot"), timeout=20)


def test_fail_sets_error_and_a_follow_up_clears_it_with_the_failed_inputs(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="see the log")
    question = env.fake.user_comment(card["id"], "the log is attached")
    out = env.tmp / "task2.json"
    log = start(env, [
        {"do": [["kankan", "fail", "--turn", "{turn}", "cannot read the log"]]},
        {"shell": f"kankan task --json > {out} && kankan answer --turn {{turn}} --file {{file}}",
         "text": "now I see it"}])
    assert wait_for(lambda: env.project.cfg.label_error in labels_of(env, card), timeout=40)
    assert wait_for(lambda: state(env) == "qa_waiting", timeout=10)
    time.sleep(2)
    assert len(prompts(log)) == 1  # the failed turn's inputs alone are not delivered again
    followup = env.fake.user_comment(card["id"], "here it is again")
    assert wait_for(lambda: with_text(env, card, "now I see it"), timeout=20)
    assert env.project.cfg.label_error not in labels_of(env, card)
    assert prompts(log)[1] == messages.QA_FOLLOWUP.format(counts="2 comments", turn=tokens(log)[1])
    data = task_json(out)["data"]
    assert comment_new(data, question["id"]) is True and comment_new(data, followup["id"]) is True


# ---------------------------------------------------------------------- no signal, late signals

def test_no_signal_sets_error_and_a_follow_up_wakes_the_agent(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [{"sleep": 0.2}, answer("awake now")])
    assert wait_for(lambda: env.project.cfg.label_error in labels_of(env, card), timeout=40)
    assert wait_for(lambda: with_text(env, card, "without a signal"), timeout=10)
    assert wait_for(lambda: state(env) == "qa_waiting", timeout=10)
    env.fake.user_comment(card["id"], "hello?")
    assert wait_for(lambda: with_text(env, card, "awake now"), timeout=20)
    assert env.project.cfg.label_error not in labels_of(env, card)
    assert len(prompts(log)) == 2


def test_second_no_signal_in_a_later_turn_comments_again(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    start(env, [{"sleep": 0.2}, {"sleep": 0.2}])
    assert wait_for(lambda: len(with_text(env, card, "without a signal")) == 1, timeout=40)
    assert wait_for(lambda: state(env) == "qa_waiting", timeout=10)
    env.fake.user_comment(card["id"], "hello?")
    assert wait_for(lambda: len(with_text(env, card, "without a signal")) == 2, timeout=20)
    assert env.project.cfg.label_error in labels_of(env, card)


class _NoTmux:
    def capture(self, pane):
        return ""


def test_stale_signal_from_an_older_turn_is_dropped(kproject, fake_trello):
    project, fake = kproject, fake_trello
    add_questions_seed()
    qa_list = add_qa_list(project, fake)
    card = fake.add_card(qa_list, "how")
    launcher = QaLauncher(project, load_adapter(project.paths, "claude"), _NoTmux(), "claude", SEAT)
    launcher.lease = seats.acquire_qa(project, "claude", None)
    task = qa.claim(project, launcher.lease, project.trello.card(card["id"]), None)
    launcher.task = task
    old = launcher.begin_turn(qa.Inputs())
    new = launcher.begin_turn(qa.Inputs())
    assert old != new and launcher.turn == new
    agentcmds.write_signal(project, SEAT, task["id"], "answer", {"turn": old})
    assert launcher.take_signal() is None
    assert load_state(project.paths, SEAT).signal is None  # taken and dropped
    agentcmds.write_signal(project, SEAT, "another-task", "answer", {"turn": new})
    assert launcher.take_signal() is None
    agentcmds.write_signal(project, SEAT, task["id"], "fail", {"turn": new})
    assert launcher.take_signal()["kind"] == "fail"


# ---------------------------------------------------------------------- ends

def test_card_moved_to_done_while_working_ends_at_once(chain):
    env = chain
    first = env.fake.add_card(env.qa_list, "first", desc="question one")
    second = env.fake.add_card(env.qa_list, "second", desc="question two")
    out = env.tmp / "late.txt"
    log = start(env, [
        {"shell": f"sleep 6; kankan answer --turn {{turn}} --file {{file}} > {out} 2>&1", "text": "late answer"},
        {"do": [["kankan", "yield", "--json"]]},
        answer("second card answer")])
    assert wait_for(lambda: state(env) == "working" and prompts(log), timeout=40)
    env.fake.user_move(first["id"], env.project.cfg.done)
    assert wait_for(lambda: ref_meta(env, first) is None, timeout=5)  # ended at once, during the turn
    assert state(env) == "working"
    time.sleep(1)
    assert pane_writes(env).count("/clear") == 1  # the clear waits for "ready"
    assert wait_for(lambda: out.exists() and "not_owner" in out.read_text(), timeout=15)
    assert wait_for(lambda: with_text(env, second, "second card answer"), timeout=30)
    assert not with_text(env, first, "late answer")
    assert env.fake.card_list(first["id"]) == env.project.cfg.done  # never moved back
    lines = agent_inputs(log)
    suspend = lines.index(messages.QA_SUSPEND)
    assert lines[suspend + 1] == "/clear"
    assert lines[suspend + 2] == messages.QA_PROMPT.format(turn=tokens(log)[1])
    assert ref_meta(env, second)["phase"] == "qa"


def test_hold_in_qa_waiting_ends_the_conversation_and_the_card_stays(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [answer("first answer")])
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=40)
    env.fake.user_label(card["id"], env.project.cfg.label_hold)
    assert wait_for(lambda: ref_meta(env, card) is None, timeout=10)
    assert wait_for(lambda: state(env) == "idle", timeout=10)
    assert agent_inputs(log)[-1] == "/clear"
    assert env.fake.card_list(card["id"]) == env.qa_list
    time.sleep(2)
    assert ref_meta(env, card) is None and len(prompts(log)) == 1  # a card on HOLD is not taken again


def test_hold_in_working_pastes_qa_suspend_and_yield_succeeds(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    out = env.tmp / "yield.txt"
    log = start(env, [{"sleep": 5}, {"shell": f"kankan yield --json > {out} 2>&1"}])
    assert wait_for(lambda: state(env) == "working" and prompts(log), timeout=40)
    env.fake.user_label(card["id"], env.project.cfg.label_hold)
    assert wait_for(lambda: ref_meta(env, card) is None, timeout=5)
    assert wait_for(lambda: out.exists() and '"ok": true' in out.read_text(), timeout=20)
    assert wait_for(lambda: state(env) == "idle" and agent_inputs(log)[-1] == "/clear", timeout=20)
    assert messages.QA_SUSPEND in agent_inputs(log)
    assert messages.SUSPEND_REQUEST not in agent_inputs(log)
    assert env.fake.card_list(card["id"]) == env.qa_list
    assert not with_text(env, card, "without a signal")


def test_cards_on_hold_are_skipped(chain):
    env = chain
    held = env.fake.add_card(env.qa_list, "held", desc="later")
    env.fake.user_label(held["id"], env.project.cfg.label_hold)
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    start(env, [answer("the answer")])
    assert wait_for(lambda: with_text(env, card, "the answer"), timeout=40)
    assert ref_meta(env, held) is None and not env.fake.card_comments(held["id"])


# ---------------------------------------------------------------------- pane and list

def test_typing_in_the_pane_during_qa_waiting_starts_a_turn(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    out = env.tmp / "typed.json"
    log = start(env, [answer("first answer"), {"shell": f"sleep 3 && kankan task --json > {out}"}])
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=40)
    time.sleep(1.5)  # the launcher sees the ready screen first
    adapter = load_adapter(env.project.paths, "claude")
    env.tmux.paste(seat_session(env.project, SEAT), "what about Y?", adapter.submit_key)
    assert wait_for(lambda: out.exists() and out.stat().st_size > 0, timeout=20)
    data = task_json(out)["data"]
    conversation = ref_meta(env, card)["conversation"]
    assert data["turn"] != tokens(log)[0]
    assert data["turn"] == qa.read_turn(env.project.paths, conversation)["token"]
    assert load_state(env.project.paths, SEAT).task["turn"] == data["turn"]


def test_without_an_open_qa_list_the_launcher_exits_fatally_at_start(git_repo, fake_trello, monkeypatch, agent_path,
                                                                     tmux_server, tmp_path):
    project = bind_project(git_repo, fake_trello, monkeypatch, **FAST)
    add_questions_seed()
    session, _ = start_qa_agent(tmux_server, project, tmp_path=tmp_path)
    assert wait_for(lambda: any(e.get("status") == "config_error"
                                for e in auditlog.read(project.paths) if e["event"] == "launcher_error"), timeout=30)
    assert wait_for(lambda: "configuration problem" in tmux_server.capture(session), timeout=10)


def test_qa_list_archived_mid_run_exits_fatally_after_the_turn(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [{"shell": "sleep 4 && kankan answer --turn {turn} --file {file}", "text": "the answer"}])
    assert wait_for(lambda: state(env) == "working" and prompts(log), timeout=40)
    env.fake.state["lists"][env.qa_list]["closed"] = True
    assert wait_for(lambda: with_text(env, card, "the answer"), timeout=20)  # the current turn ends first
    assert wait_for(lambda: any(e.get("status") == "config_error" for e in audit(env, "launcher_error")), timeout=20)
    assert wait_for(lambda: "configuration problem" in env.tmux.capture(seat_session(env.project, SEAT)), timeout=10)
    assert ref_meta(env, card)["phase"] == "qa"  # the conversation is kept


# ---------------------------------------------------------------------- restarts, settings, stop and limits (Task 9)

class _PaneTmux:
    """A pane that records what the launcher pastes."""

    def __init__(self):
        self.pasted = []

    def capture(self, pane):
        return ""

    def paste(self, pane, text, submit_key):
        self.pasted.append(text)

    def client_activity(self, session):
        return None


READY = Classification("ready")
PAIR = ModelEffort("sonnet", "high")


@pytest.fixture
def unit(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    add_questions_seed()
    env = SimpleNamespace(project=project, fake=fake, monkeypatch=monkeypatch)
    env.qa_list = add_qa_list(project, fake)
    env.lease = seats.acquire_qa(project, "claude", None)
    return env


def make_launcher(env):
    launcher = QaLauncher(env.project, load_adapter(env.project.paths, "claude"), _PaneTmux(), "claude", SEAT)
    launcher.lease = env.lease
    launcher.running_pair = PAIR
    launcher.state = "idle"
    return launcher


def claimed(env, launcher, card):
    launcher.task = qa.claim(env.project, env.lease, env.project.trello.card(card["id"]), PAIR)
    return launcher.task


def restarted(env):
    """A new launcher run on the same seat (the old one crashed): it takes up the held conversation."""
    launcher = make_launcher(env)
    held = launcher.held_conversation()
    assert held is not None
    launcher.resume(held, READY)
    return launcher


def unit_meta(env, card):
    value = env.project.refs.read(card_ref(card["id"]))
    return value.meta if value else None


def test_crash_after_turn_saved_delivers_the_inputs_after_the_restart(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    first = make_launcher(env)
    task = claimed(env, first, card)
    question = env.fake.user_comment(card["id"], "and what about X?")["id"]
    env.monkeypatch.setenv("KANKAN_FAULT", "qa_after_turn_saved")
    with pytest.raises(faults.SimulatedCrash):
        first.begin_turn(qa.Inputs([question], []))
    env.monkeypatch.delenv("KANKAN_FAULT")
    assert first.tmux.pasted == []  # the prompt was never written
    launcher = restarted(env)
    turn = qa.read_turn(env.project.paths, task["conversation"])
    assert launcher.tmux.pasted == [messages.QA_RESTART.format(turn=turn["token"])]
    assert question in turn["inputs"]["comments"]
    current = load_state(env.project.paths, SEAT).task
    assert current["id"] != task["id"] and current["conversation"] == task["conversation"]
    assert current["turn"] == turn["token"] and launcher.state == "working"
    assert (current["base"], current["reflog"]) == (task["base"], task["reflog"])  # same seat: kept


def answered(env, launcher, card, text="the answer"):
    """Claim ``card``, start its first turn and answer it; returns the turn token."""
    claimed(env, launcher, card)
    token = launcher.begin_turn(qa.Inputs([], []))
    qa_answer.answer(agent_ctx(env.project, env.lease, env.monkeypatch), text, token)
    return token


def test_restart_in_qa_waiting_with_nothing_new_writes_no_prompt_and_no_comment(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    answered(env, make_launcher(env), card)
    task = load_state(env.project.paths, SEAT).task
    comments = len(env.fake.card_comments(card["id"]))
    launcher = restarted(env)
    assert launcher.tmux.pasted == [] and launcher.state == "qa_waiting"
    assert len(env.fake.card_comments(card["id"])) == comments
    current = load_state(env.project.paths, SEAT).task
    assert current["id"] != task["id"] and current["conversation"] == task["conversation"]
    launcher.poll_active()
    launcher.qa_waiting(READY)
    assert launcher.tmux.pasted == [] and launcher.state == "qa_waiting"  # still nothing to deliver
    # the next delivery tells the fresh agent to read the whole thread
    followup = env.fake.user_comment(card["id"], "and what about Y?")["id"]
    launcher.next_qa_poll = 0.0
    launcher.poll_active()
    launcher.qa_waiting(READY)
    turn = qa.read_turn(env.project.paths, task["conversation"])
    assert launcher.tmux.pasted == [messages.QA_RESTART_FOLLOWUP.format(counts="1 comment", turn=turn["token"])]
    assert turn["inputs"]["comments"] == [followup] and launcher.state == "working"


def test_restart_between_the_claim_and_the_first_turn_marks_only_the_items_after_the_latest_answer(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    old = env.fake.user_comment(card["id"], "an old question")["id"]
    env.project.trello.add_comment(card["id"], "an old answer", "qa-0123456789abcdef-0123456789abcdef-1")
    new = env.fake.user_comment(card["id"], "a new question")["id"]
    first = make_launcher(env)
    task = claimed(env, first, card)  # the launcher dies before the first turn: no turn.json
    assert qa.read_turn(env.project.paths, task["conversation"]) is None
    restarted(env)
    turn = qa.read_turn(env.project.paths, task["conversation"])
    assert turn["inputs"]["comments"] == [new] and turn["context"]["comments"] == [old]


LONG = (("word " * 199 + "word") + "\n\n") * 40


def broken_answer(env, launcher, card):
    """Claim ``card``, start a turn and let ``answer`` die after its first chunk while the launcher stays alive."""
    question = env.fake.user_comment(card["id"], "and what about X?")["id"]
    claimed(env, launcher, card)
    token = launcher.begin_turn(qa.Inputs([question], []))
    launcher.set_state("working")
    env.monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(agent_ctx(env.project, env.lease, env.monkeypatch), LONG, token)
    env.monkeypatch.delenv("KANKAN_FAULT")
    return question, token


def answer_chunks(env, card):
    return [t for t in env.fake.card_comments(card["id"]) if "`kankan:qa-" in t]


def test_no_signal_after_a_partly_posted_answer_completes_it_without_error(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    launcher = make_launcher(env)
    question, token = broken_answer(env, launcher, card)
    assert len(answer_chunks(env, card)) == 1
    launcher.report_inactivity(messages.NO_SIGNAL)
    chunks = answer_chunks(env, card)
    assert [c.split("\n", 1)[0] for c in chunks] == ["1/3", "2/3", "3/3"]
    assert sum("@" in c for c in chunks) == 1  # one mention, on the last chunk
    assert env.project.cfg.label_error not in env.fake.state["cards"][card["id"]]["idLabels"]
    assert not [t for t in env.fake.card_comments(card["id"]) if "without a signal" in t]
    assert unit_meta(env, card)["covered"]["comment"] == question
    assert launcher.state == "qa_waiting" and launcher.inactivity_reported is False
    assert qa_answer.read_pending(env.project.paths, unit_meta(env, card)["conversation"]) is None


def test_no_signal_while_the_answer_command_holds_the_conversation_lock_waits(unit):
    import threading
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    launcher = make_launcher(env)
    broken_answer(env, launcher, card)
    conversation = launcher.task["conversation"]
    held, release = threading.Event(), threading.Event()

    def posting():
        with qa.conversation_lock(env.project.paths, conversation, 5):
            held.set()
            release.wait(10)
    thread = threading.Thread(target=posting, daemon=True)
    thread.start()
    assert held.wait(5)
    try:
        launcher.report_inactivity(messages.NO_SIGNAL)
        assert launcher.inactivity_reported is False and launcher.state == "working"  # checked again later
        assert env.project.cfg.label_error not in env.fake.state["cards"][card["id"]]["idLabels"]
        assert len(answer_chunks(env, card)) == 1
    finally:
        release.set()
        thread.join(10)
    launcher.report_inactivity(messages.NO_SIGNAL)
    assert len(answer_chunks(env, card)) == 3 and launcher.state == "qa_waiting"


def test_delivery_completes_a_pending_answer_before_starting_a_new_turn(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    launcher = make_launcher(env)
    question, token = broken_answer(env, launcher, card)
    launcher.turn_over()  # e.g. the agent called `fail` after the broken answer
    later = env.fake.user_comment(card["id"], "and Y?")["id"]
    launcher.poll_active()
    assert launcher.followups is not None
    launcher.deliver(READY)
    assert len(answer_chunks(env, card)) == 3 and launcher.tmux.pasted == []
    assert qa.read_turn(env.project.paths, launcher.task["conversation"])["token"] == token  # no turn started yet
    launcher.next_qa_poll = 0.0
    launcher.poll_active()
    launcher.deliver(READY)
    turn = qa.read_turn(env.project.paths, launcher.task["conversation"])
    assert turn["inputs"]["comments"] == [later]  # the completed answer covered the question
    assert launcher.tmux.pasted == [messages.QA_FOLLOWUP.format(counts="1 comment", turn=turn["token"])]


def test_attachment_names_in_the_follow_up_prompt_are_sanitized(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how")
    att = env.fake.upload(card["id"], "evil\n\tname `x`\r.png", b"png", "image/png")
    launcher = make_launcher(env)
    launcher.card = env.project.trello.card(card["id"])
    assert launcher.counts(qa.Inputs([], [att["id"]])) == "1 attachment (`evil name 'x' .png`)"


def test_pending_answer_is_completed_before_the_restart_prompt(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    question = env.fake.user_comment(card["id"], "and what about X?")["id"]
    first = make_launcher(env)
    claimed(env, first, card)
    token = first.begin_turn(qa.Inputs([question], []))
    ctx = agent_ctx(env.project, env.lease, env.monkeypatch)
    text = (("word " * 199 + "word") + "\n\n") * 40
    env.monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(ctx, text, token)
    env.monkeypatch.delenv("KANKAN_FAULT")
    late = env.fake.user_comment(card["id"], "one more thing")["id"]
    launcher = restarted(env)
    posted = [t for t in env.fake.card_comments(card["id"]) if "`kankan:qa-" in t]
    assert [c.split("\n", 1)[0] for c in posted] == ["1/3", "2/3", "3/3"]
    assert unit_meta(env, card)["covered"]["comment"] == question
    events = [e["event"] for e in auditlog.read(env.project.paths)
              if e["event"] in ("qa_pending_completed", "pane_write")]
    assert events == ["qa_pending_completed", "pane_write"]
    turn = qa.read_turn(env.project.paths, unit_meta(env, card)["conversation"])
    assert launcher.tmux.pasted == [messages.QA_RESTART.format(turn=turn["token"])]
    assert turn["inputs"]["comments"] == [late]  # the comment added while the answer was written is new
    assert load_state(env.project.paths, SEAT).signal is None  # the completed answer's signal is not a turn's


def test_pending_handoff_is_completed_at_restart_and_ends_the_conversation(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="please add X")
    first = make_launcher(env)
    claimed(env, first, card)
    token = first.begin_turn(qa.Inputs([], []))
    ctx = agent_ctx(env.project, env.lease, env.monkeypatch)
    env.monkeypatch.setenv("KANKAN_FAULT", "qa_after_handoff_summary")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.handoff(ctx, "implement X", token)
    env.monkeypatch.delenv("KANKAN_FAULT")
    launcher = restarted(env)
    assert env.fake.card_list(card["id"]) == env.project.cfg.backlog
    assert unit_meta(env, card)["phase"] == "parked" and unit_meta(env, card)["handoff"]
    assert launcher.ended == "handoff" and launcher.state == "closing"
    assert not any(t.startswith(messages.QA_RESTART.split("{")[0]) for t in launcher.tmux.pasted)


def test_pending_operation_of_an_ended_conversation_goes_to_closing(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    first = make_launcher(env)
    claimed(env, first, card)
    token = first.begin_turn(qa.Inputs([], []))
    ctx = agent_ctx(env.project, env.lease, env.monkeypatch)
    env.monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(ctx, (("word " * 199 + "word") + "\n\n") * 40, token)
    env.monkeypatch.delenv("KANKAN_FAULT")
    launcher = make_launcher(env)
    held = launcher.held_conversation()
    env.fake.user_move(card["id"], env.project.cfg.done)
    original = qa_answer.complete_pending

    def ended_meanwhile(project, lease, task):
        qa.end(project, lease, task)  # the conversation ends between the take-over and the completion
        return original(project, lease, task)
    env.monkeypatch.setattr(qa_answer, "complete_pending", ended_meanwhile)
    launcher.resume(held, READY)
    assert launcher.state == "closing" and launcher.tmux.pasted == []
    assert len([t for t in env.fake.card_comments(card["id"]) if "`kankan:qa-" in t]) == 1


def test_failed_take_over_after_an_agent_restart_is_retried_by_the_next_idle_pass(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    launcher = make_launcher(env)
    task = claimed(env, launcher, card)
    launcher.begin_turn(qa.Inputs([], []))
    launcher.state = "idle"  # agent_ready after the planned restart
    original = qa.take_over
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise KankanError(Status.NETWORK_ERROR, "timeout")
        return original(*args, **kwargs)
    env.monkeypatch.setattr(qa, "take_over", flaky)
    launcher.after_restart_ready(READY)
    assert launcher.task is None and launcher.tmux.pasted == []
    launcher.next_tick = 0.0
    launcher.idle(READY)
    current = load_state(env.project.paths, SEAT).task
    assert len(calls) == 2 and current["id"] != task["id"] and current["conversation"] == task["conversation"]
    assert launcher.tmux.pasted == [messages.QA_RESTART.format(turn=current["turn"])]
    assert "/clear" not in launcher.tmux.pasted and launcher.state == "working"


def test_stop_before_the_first_prompt_keeps_the_seat_task(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    launcher = make_launcher(env)
    task = claimed(env, launcher, card)
    token = launcher.begin_turn(qa.Inputs([], []))
    launcher.pending_writes = ["/clear", messages.QA_PROMPT.format(turn=token)]
    update_state(env.project.paths, SEAT, lambda s: setattr(s, "stop_requested", True))
    launcher.flush_assignment(READY)
    assert launcher.tmux.pasted == [] and launcher.task is None
    kept = load_state(env.project.paths, SEAT).task
    assert kept["id"] == task["id"] and (kept["base"], kept["reflog"]) == (task["base"], task["reflog"])
    assert unit_meta(env, card)["phase"] == "qa"
    assert launcher.idle(READY) is True  # the stop is honoured; the conversation is kept


def test_failed_end_after_yield_is_retried_before_closing(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    launcher = make_launcher(env)
    claimed(env, launcher, card)
    token = launcher.begin_turn(qa.Inputs([], []))
    launcher.set_state("working")
    original = qa.end
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise KankanError(Status.NETWORK_ERROR, "timeout")
        return original(*args, **kwargs)
    env.monkeypatch.setattr(qa, "end", flaky)
    agentcmds.write_signal(env.project, SEAT, launcher.task["id"], "yield", {"turn": token})
    launcher.working(READY)
    assert launcher.state == "working" and launcher.ended is None and launcher.task is not None
    assert unit_meta(env, card)["phase"] == "qa"
    launcher.next_end_retry = 0.0
    launcher.working(READY)
    assert launcher.ended == "yield" and launcher.state == "closing"
    assert unit_meta(env, card) is None


def test_converge_handoffs_completes_an_unfinished_handoff_move(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="please add X")
    launcher = make_launcher(env)
    task = claimed(env, launcher, card)
    env.monkeypatch.setenv("KANKAN_FAULT", "qa_after_handoff_tx")
    with pytest.raises(faults.SimulatedCrash):
        qa.handoff_transition(env.project, env.lease, task, ["c1"])
    env.monkeypatch.delenv("KANKAN_FAULT")
    assert env.fake.card_list(card["id"]) == env.qa_list and unit_meta(env, card)["from_list"]
    launcher.converge_handoffs(workflow.card_refs(env.project))
    assert env.fake.card_list(card["id"]) == env.project.cfg.backlog
    meta = unit_meta(env, card)
    assert meta["phase"] == "parked" and not meta.get("from_list") and meta["handoff"] == ["c1"]
    assert audit(SimpleNamespace(project=env.project), "qa_handoff_converged")


def test_no_signal_after_a_completed_handoff_sets_no_error(unit):
    env = unit
    card = env.fake.add_card(env.qa_list, "how", desc="please add X")
    launcher = make_launcher(env)
    task = claimed(env, launcher, card)
    launcher.begin_turn(qa.Inputs([], []))
    launcher.set_state("working")
    qa.handoff_transition(env.project, env.lease, task, ["c1"])
    launcher.report_inactivity(messages.NO_SIGNAL)
    assert env.project.cfg.label_error not in env.fake.state["cards"][card["id"]]["idLabels"]
    assert not [t for t in env.fake.card_comments(card["id"]) if "without a signal" in t]
    assert launcher.ended == "handoff" and launcher.state == "closing"
    assert unit_meta(env, card)["phase"] == "parked"


def test_idle_pane_leaving_ready_forgets_the_clear(unit):
    env = unit
    launcher = make_launcher(env)
    launcher.cleared = True
    launcher.next_tick = float("inf")
    launcher.idle(READY)
    assert launcher.cleared is True
    launcher.idle(Classification("working"))  # the user typed into the idle pane
    assert launcher.cleared is False


def scenario_file(log):
    return log.parent / log.name.replace("agent-", "scenario-").replace(".log", ".json")


def rewrite_steps(log, steps, **extra):
    """The steps of the next agent the launcher starts (a new fake agent reads the scenario again)."""
    path = scenario_file(log)
    data = json.loads(path.read_text())
    data.update(steps=steps, **extra)
    path.write_text(json.dumps(data))


def kill_launcher(env):
    os.kill(proc_info(env.project, SEAT)["launcher"]["pid"], signal.SIGKILL)


def starts(log):
    return [e for e in agent_events(log) if e["event"] == "start"]


def set_questions_pair(env, model, effort="high"):
    current = boardconfig.load(env.project.paths)
    current.settings["questions"] = {"value": {"claude": {"model": model, "effort": effort}}, "revision": 1}
    atomic_write_json(env.project.paths.kankan_dir / "board-settings.json", current.to_dict())


def restart_prompts(log):
    return [line for line in agent_inputs(log) if line.startswith(messages.QA_RESTART.split("{")[0])]


def restart_followups(log):
    return [line for line in agent_inputs(log) if line.startswith(messages.QA_RESTART_FOLLOWUP.split("{")[0])]


def test_launcher_restart_in_qa_waiting_resumes_with_a_new_task(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [answer("first answer")])
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=40)
    before = load_state(env.project.paths, SEAT).task
    conversation = ref_meta(env, card)["conversation"]
    rewrite_steps(log, [answer("after the restart")])
    comments = len(env.fake.card_comments(card["id"]))
    kill_launcher(env)
    assert wait_for(lambda: load_state(env.project.paths, SEAT).task["id"] != before["id"]
                    and state(env) == "qa_waiting", timeout=40)
    time.sleep(2)
    # nothing new since the answer: the fresh agent gets no prompt and the card no comment (questions T§4)
    assert len(prompts(log)) == 1 and not restart_prompts(log)
    added = env.fake.card_comments(card["id"])[comments:]
    assert all("was recovered after its previous holder died" in text for text in added)  # the seat recovery only
    env.fake.user_comment(card["id"], "and what about X?")
    assert wait_for(lambda: with_text(env, card, "after the restart"), timeout=20)
    after = load_state(env.project.paths, SEAT).task
    assert after["id"] != before["id"] and after["conversation"] == conversation
    assert restart_followups(log) == [messages.QA_RESTART_FOLLOWUP.format(counts="1 comment", turn=tokens(log)[-1])]
    assert ref_meta(env, card)["phase"] == "qa" and ref_meta(env, card)["conversation"] == conversation
    assert "/clear" not in agent_inputs(log)[agent_inputs(log).index(restart_followups(log)[0]) - 1:]


def test_comment_added_while_the_answer_was_written_is_new_after_a_restart(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    out = env.tmp / "restart.json"
    log = start(env, [{"shell": "sleep 4 && kankan answer --turn {turn} --file {file} && sleep 60",
                       "text": "first answer"}])
    assert wait_for(lambda: state(env) == "working" and prompts(log), timeout=40)
    late = env.fake.user_comment(card["id"], "one more thing")
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=20)
    rewrite_steps(log, [{"shell": f"kankan task --json > {out} && kankan answer --turn {{turn}} --file {{file}}",
                         "text": "second answer"}])
    kill_launcher(env)  # the agent is still busy: the comment was never delivered
    assert wait_for(lambda: with_text(env, card, "second answer"), timeout=40)
    data = task_json(out)["data"]
    assert comment_new(data, late["id"]) is True
    assert len(prompts(log)) == 2 and restart_prompts(log)


def test_changed_questions_pair_restarts_the_agent_in_qa_waiting(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [answer("first answer")])
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=40)
    conversation = ref_meta(env, card)["conversation"]
    rewrite_steps(log, [answer("with opus")])
    set_questions_pair(env, "opus")
    assert wait_for(lambda: len(starts(log)) == 2, timeout=40)
    assert starts(log)[-1]["argv"][-4:] == ["--model", "opus", "--effort", "high"]
    assert wait_for(lambda: state(env) == "qa_waiting" and load_state(env.project.paths, SEAT).task["model"] == "opus",
                    timeout=30)
    time.sleep(2)
    assert not restart_prompts(log) and not with_text(env, card, "with opus")  # nothing new: no prompt
    env.fake.user_comment(card["id"], "and now?")
    assert wait_for(lambda: with_text(env, card, "with opus"), timeout=30)
    assert restart_followups(log) and ref_meta(env, card)["conversation"] == conversation
    task = load_state(env.project.paths, SEAT).task
    assert (task["model"], task["effort"]) == ("opus", "high")


def test_changed_questions_pair_restarts_the_agent_in_idle(chain):
    env = chain
    log = start(env, [])
    assert wait_for(lambda: state(env) == "idle", timeout=40)
    rewrite_steps(log, [answer("with opus")])  # the steps of the replacement agent
    set_questions_pair(env, "opus")
    assert wait_for(lambda: len(starts(log)) == 2, timeout=40)
    assert starts(log)[-1]["argv"][-4:] == ["--model", "opus", "--effort", "high"]
    assert wait_for(lambda: state(env) == "idle", timeout=20)
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    assert wait_for(lambda: with_text(env, card, "with opus"), timeout=30)
    assert load_state(env.project.paths, SEAT).task["model"] == "opus"


def test_pair_change_during_a_turn_waits_for_its_end(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [{"shell": "sleep 6 && kankan answer --turn {turn} --file {file}", "text": "slow answer"}])
    assert wait_for(lambda: state(env) == "working" and prompts(log), timeout=40)
    rewrite_steps(log, [])
    set_questions_pair(env, "opus")
    time.sleep(3)
    assert len(starts(log)) == 1 and state(env) == "working"
    assert wait_for(lambda: len(starts(log)) == 2, timeout=40)
    events = [e["event"] for e in agent_events(log)]
    assert events.index("shell") < [i for i, e in enumerate(events) if e == "start"][1]  # the turn ended first
    assert with_text(env, card, "slow answer")


def test_unstartable_questions_pair_keeps_the_conversation_without_fallback(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [answer("first answer")])
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=40)
    conversation = ref_meta(env, card)["conversation"]
    rewrite_steps(log, [], startup_exit=1)
    set_questions_pair(env, "opus")
    assert wait_for(lambda: any("exited before readiness" in e.get("error", "")
                                for e in audit(env, "launcher_error")), timeout=40)
    assert ref_meta(env, card)["phase"] == "qa" and ref_meta(env, card)["conversation"] == conversation
    assert wait_for(lambda: env.project.cfg.label_error in labels_of(env, card), timeout=10)
    assert wait_for(lambda: with_text(env, card, "could not start"), timeout=10)
    rewrite_steps(log, [answer("finally started")], startup_exit=0)
    assert wait_for(lambda: state(env) == "qa_waiting" and env.project.cfg.label_error not in labels_of(env, card),
                    timeout=60)
    env.fake.user_comment(card["id"], "are you back?")
    assert wait_for(lambda: with_text(env, card, "finally started"), timeout=30)
    assert all(s["argv"][-4:] == ["--model", "opus", "--effort", "high"] for s in starts(log)[1:])
    assert ref_meta(env, card)["conversation"] == conversation
    assert wait_for(lambda: env.project.cfg.label_error not in labels_of(env, card), timeout=10)


def test_stop_in_qa_waiting_keeps_the_conversation_until_the_next_start(chain, monkeypatch):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [answer("first answer")])
    assert wait_for(lambda: with_text(env, card, "first answer") and state(env) == "qa_waiting", timeout=40)
    conversation = ref_meta(env, card)["conversation"]
    monkeypatch.chdir(env.project.paths.repo)
    assert cli.main(["stop", SEAT]) == 0
    assert wait_for(lambda: not env.tmux.has_session(seat_session(env.project, SEAT)), timeout=30)
    assert ref_meta(env, card)["phase"] == "qa" and ref_meta(env, card)["conversation"] == conversation
    assert load_state(env.project.paths, SEAT).task["conversation"] == conversation
    followup = env.fake.user_comment(card["id"], "still there?")
    out = env.tmp / "after-stop.json"
    log2 = start(env, [{"shell": f"kankan task --json > {out} && kankan answer --turn {{turn}} --file {{file}}",
                        "text": "back again"}])
    assert wait_for(lambda: with_text(env, card, "back again"), timeout=40)
    assert restart_prompts(log2)
    data = task_json(out)["data"]
    assert comment_new(data, followup["id"]) is True


def test_usage_limit_sets_resume_and_follow_ups_wait_for_the_resumption(chain):
    env = chain
    card = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    log = start(env, [
        {"screen": "limit", "limit_text": "  ⎿  You've hit your session limit · resets 11:58pm",
         "shell": "sleep 6 && kankan answer --turn {turn} --file {file}", "text": "first answer"},
        answer("second answer")])

    def resume_labels():
        names = {lab["id"]: lab["name"] for lab in env.fake.state["labels"].values()}
        return [names.get(i, "") for i in labels_of(env, card) if names.get(i, "").startswith("Resume ")]
    assert wait_for(lambda: state(env) == "limited", timeout=40)
    assert wait_for(resume_labels, timeout=10)
    env.fake.user_comment(card["id"], "and what about X?")
    time.sleep(3)
    assert state(env) == "limited" and len(prompts(log)) == 1  # follow-ups wait for the resumption
    assert wait_for(lambda: with_text(env, card, "second answer"), timeout=40)
    assert prompts(log)[1] == messages.QA_FOLLOWUP.format(counts="1 comment", turn=tokens(log)[1])
    assert not resume_labels()
