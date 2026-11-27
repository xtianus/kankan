"""Delivery from the answer record, reconciliation and turn ends in the Q&A launcher (qapool T§5 step 3, F§4.2; F
test 24)."""

from kankan import qa, qapool
from kankan.boardreader import LIVE
from kankan.refstore import card_ref
from kankan.seatstate import load_state
from tests.test_qapool_offers import READY, PAIR, flush, launcher, pool  # noqa: F401 - the fixture

ANSWER = "the answer\n\n`kankan:qa-0123456789abcdef-0a1b2c3d-1`"


def entry(env, card):
    return qapool.load_record(env.project.paths).get(card["id"]) or qapool.blank_entry()


def listed(env, card):
    return [item["id"] for item in entry(env, card)["inputs"]]


def observe(env, card):
    """A routing round's observation of the card (its comments after the record's checked mark)."""
    project = env.project
    current = qapool.load_record(project.paths).get(card["id"])
    if current is None:
        qapool.seed(project, project.board.card(card["id"], LIVE))
        current = qapool.load_record(project.paths)[card["id"]]
    comments, point = project.board.new_comments(card["id"], current.get("checked") or "", LIVE)
    fresh = project.board.card(card["id"], LIVE)
    qapool.observe(project, card["id"], comments, point, fresh.attachment_items)


def claim_and_open(env, card, the_launcher=None):
    the_launcher = the_launcher or env.launcher
    task = qa.claim(env.project, env.lease, env.project.board.card(card["id"], LIVE), PAIR)
    the_launcher.task = task
    the_launcher.state = "idle"
    the_launcher.open_conversation(READY)
    flush(the_launcher)
    return task


def test_turn_end_updates_record_and_turn_ended_at(pool):
    question = pool.fake.user_comment(pool.a["id"], "and X?")
    observe(pool, pool.a)
    assert listed(pool, pool.a) == [question["id"]]
    claim_and_open(pool, pool.a)
    assert pool.launcher.state == "working" and load_state(pool.project.paths, "claude-qa").turn_ended_at is None
    pool.launcher.on_signal({"kind": "fail", "payload": {"turn": pool.launcher.turn}})  # a failed turn counts
    current = entry(pool, pool.a)
    assert current["inputs"] == [] and current["delivered"]["description"]
    assert current["delivered"]["comment"] == pool.fake.state["comments"][question["id"]]["date"]
    assert load_state(pool.project.paths, "claude-qa").turn_ended_at is not None


def test_comment_during_completed_answer_then_restart_not_kept_listed(pool):
    early = pool.fake.user_comment(pool.a["id"], "posted while the answer was written")
    pool.fake.user_comment(pool.a["id"], ANSWER, member_id=pool.fake.me["id"])
    qapool.turn_ended(pool.project, pool.a["id"], qa.Inputs([], []))  # the description was answered earlier
    qapool._change(pool.project.paths, lambda cards: cards[pool.a["id"]]["inputs"].append(
        {"id": early["id"], "kind": "comment", "date": pool.fake.state["comments"][early["id"]]["date"]}) or True)
    assert listed(pool, pool.a) == [early["id"]]
    claim_and_open(pool, pool.a)  # a first turn: the comment is context (dated before the complete answer)
    assert pool.launcher.state == "qa_waiting" and not pool.launcher.tmux.pasted[1:]  # not prompted
    assert listed(pool, pool.a) == []
    assert not qapool.needs_agent(entry(pool, pool.a), None, holder_alive=False)


def test_deleted_comment_not_answered(pool):
    pool.fake.user_comment(pool.a["id"], ANSWER, member_id=pool.fake.me["id"])
    observe(pool, pool.a)
    late = pool.fake.user_comment(pool.a["id"], "a question deleted soon after")
    observe(pool, pool.a)
    assert listed(pool, pool.a) == [late["id"]]
    pool.fake.user_delete_comment(late["id"])
    claim_and_open(pool, pool.a)
    assert pool.launcher.state == "qa_waiting" and listed(pool, pool.a) == []
    assert not any("turn `" in text for text in pool.launcher.tmux.pasted)


def test_holder_delivers_input_found_by_router_while_answering(pool):
    claim_and_open(pool, pool.a)
    pool.launcher.turn_over()  # the answer's turn is over: waiting for follow-ups
    pool.launcher.followups = None
    follow = pool.fake.user_comment(pool.a["id"], "a follow-up")
    observe(pool, pool.a)  # the router found it while the holder was answering
    pool.launcher.cursor = "9999-01-01T00:00:00.000Z"  # the holder's own cursor is already past it
    pool.launcher.recompute = False
    pool.launcher.next_qa_poll = 0.0
    pool.launcher.poll_active()
    assert pool.launcher.followups is not None and follow["id"] in pool.launcher.followups.comments


def test_followup_during_failed_turn_delivered_once_after_restart(pool):
    claim_and_open(pool, pool.a)  # the first turn, on the description
    during = pool.fake.user_comment(pool.a["id"], "arrives during the turn")
    observe(pool, pool.a)
    pool.launcher.next_qa_poll = 0.0
    pool.launcher.poll_active()  # the holder's cursor moves past it
    pool.launcher.on_signal({"kind": "fail", "payload": {"turn": pool.launcher.turn}})
    assert listed(pool, pool.a) == [during["id"]]  # not given to the failed turn: still listed
    restarted = launcher(pool)  # the launcher restarts: the conversation is taken up
    value = pool.project.refs.read(card_ref(pool.a["id"]))
    restarted.resume(value, READY)
    flush(restarted)
    turn = qa.read_turn(pool.project.paths, value.meta["conversation"])
    assert during["id"] in turn["inputs"]["comments"]
    restarted.on_signal({"kind": "fail", "payload": {"turn": restarted.turn}})
    assert listed(pool, pool.a) == []  # delivered once


def test_failed_description_turn_not_repeated_after_restart(pool):
    """A failed turn counts as delivered: a restart of its agent with nothing new does not ask the description again
    (final review, Codex #6); a killed first turn (no turn end) is asked again."""
    claim_and_open(pool, pool.a)
    pool.launcher.on_signal({"kind": "fail", "payload": {"turn": pool.launcher.turn}})
    restarted = launcher(pool)
    restarted.resume(pool.project.refs.read(card_ref(pool.a["id"])), READY)
    flush(restarted)
    assert restarted.state == "qa_waiting" and not any("turn `" in text for text in restarted.tmux.pasted)


def test_killed_first_turn_asked_again_after_restart(pool):
    claim_and_open(pool, pool.b)  # the agent is killed during this turn: no turn end
    restarted = launcher(pool)
    restarted.resume(pool.project.refs.read(card_ref(pool.b["id"])), READY)
    flush(restarted)
    assert restarted.state == "working" and any("turn `" in text for text in restarted.tmux.pasted)
