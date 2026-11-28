"""The Q&A pool's acceptance tests 7, 8, 17 and 24 (qapool F§8) end to end: the expiry note (F§5, T§7), the title
prefixes (F§5, T§8), the ``Q&A Agents`` setting applied by every agent of the pool (F§3 "Limits and settings", T§3)
and inputs deleted or arriving during a failed turn (F§4.2, T§5 step 3), with the real supervisor → launcher → fake
agent chains in a private tmux server and the fake Trello. The harness is the one of ``test_qapool_e2e``.

The merge commit message of a handed-off card (F test 8, last part) is covered at unit level by
``test_qapool_prefix.test_plain_title_in_task_and_merge_commit``: end to end, the router removes the prefix of a
handed-off card at its next routing round, before a work seat can claim it, so an integration would only ever see a
plain title."""

import json
import os
import re
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kankan import messages, qapool
from kankan.config import atomic_write_json
from kankan.seatstate import load_state
from tests.launcher_helpers import agent_events, proc_info, start_qa_agent, wait_for
from tests.test_qa_launcher import set_questions_pair
from tests.test_qapool_e2e import pool  # noqa: F401  (the fixture)
from tests.test_qapool_e2e import (COLD, LONG, QA2, QA3, ROUTER, WARM, answer, answered, card, cold, events,
                                   failing_tmux, first_prompt_after_clear, holder, late_answer, prompts, registered,
                                   scenario, start_router, state, title, wait_answer, wait_routes)

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

ZONE = "Asia/Tokyo"  # not the machine's zone: the note's zone comes from the agents' TZ
NOTE = re.compile(r"Prompt cache (expires|may expire) at (\d\d):(\d\d) \(([^)]+)\)\.")


# ---------------------------------------------------------------------- helpers

def note_of(env, c, text):
    """``(exact, minutes of the day, zone)`` of the note ending the answer containing ``text``."""
    [comment] = answered(env, c, text)
    found = NOTE.findall(comment)
    assert len(found) == 1 and comment.index(text) < NOTE.search(comment).start()
    kind, hours, minutes, zone = found[0]
    return kind == "expires", int(hours) * 60 + int(minutes), zone


def minutes_at(stamp):
    local = datetime.fromtimestamp(stamp, ZoneInfo(ZONE))
    return local.hour * 60 + local.minute


def close_minutes(a, b, tolerance=1):
    return min((a - b) % 1440, (b - a) % 1440) <= tolerance


def within(minutes, earliest, latest):
    """``minutes`` is the local time of a moment in ``[earliest, latest]``, give or take one minute."""
    span = int((latest - earliest) // 60) + 1
    return any(close_minutes(minutes, minutes_at(earliest + 60 * step)) for step in range(span + 1))


def status_lines(log):
    """The ``expires_at`` the fake agent reported through ``kankan statusline``, in order."""
    return [e["expires_at"] for e in agent_events(log) if e["event"] == "status_line"]


def task_json(env, name):
    return env.tmp / f"{name}.json"


def task_step(out, kind, text):
    """A turn that saves ``kankan task --json`` and then runs ``kankan <kind>`` (answer or handoff)."""
    return {"shell": f"kankan task --json > {out} && kankan {kind} --turn {{turn}} --file {{file}}", "text": text}


def watch(env, c, until, timeout):
    """Poll the card's title until ``until(title)``; returns the distinct titles seen with the time each was first
    seen, in order."""
    seen = []

    def check():
        current = title(env, c)
        if not seen or seen[-1][1] != current:
            seen.append((time.time(), current))
        return until(current)
    assert wait_for(check, timeout=timeout, interval=0.1), f"title never matched; seen {[t for _, t in seen]}"
    return seen


def started(log):
    return [e for e in agent_events(log) if e["event"] == "start"]


def model_args(argv):
    return argv[argv.index("--model"):argv.index("--model") + 4]


# ---------------------------------------------------------------------- F§8 7: the expiry note

def test_exact_note_from_the_reading_recorded_before_kankan_answer(pool, monkeypatch):
    """F test 7: a Claude agent whose status line reported an expiry before it ran ``kankan answer`` ends its answer
    with ``Prompt cache expires at HH:MM (<zone>).``: the time of that reading, not the type's 60-minute estimate."""
    env = pool
    monkeypatch.setenv("TZ", ZONE)  # inherited by the tmux server, started by start_router
    a = card(env, "first")
    log = start_router(env, [answer("Answer one.")], cache_seconds=1800)
    wait_answer(env, a, "Answer one.", ROUTER)
    exact, minutes, zone = note_of(env, a, "Answer one.")
    reported = status_lines(log)[0]  # the reading of the request that ran kankan answer
    assert exact and zone == ZONE
    assert close_minutes(minutes, minutes_at(reported))
    assert not close_minutes(minutes, minutes_at(time.time() + 3600), tolerance=10)  # not the estimate
    assert messages.QA_CACHE_EXACT.format(time=f"{minutes // 60:02d}:{minutes % 60:02d}", zone=ZONE) \
        in answered(env, a, "Answer one.")[0]


def test_estimated_note_for_codex(pool, monkeypatch):
    """F test 7: a Codex agent (``estimated`` expiry, 30 minutes) gets ``Prompt cache may expire at HH:MM``, the
    planning time plus the lifetime."""
    env = pool
    monkeypatch.setenv("TZ", ZONE)
    a = card(env, "first")
    steps = [answer("Codex answer.")]
    log = scenario(env, "codex-qa", steps)
    before = time.time()
    start_qa_agent(env.tmux, env.project, agent_type="codex", scenario={"steps": steps, "log": str(log)},
                   tmp_path=env.tmp)
    wait_answer(env, a, "Codex answer.", "codex-qa")
    after = time.time()
    exact, minutes, zone = note_of(env, a, "Codex answer.")
    assert not exact and zone == ZONE
    assert within(minutes, before + 1800, after + 1800)  # planned between the two
    assert "Prompt cache may expire at" in answered(env, a, "Codex answer.")[0]
    assert not status_lines(log)  # no status line for Codex


def test_claude_reporting_no_expiry_gets_the_estimated_note(pool, monkeypatch):
    """F test 7: a Claude agent whose status line reports no cache (``prompt_cache: null``, an older Claude Code)
    gets the ``may expire`` wording with the type's 60-minute lifetime."""
    env = pool
    monkeypatch.setenv("TZ", ZONE)
    a = card(env, "first")
    before = time.time()
    log = start_router(env, [answer("Answer one.")], cache_seconds=None)
    wait_answer(env, a, "Answer one.", ROUTER)
    after = time.time()
    exact, minutes, zone = note_of(env, a, "Answer one.")
    assert status_lines(log) and all(e is None for e in status_lines(log))  # the status line ran, without a cache
    assert not exact and zone == ZONE
    assert within(minutes, before + 3600, after + 3600)


# ---------------------------------------------------------------------- F§8 8: title prefixes

def test_prefix_none_until_an_agent_takes_the_card_then_warm_then_cold_after_expiry(pool):
    """F test 8: a card waiting for an agent (every agent answering, no agent can open) has no prefix; once a
    member takes it, it gets the warm prefix, and the cold one only after the expiry time the member reported."""
    env = pool
    flag = failing_tmux(env)
    a = card(env, "first")
    start_router(env, [answer("Answer A."), LONG])
    wait_answer(env, a, "Answer A.", ROUTER)
    assert wait_for(lambda: title(env, a) == WARM + "first", timeout=15)
    env.fake.user_comment(a["id"], "a long one")
    assert wait_for(lambda: state(env, ROUTER) == "working", timeout=20)
    flag.touch()
    failed = len(events(env, "qa_open_failed"))
    b = card(env, "second")
    assert wait_for(lambda: len(events(env, "qa_open_failed")) >= failed + 2, timeout=40)
    assert title(env, b) == "second" and b["id"] not in qapool.recorded(env.project.paths)
    assert title(env, a) == WARM + "first"  # warm while its agent answers
    logs = {seat: scenario(env, seat, [late_answer("Answer B.", 3)], cache_seconds=8) for seat in (QA2, QA3)}
    flag.unlink()
    assert wait_for(lambda: answered(env, b, "Answer B."), timeout=60)
    member = holder(env, b)
    assert member in (QA2, QA3)
    seen = watch(env, b, lambda t: t == COLD + "second"
                 and qapool.recorded(env.project.paths).get(b["id"]) == COLD.strip(), timeout=40)
    titles = [t for _, t in seen]
    assert WARM + "second" in titles
    cold_at = next(when for when, t in seen if t == COLD + "second")
    assert titles.index(WARM + "second") < titles.index(COLD + "second")
    assert cold_at >= status_lines(logs[member])[-1] - 1  # cold only once the reported expiry passed
    assert holder(env, b) == member and cold(env, member)


def test_prefix_removed_on_done_and_after_a_handoff_and_task_shows_the_plain_title(pool):
    """F test 8: the prefix leaves with a card moved to ``Done`` and with a handed-off card; ``kankan task`` run
    while the card carries the warm prefix shows its title without it."""
    env = pool
    a = card(env, "first")
    start_router(env, [answer("Answer A.")])
    wait_answer(env, a, "Answer A.", ROUTER)
    out = task_json(env, "qa2-task")
    scenario(env, QA2, [answer("Answer B."), task_step(out, "handoff", "Implement the second thing.")])
    b = card(env, "second")
    wait_answer(env, b, "Answer B.", QA2)
    assert wait_for(lambda: title(env, a) == WARM + "first" and title(env, b) == WARM + "second", timeout=15)
    env.fake.user_move(a["id"], env.done)
    assert wait_for(lambda: title(env, a) == "first" and a["id"] not in qapool.recorded(env.project.paths),
                    timeout=15)
    env.fake.user_comment(b["id"], "now implement it")
    assert wait_for(lambda: answered(env, b, "Implement the second thing."), timeout=40)
    assert wait_for(lambda: env.fake.card_list(b["id"]) == env.project.cfg.backlog, timeout=30)
    assert wait_for(lambda: title(env, b) == "second" and b["id"] not in qapool.recorded(env.project.paths),
                    timeout=15)
    assert json.loads(out.read_text())["data"]["card"]["title"] == "second"
    assert "Prompt cache" not in answered(env, b, "Implement the second thing.")[0]  # a handoff has no note


def test_prefixes_removed_when_the_pool_starts(pool):
    """F test 8: the router's start removes every prefix kankan recorded; a title the user edited so that it no
    longer starts with the recorded prefix, and a prefix kankan did not record, are left as they are."""
    env = pool
    hold = env.project.cfg.label_hold
    cards = {key: card(env, name) for key, name in (("warm", f"{WARM}one"), ("cold", f"{COLD}two"),
                                                     ("user", f"{WARM}three"), ("edited", "my own title"))}
    for item in cards.values():
        env.fake.user_label(item["id"], hold)  # no card needs an agent: nothing writes a new prefix
    gone = card(env, f"{WARM}gone")
    env.fake.user_move(gone["id"], env.done)
    atomic_write_json(env.project.paths.kankan_dir / "title-prefixes.json", {
        cards["warm"]["id"]: WARM.strip(), cards["cold"]["id"]: COLD.strip(), cards["edited"]["id"]: WARM.strip(),
        gone["id"]: WARM.strip()})
    start_router(env, [answer("never")])
    assert wait_for(lambda: qapool.recorded(env.project.paths) == {}, timeout=40)
    wait_routes(env, 2)
    assert title(env, cards["warm"]) == "one" and title(env, cards["cold"]) == "two"
    assert title(env, gone) == "gone"
    assert title(env, cards["user"]) == f"{WARM}three"  # never recorded: the user's text
    assert title(env, cards["edited"]) == "my own title"


def test_title_edited_by_the_user_is_kept_until_the_next_change_of_state(pool):
    """F test 8: a title the user edited to drop the warm prefix is not touched while the card stays warm; at its
    next change of state (cold after the expiry) kankan writes the cold prefix before the user's title."""
    env = pool
    a = card(env, "first")
    start_router(env, [answer("Answer A.")], cache_seconds=20)
    wait_answer(env, a, "Answer A.", ROUTER)
    assert wait_for(lambda: title(env, a) == WARM + "first", timeout=15)
    with env.fake.lock:
        env.fake.state["cards"][a["id"]]["name"] = "my own title"
    wait_routes(env, 3)
    assert qapool.warm(env.project, ROUTER)  # no change of state meanwhile
    assert title(env, a) == "my own title"
    assert wait_for(lambda: title(env, a) == COLD + "my own title"
                    and qapool.recorded(env.project.paths) == {a["id"]: COLD.strip()}, timeout=40)
    assert cold(env, ROUTER)


# ---------------------------------------------------------------------- F§8 17: the Q&A Agents setting

def test_changed_qa_agents_setting_restarts_every_pool_agent_cold(pool):
    """F test 17: a change of the ``Q&A Agents`` setting restarts the router and a member between their turns (the
    member answering finishes first); each restarted agent is cold (the previous process's reading ignored,
    ``turn_ended_at`` cleared) and is given another card without any agent opening."""
    env = pool
    a = card(env, "first")
    rlog = start_router(env, [answer("Answer A.")])
    wait_answer(env, a, "Answer A.", ROUTER)
    mlog = scenario(env, QA2, [late_answer("Answer B.", 5)])
    b = card(env, "second")
    assert wait_for(lambda: state(env, QA2) == "working" and prompts(mlog), timeout=60)
    assert qapool.warm(env.project, ROUTER)
    rlog2 = scenario(env, ROUTER, [answer("Router after restart.")], tag="-2")
    mlog2 = scenario(env, QA2, [answer("Member after restart.")], tag="-2")
    set_questions_pair(env, "opus")
    assert wait_for(lambda: started(rlog2) and started(mlog2), timeout=60)
    assert answered(env, b, "Answer B.")
    shell_end = next(e["t"] for e in agent_events(mlog) if e["event"] == "shell")
    assert started(mlog2)[0]["t"] > shell_end  # the member's turn ended first
    for log in (rlog2, mlog2):
        assert model_args(started(log)[0]["argv"]) == ["--model", "opus", "--effort", "high"]
    assert len(started(rlog)) == 1 and len(started(mlog)) == 1
    for seat in (ROUTER, QA2):
        assert wait_for(lambda: state(env, seat) == "qa_waiting"
                        and load_state(env.project.paths, seat).task["model"] == "opus", timeout=40)
        assert cold(env, seat) and load_state(env.project.paths, seat).turn_ended_at is None
        assert (env.project.paths.seats_dir / f"{seat}.cache.json").exists()  # the old agent's reading
        assert qapool.reading(env.project, seat) is None
    assert holder(env, a) == ROUTER and holder(env, b) == QA2
    assert not prompts(rlog2) and not prompts(mlog2)  # nothing new: no prompt
    with env.fake.lock:  # one round sees both: the older to the router (lowest number), the other to the member
        c = card(env, "third")
        d = card(env, "fourth")
    wait_answer(env, c, "Router after restart.", ROUTER)
    wait_answer(env, d, "Member after restart.", QA2)
    assert holder(env, c) == ROUTER and holder(env, d) == QA2
    assert first_prompt_after_clear(rlog2, 0) and first_prompt_after_clear(mlog2, 0)
    assert [e["card"] for e in events(env, "qa_member_registered")] == [b["id"]]
    assert registered(env) == {ROUTER, QA2}


# ---------------------------------------------------------------------- F§8 24: deleted and late inputs

def test_comment_deleted_before_any_agent_took_its_card_is_not_answered(pool):
    """F test 24, first part: a comment on a card no agent holds, waiting while no agent can open, deleted before
    an agent took the card: no agent is prompted for it and nothing is posted."""
    env = pool
    flag = failing_tmux(env)
    x = card(env, "first")
    start_router(env, [answer("Answer X."), LONG], cache_seconds=0)
    wait_answer(env, x, "Answer X.", ROUTER)
    a = card(env, "second")  # the cold router leaves the first card for it and answers at length
    assert wait_for(lambda: holder(env, a) == ROUTER and state(env, ROUTER) == "working", timeout=30)
    flag.touch()
    failed = len(events(env, "qa_open_failed"))
    question = env.fake.user_comment(x["id"], "and X?")
    assert wait_for(lambda: len(events(env, "qa_open_failed")) > failed, timeout=30)
    assert [i["id"] for i in qapool.load_record(env.project.paths)[x["id"]]["inputs"]] == [question["id"]]
    env.fake.user_delete_comment(question["id"])
    logs = {seat: scenario(env, seat, [answer("never")]) for seat in (QA2, QA3)}
    flag.unlink()
    assert wait_for(lambda: not qapool.load_record(env.project.paths)[x["id"]]["inputs"], timeout=60)
    wait_routes(env, 3)
    assert not answered(env, x, "never") and len(answered(env, x, "Answer X.")) == 1
    assert all(not prompts(log) for log in logs.values())
    assert holder(env, a) == ROUTER and state(env, ROUTER) == "working"


FAILING = {"shell": "sleep 5 && kankan fail --turn {turn} 'cannot read it' && sleep 600"}  # the screen stays busy


def fail_with_a_late_comment_then_restart(env, seat, a, log):
    """On the card ``a`` held by ``seat`` (waiting after its first answer): comment A starts a turn, comment B arrives
    during it, the turn fails (A counts as delivered, B stays listed) and ``seat``'s launcher is killed before B
    could be delivered (the agent's screen still busy), its supervisor restarting it. Returns B, the restarted
    agent's log and the file its ``kankan task --json`` was saved to."""
    question = env.fake.user_comment(a["id"], "question A")
    assert wait_for(lambda: state(env, seat) == "working" and len(prompts(log)) == 2, timeout=20)
    b = env.fake.user_comment(a["id"], "question B")
    assert wait_for(lambda: events(env, "signal_received", seat=seat, kind="fail"), timeout=30)
    assert wait_for(lambda: [i["id"] for i in qapool.load_record(env.project.paths)[a["id"]]["inputs"]]
                    == [b["id"]], timeout=20)  # found by a round while the turn ran; A delivered by the failed turn
    record = qapool.load_record(env.project.paths)[a["id"]]
    assert record["delivered"]["comment"] == env.fake.state["comments"][question["id"]]["date"]
    assert len(events(env, "qa_delivered", card=a["id"])) == 1  # only A: B not delivered, the screen is busy
    out = task_json(env, f"restarted-{seat}")
    restarted = scenario(env, seat, [task_step(out, "answer", "Answer B."), answer("unexpected")], tag="-2")
    os.kill(proc_info(env.project, seat)["launcher"]["pid"], signal.SIGKILL)
    return b, restarted, out


def answered_once_after_restart(env, seat, a, b, log, out):
    assert wait_for(lambda: answered(env, a, "Answer B."), timeout=90)
    assert wait_for(lambda: state(env, seat) == "qa_waiting", timeout=20)
    wait_routes(env, 3)
    assert len(answered(env, a, "Answer B.")) == 1 and not answered(env, a, "unexpected")
    assert len(prompts(log)) == 1
    assert env.fake.card_comments(a["id"]).count("question B") == 1  # no second comment by the user
    data = json.loads(out.read_text())["data"]
    assert [c["new"] for c in data["comments"] if c["id"] == b["id"]] == [True]
    assert holder(env, a) == seat
    assert not qapool.load_record(env.project.paths)[a["id"]]["inputs"]


def test_comment_during_a_failed_turn_then_a_router_restart_is_answered_once(pool):
    """F test 24, second part, on ``claude-qa``: comment B arrives while the router answers comment A; the turn
    fails and the router's launcher restarts (ending its own conversation, qapool T§9): B is answered once, in a
    new conversation of the router, marked new."""
    env = pool
    a = card(env, "first")
    log = start_router(env, [answer("Answer one."), FAILING])
    wait_answer(env, a, "Answer one.", ROUTER)
    b, restarted, out = fail_with_a_late_comment_then_restart(env, ROUTER, a, log)
    answered_once_after_restart(env, ROUTER, a, b, restarted, out)


def test_comment_during_a_failed_turn_then_a_member_restart_is_answered_once(pool):
    """F test 24, second part, on ``claude-qa2``: the member keeps its conversation across its launcher's restart
    and answers B once (holder delivery, qapool T§5 step 3); no other agent takes the card."""
    env = pool
    r = card(env, "router card")
    start_router(env, [answer("Router answer.")])
    wait_answer(env, r, "Router answer.", ROUTER)
    log = scenario(env, QA2, [answer("Answer one."), FAILING])
    other = scenario(env, QA3, [answer("unexpected")])
    a = card(env, "first")
    wait_answer(env, a, "Answer one.", QA2)
    b, restarted, out = fail_with_a_late_comment_then_restart(env, QA2, a, log)
    answered_once_after_restart(env, QA2, a, b, restarted, out)
    assert not prompts(other) and not events(env, "qa_offer", card=a["id"], seat=ROUTER)
