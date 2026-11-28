"""The Q&A pool end to end (qapool F§4, F§8 acceptance tests 1–6, 9–11, 19, 20, 22, 25; T§5–T§6): the router
``claude-qa`` routes the ``Q&A`` cards to its own agent and to the members it opens and closes, with the real
supervisor → launcher → fake agent chains in a private tmux server and the fake Trello. Members get their scenario
from ``<FAKE_AGENT_SCENARIO_DIR>/<seat>.json``; the fake agent reports ``expires_at = now + cache_seconds`` through
``kankan statusline`` after each step, so ``cache_seconds: 0`` makes an agent cold once it answered."""

import json
import os
import shutil
import signal
from types import SimpleNamespace

import pytest

from kankan import agents_cmd, auditlog, faults, messages, qapool, seats
from kankan.config import SeatConfig, load_config, save_config
from kankan.refstore import card_ref
from tests.helpers import add_qa_list, add_questions_seed, bind_project
from tests.launcher_helpers import (agent_inputs, launcher_state, proc_info, seat_session, start_qa_agent,
                                    wait_for)

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=0.05, qa_poll_seconds=1)
ROUTER, QA2, QA3, QA4 = "claude-qa", "claude-qa2", "claude-qa3", "claude-qa4"
WARM, COLD = "\U0001F7E2 ", "⚪ "
LIMIT = {"screen": "limit", "limit_text": "  ⎿  You've hit your session limit · resets 11:58pm", "sleep": 600}
LONG = {"screen": "working", "sleep": 600}  # a turn that runs until its agent is killed


# ---------------------------------------------------------------------- harness

@pytest.fixture
def pool(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    monkeypatch.setenv("FAKE_AGENT_SCENARIO_DIR", str(scenarios))  # inherited by the tmux server, started later
    project = bind_project(git_repo, fake_trello, monkeypatch, **FAST)
    add_questions_seed()
    env = SimpleNamespace(project=project, fake=fake_trello, tmux=tmux_server, tmp=tmp_path, scenarios=scenarios,
                          bindir=agent_path, logs={})
    env.qa_list = add_qa_list(project, fake_trello)
    env.done = fake_trello.list_by_name(project.cfg.board_id, "Done")[0]["id"]
    return env


def answer(text):
    return {"do": [["kankan", "answer", "--turn", "{turn}", "--file", "{file}"]], "text": text}


def late_answer(text, seconds):
    """A turn that answers after ``seconds``: the agent is "working" meanwhile."""
    return {"shell": f"sleep {seconds} && kankan answer --turn {{turn}} --file {{file}}", "text": text}


def scenario(env, seat, steps, cache_seconds=3600, tag=""):
    """The seat's scenario, read by its fake agent when the agent process starts (each start replays it from its
    first step); returns the agent's log."""
    log = env.tmp / f"agent-{seat}{tag}.log"
    (env.scenarios / f"{seat}.json").write_text(json.dumps({"steps": steps, "log": str(log),
                                                            "cache_seconds": cache_seconds}))
    env.logs[seat] = log
    return log


def start_router(env, steps, cache_seconds=3600):
    log = scenario(env, ROUTER, steps, cache_seconds)
    start_qa_agent(env.tmux, env.project, scenario={"steps": steps, "log": str(log), "cache_seconds": cache_seconds},
                   tmp_path=env.tmp)
    return log


def prompts(log):
    """The prompts the agent received (not the new-conversation commands)."""
    return [line for line in agent_inputs(log) if line not in ("/clear", "/new")]


def card(env, name, desc=None, pos=None):
    return env.fake.add_card(env.qa_list, name, desc=desc or f"{name}?", pos=pos)


def answered(env, c, text):
    return [t for t in env.fake.card_comments(c["id"]) if text in t]


def title(env, c):
    return env.fake.state["cards"][c["id"]]["name"]


def holder(env, c):
    value = env.project.refs.read(card_ref(c["id"]))
    return value.meta.get("owner") if value is not None and value.meta.get("phase") == "qa" else None


def registered(env):
    return set(env.project.reload_config().seats)


def events(env, name, **match):
    return [e for e in auditlog.read(env.project.paths)
            if e["event"] == name and all(e.get(k) == v for k, v in match.items())]


def routes(env):
    return len(events(env, "poll_done", kind="route"))


def wait_routes(env, rounds=2, timeout=30):
    """Wait until the router has run ``rounds`` more routing rounds (one per ``qa_poll_seconds``)."""
    start = routes(env)
    assert wait_for(lambda: routes(env) >= start + rounds, timeout=timeout), f"no {rounds} routing rounds"


def state(env, seat):
    return launcher_state(env.project, seat)


def wait_answer(env, c, text, seat, timeout=60):
    """``seat`` answered the card with ``text`` and waits for the user again."""
    assert wait_for(lambda: answered(env, c, text), timeout=timeout), f"{text!r} not posted"
    assert wait_for(lambda: state(env, seat) == "qa_waiting", timeout=20), f"{seat} not waiting"


def cold(env, seat):
    return not qapool.warm(env.project, seat)


def removed(env, seat):
    """The member's seat is gone: configuration entry and worktree."""
    return seat not in registered(env) and not env.project.paths.seat_worktree(seat).exists()


def first_prompt_after_clear(log, index):
    """The ``index``-th prompt (0-based) is a first turn of a new session: ``/clear`` came right before it."""
    lines = agent_inputs(log)
    position = [i for i, line in enumerate(lines) if line not in ("/clear", "/new")][index]
    return lines[position - 1] == "/clear" and lines[position].startswith(messages.QA_PROMPT.split("`")[0])


def at_once(env, *changes):
    """Board changes made together: no Trello read sees one without the others (the fake serves each request under
    its lock)."""
    with env.fake.lock:
        for change in changes:
            change()


# ---------------------------------------------------------------------- F§8 1, 2: a warm router, a new card

def test_new_card_opens_a_member_while_the_router_is_warm(pool):
    """F test 1: a new card while ``claude-qa`` waits warm on another: ``claude-qa2`` opens and answers it; both
    cards get the warm prefix."""
    env = pool
    a = card(env, "first")
    rlog = start_router(env, [answer("Answer one.")])
    wait_answer(env, a, "Answer one.", ROUTER)
    assert qapool.warm(env.project, ROUTER)
    mlog = scenario(env, QA2, [answer("Answer two.")])
    b = card(env, "second")
    wait_answer(env, b, "Answer two.", QA2)
    assert holder(env, a) == ROUTER and holder(env, b) == QA2
    assert len(prompts(rlog)) == 1 and len(prompts(mlog)) == 1
    assert QA2 in registered(env) and env.project.paths.seat_worktree(QA2).exists()
    assert wait_for(lambda: title(env, a) == WARM + "first" and title(env, b) == WARM + "second", timeout=15)


def test_follow_ups_stay_with_the_warm_router(pool):
    """F test 2: a follow-up is answered by ``claude-qa`` in the same session; one arriving while it answers is
    delivered after its answer; no member opens."""
    env = pool
    a = card(env, "first")
    rlog = start_router(env, [answer("Answer one."), late_answer("Answer two.", 4), answer("Answer three.")])
    wait_answer(env, a, "Answer one.", ROUTER)
    env.fake.user_comment(a["id"], "and X?")
    assert wait_for(lambda: state(env, ROUTER) == "working" and len(prompts(rlog)) == 2, timeout=15)
    env.fake.user_comment(a["id"], "and Y?")
    wait_answer(env, a, "Answer three.", ROUTER)
    assert answered(env, a, "Answer two.")
    lines = agent_inputs(rlog)
    assert lines.count("/clear") == 1 and lines[0] == "/clear"  # one session
    assert prompts(rlog)[1].startswith(messages.QA_FOLLOWUP.split("{")[0])
    assert prompts(rlog)[2].startswith(messages.QA_FOLLOWUP.split("{")[0])
    wait_routes(env)
    assert registered(env) == {ROUTER} and not events(env, "qa_member_registered")
    assert len(prompts(rlog)) == 3


# ---------------------------------------------------------------------- F§8 3, 4: a cold router

def test_new_card_goes_to_the_cold_router_in_a_new_session(pool):
    """F test 3: a new card while ``claude-qa`` is cold: no agent opens; ``claude-qa`` clears its session and answers
    the new card; the old card's prefix becomes cold."""
    env = pool
    a = card(env, "first")
    rlog = start_router(env, [answer("Answer one."), answer("Answer two.")], cache_seconds=0)
    wait_answer(env, a, "Answer one.", ROUTER)
    assert cold(env, ROUTER)
    b = card(env, "second")
    wait_answer(env, b, "Answer two.", ROUTER)
    assert holder(env, b) == ROUTER and holder(env, a) is None
    assert first_prompt_after_clear(rlog, 1)
    assert registered(env) == {ROUTER} and not events(env, "qa_member_registered")
    assert wait_for(lambda: title(env, a) == COLD + "first", timeout=15)


def test_comment_on_a_cold_agents_card_is_its_follow_up(pool):
    """F test 4: a comment on a card whose agent is cold: that agent answers it in its own session."""
    env = pool
    a = card(env, "first")
    rlog = start_router(env, [answer("Answer one."), answer("Answer two.")], cache_seconds=0)
    wait_answer(env, a, "Answer one.", ROUTER)
    assert cold(env, ROUTER)
    env.fake.user_comment(a["id"], "and X?")
    wait_answer(env, a, "Answer two.", ROUTER)
    assert agent_inputs(rlog).count("/clear") == 1
    assert prompts(rlog)[1] == messages.QA_FOLLOWUP.format(counts="1 comment", turn=prompts(rlog)[1].split("`")[1])
    assert registered(env) == {ROUTER} and not events(env, "qa_member_registered")


# ---------------------------------------------------------------------- F§8 6: wait-key order

def test_older_input_first_whatever_the_list_order(pool):
    """F test 6: two cards needing an agent at once, one agent available (the router, cold at its start): the card
    with the older input (created first, at the bottom of the list) goes to it, the other to a new member."""
    env = pool
    older = card(env, "older", pos=5000)
    newer = card(env, "newer", pos=1)  # on top of the list
    scenario(env, QA2, [answer("Member answer.")])
    start_router(env, [answer("Router answer.")])
    wait_answer(env, older, "Router answer.", ROUTER)
    wait_answer(env, newer, "Member answer.", QA2)
    assert holder(env, older) == ROUTER and holder(env, newer) == QA2
    assert [e["card"] for e in events(env, "qa_routed", to=ROUTER)] == [older["id"]]
    assert [e["card"] for e in events(env, "qa_member_registered")] == [newer["id"]]


# ---------------------------------------------------------------------- F§8 9: closing when a card ends

def test_ended_cards_close_members_but_not_the_router(pool):
    """F test 9: an idle cold ``claude-qa2`` stays open while no card needs an agent; moving the router's card to
    ``Done`` leaves ``claude-qa`` running; moving the card of ``claude-qa2`` to ``Done`` closes it."""
    env = pool
    a = card(env, "first")
    start_router(env, [answer("Answer one.")])
    wait_answer(env, a, "Answer one.", ROUTER)
    scenario(env, QA2, [answer("Answer two.")], cache_seconds=0)
    b = card(env, "second")
    wait_answer(env, b, "Answer two.", QA2)
    assert cold(env, QA2)
    wait_routes(env, 3)
    assert QA2 in registered(env) and not env.project.reload_config().seats[QA2].removing
    env.fake.user_move(a["id"], env.done)
    assert wait_for(lambda: holder(env, a) is None and state(env, ROUTER) == "idle", timeout=20)
    wait_routes(env, 2)
    assert ROUTER in registered(env) and seats.liveness(env.project, ROUTER) == "alive"
    assert QA2 in registered(env)
    assert wait_for(lambda: title(env, a) == "first", timeout=15)  # the prefix leaves with the card
    env.fake.user_move(b["id"], env.done)
    assert wait_for(lambda: removed(env, QA2), timeout=40)
    assert events(env, "qa_member_closing", member=QA2)
    assert wait_for(lambda: seat_session(env.project, QA2) not in env.tmux.sessions(), timeout=20)
    assert ROUTER in registered(env) and seats.liveness(env.project, ROUTER) == "alive"


# ---------------------------------------------------------------------- F§8 11: usage limit

def test_no_member_opens_during_a_usage_limit(pool):
    """F test 11: while an agent of the pool is stopped by a usage limit, no agent opens; the new card waits."""
    env = pool
    a = card(env, "first")
    start_router(env, [answer("Answer one."), LIMIT])
    wait_answer(env, a, "Answer one.", ROUTER)
    scenario(env, QA2, [answer("never")])
    env.fake.user_comment(a["id"], "and X?")
    assert wait_for(lambda: state(env, ROUTER) == "limited", timeout=20)
    b = card(env, "second")
    wait_routes(env, 3)
    assert state(env, ROUTER) == "limited"
    assert registered(env) == {ROUTER} and not events(env, "qa_member_registered")
    assert holder(env, b) is None and not events(env, "qa_offer", card=b["id"])


# ---------------------------------------------------------------------- F§8 20, 25: reservations and reuse

def test_follow_up_reserves_its_cold_agent(pool):
    """F test 20: a follow-up on a card whose cold agent has not polled yet, while another card needs an agent: the
    follow-up goes to that card's agent, the other card to another agent."""
    env = pool
    a = card(env, "first")
    rlog = start_router(env, [answer("Answer one."), answer("Answer two.")], cache_seconds=0)
    wait_answer(env, a, "Answer one.", ROUTER)
    assert cold(env, ROUTER)
    mlog = scenario(env, QA2, [answer("Member answer.")])
    with env.fake.lock:  # one routing round sees both
        env.fake.user_comment(a["id"], "and X?")
        b = card(env, "second")
    wait_answer(env, a, "Answer two.", ROUTER)
    wait_answer(env, b, "Member answer.", QA2)
    assert agent_inputs(rlog).count("/clear") == 1  # the follow-up in the router's own session
    assert prompts(rlog)[1].startswith(messages.QA_FOLLOWUP.split("{")[0])
    assert len(prompts(mlog)) == 1 and holder(env, a) == ROUTER and holder(env, b) == QA2
    assert not events(env, "qa_offer", seat=ROUTER, card=b["id"])


def test_member_whose_card_ended_takes_the_waiting_card_in_the_same_round(pool):
    """F test 25: the card of ``claude-qa2`` moved to ``Done`` while another card needs an agent: ``claude-qa2``
    takes that card instead of being closed (and no other member opens)."""
    env = pool
    a = card(env, "first")
    start_router(env, [answer("Answer one.")])
    wait_answer(env, a, "Answer one.", ROUTER)
    mlog = scenario(env, QA2, [answer("Answer two."), answer("Answer three.")])
    b = card(env, "second")
    wait_answer(env, b, "Answer two.", QA2)
    assert qapool.warm(env.project, QA2)
    with env.fake.lock:
        env.fake.user_move(b["id"], env.done)
        c = card(env, "third")
    wait_answer(env, c, "Answer three.", QA2)
    assert holder(env, c) == QA2 and first_prompt_after_clear(mlog, 1)
    wait_routes(env, 2)
    assert registered(env) == {ROUTER, QA2}
    assert not events(env, "qa_member_closing", member=QA2) and not env.project.reload_config().seats[QA2].removing


# ---------------------------------------------------------------------- F§8 5: available before new, closing after a search

def test_card_of_a_closed_agent_goes_to_the_lowest_cold_member_and_the_others_close(pool):
    """F test 5: a comment on a card whose agent was closed, with ``claude-qa2`` and ``claude-qa3`` cold and idle:
    ``claude-qa2`` takes the card in a new session over the whole thread, ``claude-qa3`` is closed."""
    env = pool
    r = card(env, "router card")
    start_router(env, [answer("Router answer.")])
    wait_answer(env, r, "Router answer.", ROUTER)
    assert qapool.warm(env.project, ROUTER)
    out = env.tmp / "qa2-task.json"
    logs = {QA2: scenario(env, QA2, [answer("Answer B."), {
        "shell": f"kankan task --json > {out} && kankan answer --turn {{turn}} --file {{file}}",
        "text": "Answer X again."}], cache_seconds=0)}
    logs[QA3] = scenario(env, QA3, [answer("Answer C.")], cache_seconds=0)
    logs[QA4] = scenario(env, QA4, [answer("Answer X.")], cache_seconds=0)
    with env.fake.lock:  # one round: the warm router is not available, three members open
        b, c, x = card(env, "b"), card(env, "c"), card(env, "x")
    for item, text, seat in ((b, "Answer B.", QA2), (c, "Answer C.", QA3), (x, "Answer X.", QA4)):
        wait_answer(env, item, text, seat)
        assert holder(env, item) == seat
    assert cold(env, QA2) and cold(env, QA3)
    env.fake.user_move(x["id"], env.done)  # closes claude-qa4: the card of a closed agent
    assert wait_for(lambda: removed(env, QA4), timeout=40)
    wait_routes(env, 2)
    assert {QA2, QA3} <= registered(env)  # idle cold members stay while no card needs an agent
    with env.fake.lock:
        env.fake.user_move(x["id"], env.qa_list)
        env.fake.user_comment(x["id"], "and one more thing?")
    wait_answer(env, x, "Answer X again.", QA2)
    assert holder(env, x) == QA2 and first_prompt_after_clear(logs[QA2], 1)
    assert "Answer X." in out.read_text() and "and one more thing?" in out.read_text()  # the whole thread
    assert wait_for(lambda: removed(env, QA3), timeout=40)
    assert QA2 in registered(env) and len(prompts(logs[QA3])) == 1


# ---------------------------------------------------------------------- F§8 10: opening fails

def failing_tmux(env):
    """A ``tmux`` before the real one on the PATH that fails the start of a member's session while the returned
    flag file exists (a tmux error when the router opens an agent); everything else goes to the real tmux."""
    real = shutil.which("tmux")
    flag = env.tmp / "tmux-fails"
    wrapper = env.bindir / "tmux"
    wrapper.write_text(f"""#!/bin/sh
if [ -e {flag} ]; then
  prev=""
  for arg in "$@"; do
    case "$prev:$arg" in -s:*-claude-starting-*) echo "simulated tmux error" >&2; exit 1;; esac
    prev="$arg"
  done
fi
exec {real} "$@"
""")
    wrapper.chmod(0o755)
    return flag


def test_failed_opening_falls_back_to_the_warm_agent_expiring_first_or_waits(pool):
    """F test 10: opening an agent fails: the card goes to the idle warm agent whose cache expires soonest; with
    every agent answering, the card waits and the opening is retried until it succeeds."""
    env = pool
    flag = failing_tmux(env)
    a = card(env, "first")
    start_router(env, [answer("Answer A."), LONG])
    wait_answer(env, a, "Answer A.", ROUTER)
    mlog = scenario(env, QA2, [answer("Answer B."), answer("Answer C."), LONG], cache_seconds=1800)
    b = card(env, "second")
    wait_answer(env, b, "Answer B.", QA2)
    router_expiry, member_expiry = qapool.expiry(env.project, ROUTER)[0], qapool.expiry(env.project, QA2)[0]
    assert member_expiry < router_expiry  # both warm, the member's cache expires first
    flag.touch()
    c = card(env, "third")
    wait_answer(env, c, "Answer C.", QA2)
    assert events(env, "qa_open_failed")
    assert holder(env, c) == QA2 and first_prompt_after_clear(mlog, 1)
    assert not events(env, "qa_offer", seat=ROUTER, card=c["id"])
    assert wait_for(lambda: removed(env, QA3), timeout=40)  # the member that failed to open is closed
    # every agent answering: the card waits and the opening is retried
    env.fake.user_comment(a["id"], "a long one for the router")
    env.fake.user_comment(c["id"], "a long one for the member")
    assert wait_for(lambda: state(env, ROUTER) == "working" and state(env, QA2) == "working", timeout=20)
    failed = len(events(env, "qa_open_failed"))
    scenario(env, QA3, [answer("Answer D.")])
    d = card(env, "fourth")
    assert wait_for(lambda: len(events(env, "qa_open_failed")) >= failed + 2, timeout=40)  # retried
    assert holder(env, d) is None and not answered(env, d, "Answer D.")
    assert not [e for e in events(env, "qa_offer", card=d["id"]) if e["seat"] in (ROUTER, QA2)]
    flag.unlink()
    assert wait_for(lambda: answered(env, d, "Answer D."), timeout=60)
    assert holder(env, d) not in (None, ROUTER, QA2)


# ---------------------------------------------------------------------- F§8 19: a comment waiting through a restart

def test_comment_waiting_while_no_agent_can_take_it_survives_a_router_restart(pool):
    """F test 19: a comment on a card while every agent is answering and no agent can open, for two routing
    rounds, then a restart of ``claude-qa``'s launcher: the comment is answered once an agent is free, without a
    second comment."""
    env = pool
    flag = failing_tmux(env)
    x = card(env, "first")
    start_router(env, [answer("Answer X."), LONG], cache_seconds=0)
    wait_answer(env, x, "Answer X.", ROUTER)
    a = card(env, "second")  # the cold router leaves the first card for it and answers at length
    assert wait_for(lambda: holder(env, a) == ROUTER and state(env, ROUTER) == "working", timeout=30)
    flag.touch()  # no agent can open
    question = env.fake.user_comment(x["id"], "and X?")
    failed = len(events(env, "qa_open_failed"))
    wait_routes(env, 2)
    assert wait_for(lambda: len(events(env, "qa_open_failed")) > failed, timeout=20)
    wait_routes(env, 2)
    assert holder(env, x) is None and not answered(env, x, "Answer X again.")
    assert [i["id"] for i in qapool.load_record(env.project.paths)[x["id"]]["inputs"]] == [question["id"]]
    rlog = scenario(env, ROUTER, [answer("Answer A."), answer("Answer X again.")], cache_seconds=0, tag="-2")
    os.kill(proc_info(env.project, ROUTER)["launcher"]["pid"], signal.SIGKILL)
    wait_answer(env, a, "Answer A.", ROUTER, timeout=90)
    wait_answer(env, x, "Answer X again.", ROUTER, timeout=60)
    assert holder(env, x) == ROUTER
    assert env.fake.card_comments(x["id"]).count("and X?") == 1
    assert len(answered(env, x, "Answer X again.")) == 1 and len(answered(env, a, "Answer A.")) == 1
    # qapool T§9: the restarted router ended its own conversation; the cut-short question is asked again in a new one
    assert prompts(rlog)[0].startswith(messages.QA_PROMPT.split("`")[0])
    assert first_prompt_after_clear(rlog, 1)


# ---------------------------------------------------------------------- F§8 22: a gone member

def test_closed_pane_makes_a_member_gone_but_a_launcher_restart_does_not(pool):
    """F test 22: while its supervisor only restarts its launcher, ``claude-qa2`` keeps its card; once its pane is
    closed it is closed at the next routing round and a new comment on its card is answered by another agent."""
    env = pool
    a = card(env, "first")
    start_router(env, [answer("Answer A."), {**answer("Answer A2."), "cache_seconds": 0},
                       answer("Answer B by the router.")])
    wait_answer(env, a, "Answer A.", ROUTER)
    scenario(env, QA2, [answer("Answer B.")])
    b = card(env, "second")
    wait_answer(env, b, "Answer B.", QA2)
    first = proc_info(env.project, QA2)["launcher"]["pid"]
    os.kill(first, signal.SIGKILL)
    assert wait_for(lambda: (proc_info(env.project, QA2).get("launcher") or {}).get("pid") not in (None, first)
                    and state(env, QA2) == "qa_waiting", timeout=40)
    wait_routes(env, 2)
    assert holder(env, b) == QA2 and QA2 in registered(env) and not events(env, "qa_member_gone")
    assert len(answered(env, b, "Answer B.")) == 1
    env.fake.user_comment(a["id"], "and X?")  # the router answers and goes cold: available for the next card
    wait_answer(env, a, "Answer A2.", ROUTER)
    assert cold(env, ROUTER)
    env.tmux.kill_session(seat_session(env.project, QA2))  # the pane closed
    assert wait_for(lambda: removed(env, QA2), timeout=40)
    assert events(env, "qa_member_gone", seat=QA2)
    env.fake.user_comment(b["id"], "and Y?")
    wait_answer(env, b, "Answer B by the router.", ROUTER)
    assert holder(env, b) == ROUTER


# ---------------------------------------------------------------------- crash point after_member_registered

def test_crash_after_member_registered_removes_the_member_as_gone(pool, monkeypatch):
    """The router crashes between registering a member and starting it: the member is never started. A later round
    of the same router keeps it until its startup deadline passes and then removes it as gone; a restarted router,
    which no longer knows the opening, removes such a member at once and answers the card itself."""
    env = pool
    cfg = load_config(env.project.paths)
    cfg.seats[ROUTER] = SeatConfig(type="claude", role="qa")
    save_config(env.project.paths, cfg)
    env.project.reload_config()
    c = card(env, "first")
    router = qapool.Router(env.project, ROUTER, lambda event, **fields: env.project.log(event, **fields))
    monkeypatch.setenv("KANKAN_FAULT", "after_member_registered")
    with pytest.raises(faults.SimulatedCrash):
        router.round()  # the router's own agent does not run here: the card needs a new member
    monkeypatch.delenv("KANKAN_FAULT")
    assert QA2 in registered(env) and qapool.outstanding(env.project) == {}  # registered, no offer, not started
    hold = env.project.cfg.label_hold
    env.fake.user_label(c["id"], hold)  # no card needs an agent meanwhile: the rounds only look at the member
    started = []
    monkeypatch.setattr(agents_cmd, "start_member", lambda project, seat, tmux=None: started.append(seat))
    router.round()
    assert not env.project.reload_config().seats[QA2].removing  # within its startup deadline
    monkeypatch.setattr(qapool, "startup_deadline_seconds", lambda project, seat: 0)
    router.round()
    assert events(env, "qa_open_failed", member=QA2)
    assert wait_for(lambda: removed(env, QA2), timeout=30)
    assert started == []
    # the same crash, then a restarted router: the member registered and never started is gone at once
    cfg = load_config(env.project.paths)
    cfg.seats[QA2] = SeatConfig(type="claude", role="qa")
    save_config(env.project.paths, cfg)
    env.fake.user_label(c["id"], hold, add=False)
    scenario(env, QA2, [answer("never")])
    start_router(env, [answer("Router answer.")])
    wait_answer(env, c, "Router answer.", ROUTER)
    assert wait_for(lambda: removed(env, QA2), timeout=40)  # by the router's start (qapool T§9)
    assert not events(env, "qa_member_started", seat=QA2) and not (env.tmp / f"agent-{QA2}.log").exists()
