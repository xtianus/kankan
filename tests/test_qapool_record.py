"""The answer record of the Q&A pool (qapool T§5 step 3): seeding, rounds, turn ends, reconciliation, the wait
key and the needs-an-agent rules; comments ordered by date, never by ID."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from kankan import faults, qa, qa_answer, qapool, seats
from kankan.adapters import ModelEffort
from kankan.boardreader import LIVE
from kankan.refstore import RefValue, card_ref
from kankan.trello import Comment
from tests.flow import agent_ctx
from tests.helpers import add_qa_list

PAIR = ModelEffort("sonnet", "high")
ANSWER = "the answer\n\n`kankan:qa-0123456789abcdef-0a1b2c3d-1`"
CUT = "1/2\n\nthe first part\n\n`kankan:qa-0123456789abcdef-0a1b2c3d-1`"


@pytest.fixture
def env(kproject, fake_trello):
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    return project, fake, qa_list


def _card(project, card_id):
    return project.board.card(card_id, LIVE)


def _entry(project, card_id):
    return qapool.load_record(project.paths).get(card_id)


def _date(fake, comment_id):
    return fake.state["comments"][comment_id]["date"]


def _ids(entry):
    return [item["id"] for item in entry["inputs"]]


def _posts(fake):
    return sum(1 for m, p in fake.requests if m == "POST" and p.endswith("/actions/comments"))


# ---------------------------------------------------------------------- comment order

def test_coverage_orders_same_second_comments_by_date(env, monkeypatch):
    project, fake, qa_list = env
    fake.shuffled_comment_ids = True
    card = fake.add_card(qa_list, "question")
    posted = []
    for _ in range(200):  # until the newest comment by date does not have the greatest ID
        posted.append(fake.user_comment(card["id"], f"comment {len(posted)}")["id"])
        if len(posted) >= 2 and max(posted, key=lambda c: (len(c), c)) != posted[-1]:
            break
    else:
        pytest.fail("no same-second inversion of comment IDs")
    newest = posted[-1]
    comments = project.board.thread(card["id"], LIVE)
    cands = qa.candidates(project, _card(project, card["id"]), comments)
    assert cands.comments == posted  # by date, not by ID
    assert qa.newest(cands.comments, cands.dates) == newest
    assert qa.coverage(None, cands) == {"comment": newest, "attachments": []}
    assert qa.uncovered(cands, {"comment": newest, "attachments": []}) == qa.Inputs()
    # a previous covered comment with a greater ID but an older date gives way to the newer one
    bigger = max(posted, key=lambda c: (len(c), c))
    dates = {c.id: c.date for c in comments}
    assert qa.coverage({"comment": bigger, "attachments": []}, qa.Inputs([newest], [], dates)) == \
        {"comment": newest, "attachments": []}
    # the real path: turn.json carries the dates, the answer covers the newest comment by date
    lease = seats.acquire_qa(project, "claude", None)
    task = qa.claim(project, lease, _card(project, card["id"]), PAIR)
    i = posted.index(bigger)
    token = qa.start_turn(project, lease, task, qa.Inputs(posted[:i + 1], [], cands.dates))
    qa_answer.answer(agent_ctx(project, lease, monkeypatch), "answer", token)
    covered = project.refs.read(card_ref(card["id"])).meta["covered"]
    assert covered["comment"] == bigger
    # the follow-up carries the previous covered comment's date, so the newer comment with a smaller ID wins
    follow = qa.uncovered(cands, covered)
    assert follow.comments == posted[i + 1:] and bigger in follow.dates
    token = qa.start_turn(project, lease, task, follow)
    qa_answer.answer(agent_ctx(project, lease, monkeypatch), "follow-up", token)
    assert project.refs.read(card_ref(card["id"])).meta["covered"]["comment"] == newest
    # a turn.json written by the previous version (IDs only) still completes, by ID order
    turn_path = qa.conversation_dir(project.paths, task["conversation"]) / "turn.json"
    token = qa.start_turn(project, lease, task, qa.Inputs(list(posted), []))
    assert "dates" not in qa.read_turn(project.paths, task["conversation"])["inputs"]
    assert turn_path.exists()
    qa_answer.answer(agent_ctx(project, lease, monkeypatch), "again", token)
    assert project.refs.read(card_ref(card["id"])).meta["covered"]["comment"] == bigger


# ---------------------------------------------------------------------- seeding

def test_seed_complete_answer(env):
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    fake.user_comment(card["id"], "a question")
    att = fake.add_attachment(card["id"], "https://example.com/a")["id"]
    answer = fake.user_comment(card["id"], ANSWER, member_id=fake.me["id"])["id"]
    entry = qapool.seed(project, _card(project, card["id"]))
    assert entry == _entry(project, card["id"])
    assert entry["answered"] == {"id": answer, "date": _date(fake, answer)}
    assert entry["delivered"] == {"description": True, "comment": _date(fake, answer), "attachments": [att]}
    assert entry["checked"] == _date(fake, answer)
    assert entry["inputs"] == []


def test_seed_cut_short_answer_never_delivered(env):
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    question = fake.user_comment(card["id"], "a question")["id"]
    cut = fake.user_comment(card["id"], CUT, member_id=fake.me["id"])["id"]
    entry = qapool.seed(project, _card(project, card["id"]))
    assert entry["answered"] is None
    assert entry["delivered"] == {"description": False, "comment": None, "attachments": []}
    assert entry["checked"] == _date(fake, cut)
    assert entry["inputs"] == [{"id": question, "kind": "comment", "date": _date(fake, question)}]
    assert qapool.needs_agent(entry, None, False)


def test_seed_inputs_after_answer(env):
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    outsider = fake.add_member("outsider")
    fake.user_comment(card["id"], "a question")
    old = fake.add_attachment(card["id"], "https://example.com/old")["id"]
    fake.user_comment(card["id"], ANSWER, member_id=fake.me["id"])
    later = fake.user_comment(card["id"], "a follow-up")["id"]
    fake.user_comment(card["id"], "from outside", member_id=outsider["id"])
    fake.user_comment(card["id"], "pasted `kankan:qa-0123456789abcdef-t-1`")
    new = fake.add_attachment(card["id"], "https://example.com/new")
    fake.add_attachment(card["id"], "https://example.com/c", member_id=outsider["id"])
    last = fake.user_comment(card["id"], "notice", member_id=fake.me["id"])["id"]
    entry = qapool.seed(project, _card(project, card["id"]))
    assert entry["inputs"] == [{"id": later, "kind": "comment", "date": _date(fake, later)},
                               {"id": new["id"], "kind": "attachment", "date": new["date"]}]
    assert entry["delivered"]["attachments"] == [old]
    assert entry["checked"] == _date(fake, last)
    # a card already in the record is not seeded again
    fake.user_comment(card["id"], "another")
    assert qapool.seed(project, _card(project, card["id"])) == entry


# ---------------------------------------------------------------------- rounds and turn ends

def test_observe_checked_and_inputs_one_write(env, monkeypatch):
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    fake.user_comment(card["id"], ANSWER, member_id=fake.me["id"])
    qapool.seed(project, _card(project, card["id"]))
    one = fake.user_comment(card["id"], "one")
    att = fake.add_attachment(card["id"], "https://example.com/a")
    two = fake.user_comment(card["id"], "two")
    comments = [Comment(c["id"], c["idMemberCreator"], "u", c["date"], c["data"]["text"]) for c in (one, two)]
    stale = Comment("0" * 24, fake.owner["id"], "u", "2020-01-01T00:00:00.000Z", "older than the answer")
    writes = []
    real = qapool.atomic_write_json
    monkeypatch.setattr(qapool, "atomic_write_json", lambda *a, **k: (writes.append(a[0]), real(*a, **k)))
    entry = qapool.observe(project, card["id"], comments + [stale], two["date"],
                           _card(project, card["id"]).attachment_items)
    assert len(writes) == 1
    assert entry["checked"] == two["date"]
    assert entry["inputs"] == [{"id": one["id"], "kind": "comment", "date": one["date"]},
                               {"id": two["id"], "kind": "comment", "date": two["date"]},
                               {"id": att["id"], "kind": "attachment", "date": att["date"]}]
    # observed again: nothing listed twice
    entry = qapool.observe(project, card["id"], comments, two["date"], _card(project, card["id"]).attachment_items)
    assert len(entry["inputs"]) == 3 and entry == _entry(project, card["id"])


def test_turn_end_removes_only_given_inputs(env):
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    qapool.seed(project, _card(project, card["id"]))
    first = fake.user_comment(card["id"], "A")["id"]
    thread = project.board.thread(card["id"], LIVE)
    qapool.observe(project, card["id"], thread, _date(fake, first), [])
    # B arrives during A's turn; the turn fails: A counts as delivered, B stays listed
    second = fake.user_comment(card["id"], "B")["id"]
    qapool.observe(project, card["id"], project.board.thread(card["id"], LIVE), _date(fake, second), [])
    entry = qapool.turn_ended(project, card["id"], qa.Inputs([first], [], {first: _date(fake, first)}))
    assert _ids(entry) == [second]
    assert entry["delivered"] == {"description": True, "comment": _date(fake, first), "attachments": []}
    assert entry["answered"] is None
    # the date of a given comment comes from its input entry when the caller has none
    entry = qapool.turn_ended(project, card["id"], qa.Inputs([second], []))
    assert entry["inputs"] == [] and entry["delivered"]["comment"] == _date(fake, second)
    # delivered never moves back
    entry = qapool.turn_ended(project, card["id"], qa.Inputs([first], [], {first: _date(fake, first)}))
    assert entry["delivered"]["comment"] == _date(fake, second)


@pytest.fixture
def conv(env, monkeypatch):
    project, fake, qa_list = env
    lease = seats.acquire_qa(project, "claude", None)
    card = fake.add_card(qa_list, "question", desc="how does it work?")
    question = fake.user_comment(card["id"], "and what about X?")["id"]
    qapool.seed(project, _card(project, card["id"]))
    task = qa.claim(project, lease, _card(project, card["id"]), PAIR)
    cands = qa.candidates(project, _card(project, card["id"]), project.board.thread(card["id"], LIVE))
    token = qa.start_turn(project, lease, task, qa.uncovered(cands, None))
    return project, fake, lease, card, task, token, question


def test_killed_turn_updates_nothing(conv, monkeypatch):
    project, fake, lease, card, task, token, question = conv
    before = _entry(project, card["id"])
    assert _ids(before) == [question] and before["delivered"]["description"] is False
    # a turn started and its agent killed before answering, or killed with its answer cut short: no change
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(agent_ctx(project, lease, monkeypatch), (("word " * 199 + "word") + "\n\n") * 40, token)
    assert _entry(project, card["id"]) == before
    # completed later (a removal or a router start): delivered once complete
    monkeypatch.delenv("KANKAN_FAULT")
    assert qa_answer.complete_pending(project, lease, task) == "answer"
    after = _entry(project, card["id"])
    assert after["inputs"] == [] and after["delivered"]["comment"] == _date(fake, question)


def test_description_only_card_answered_not_needing_agent(env, monkeypatch):
    project, fake, qa_list = env
    lease = seats.acquire_qa(project, "claude", None)
    card = fake.add_card(qa_list, "question", desc="the question is here")
    entry = qapool.seed(project, _card(project, card["id"]))
    assert entry["delivered"]["description"] is False and entry["inputs"] == []
    assert qapool.needs_agent(entry, None, False)  # a new card: its description is the question
    task = qa.claim(project, lease, _card(project, card["id"]), PAIR)
    token = qa.start_turn(project, lease, task, qa.Inputs())
    qa_answer.answer(agent_ctx(project, lease, monkeypatch), "answered", token)
    entry = _entry(project, card["id"])
    assert entry["delivered"]["description"] is True and entry["answered"] is not None
    assert not qapool.needs_agent(entry, None, False)


def test_attachments_tracked_by_id_same_date(env):
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    qapool.seed(project, _card(project, card["id"]))
    fake.add_attachment(card["id"], "https://example.com/a")
    fake.add_attachment(card["id"], "https://example.com/b")
    first, second = _card(project, card["id"]).attachment_items
    same = [first, replace(second, date=first.date)]
    qapool.turn_ended(project, card["id"], qa.Inputs([], [first.id]))
    entry = qapool.observe(project, card["id"], [], "", same)
    assert _ids(entry) == [second.id]
    assert entry["delivered"]["attachments"] == [first.id]


def test_reconcile_drops_deleted_comment_listed_before_read(env):
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    qapool.seed(project, _card(project, card["id"]))
    a = fake.user_comment(card["id"], "A")["id"]
    b = fake.user_comment(card["id"], "B")["id"]
    fake.add_attachment(card["id"], "https://example.com/x")
    att = _card(project, card["id"]).attachment_items
    qapool.observe(project, card["id"], project.board.thread(card["id"], LIVE), _date(fake, b), att)
    listed = set(_ids(_entry(project, card["id"])))
    fake.user_delete_comment(a)
    fake.remove_attachment(card["id"], att[0].id)
    thread = [c.id for c in project.board.thread(card["id"], LIVE)]
    dropped = qapool.reconcile(project, card["id"], listed, thread,
                               [x.id for x in _card(project, card["id"]).attachment_items])
    assert sorted(dropped) == sorted([a, att[0].id])
    assert _ids(_entry(project, card["id"])) == [b]


def test_reconcile_keeps_entries_listed_after_read(env):
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    qapool.seed(project, _card(project, card["id"]))
    a = fake.user_comment(card["id"], "A")["id"]
    qapool.observe(project, card["id"], project.board.thread(card["id"], LIVE), _date(fake, a), [])
    listed = set(_ids(_entry(project, card["id"])))
    thread = [c.id for c in project.board.thread(card["id"], LIVE)]  # the live reads, sent now
    c = fake.user_comment(card["id"], "C")["id"]  # listed by a round after the reads were sent
    qapool.observe(project, card["id"], project.board.thread(card["id"], LIVE), _date(fake, c), [])
    assert qapool.reconcile(project, card["id"], listed, thread, []) == []
    assert _ids(_entry(project, card["id"])) == [a, c]
    # an entry the first turn counts as context is dropped too
    assert qapool.reconcile(project, card["id"], listed, thread + [c], [], context_ids=[a]) == [a]
    assert _ids(_entry(project, card["id"])) == [c]


def test_wait_key_oldest_input_or_card_creation():
    created = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    card_id = f"{int(created.timestamp()):08x}" + "0" * 16
    entry = {"delivered": {"description": False, "comment": None, "attachments": []}, "answered": None,
             "checked": None, "inputs": []}
    assert qapool.wait_key(card_id, entry) == "2026-11-17T10:00:00.000Z"
    assert qapool.wait_key(card_id, None) == "2026-11-17T10:00:00.000Z"
    entry["delivered"]["description"] = True  # answered: the oldest input sets the key
    entry["inputs"] = [{"id": "b", "kind": "comment", "date": "2026-11-18T09:00:00.500Z"},
                       {"id": "a", "kind": "attachment", "date": "2026-11-18T08:59:59.999Z"}]
    assert qapool.wait_key(card_id, entry) == "2026-11-18T08:59:59.999Z"
    assert qapool.wait_key(card_id, entry) > qapool.wait_key(card_id, None)


def test_needs_agent_rules():
    new = {"delivered": {"description": False, "comment": None, "attachments": []}, "answered": None,
           "checked": None, "inputs": []}
    answered = {"delivered": {"description": True, "comment": "2026-11-17T10:00:00.000Z", "attachments": []},
                "answered": {"id": "x", "date": "2026-11-17T10:00:00.000Z"}, "checked": None, "inputs": []}
    with_input = dict(answered, inputs=[{"id": "c", "kind": "comment", "date": "2026-11-17T11:00:00.000Z"}])
    parked = RefValue("0" * 40, {"phase": "parked", "cause": "handoff", "card": "c", "handoff": ["h"]})
    qa_ref = RefValue("0" * 40, {"phase": "qa", "owner": "claude-qa", "card": "c"})
    assert qapool.needs_agent(new, None, False)  # a new card
    assert qapool.needs_agent(None, None, False)  # not seeded yet: never delivered
    assert not qapool.needs_agent(new, parked, False)  # handed off: not a new question
    assert qapool.needs_agent(dict(new, inputs=with_input["inputs"]), parked, False)
    assert not qapool.needs_agent(answered, None, False)
    assert qapool.needs_agent(with_input, None, False)
    assert qapool.needs_agent(with_input, qa_ref, False)  # its holder is not alive
    assert not qapool.needs_agent(with_input, qa_ref, True)  # its alive holder delivers it
    assert not qapool.needs_agent(new, qa_ref, True)


def test_answer_completion_updates_delivered_before_pending_removed(conv, monkeypatch):
    project, fake, lease, card, task, token, question = conv
    seen = {}
    real = qa_answer._drop_pending

    def drop(paths, conversation):
        seen.setdefault("entry", _entry(project, card["id"]))
        real(paths, conversation)
    monkeypatch.setattr(qa_answer, "_drop_pending", drop)
    # a crash right after the record update keeps pending.json; the retry posts nothing twice
    monkeypatch.setenv("KANKAN_FAULT", "after_delivered_recorded")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(agent_ctx(project, lease, monkeypatch), "It works like this.", token)
    assert qa_answer.read_pending(project.paths, task["conversation"]) is not None
    recorded = _entry(project, card["id"])
    posted = [c for c in fake.state["comments"].values() if "`kankan:qa-" in c["data"]["text"]]
    assert len(posted) == 1
    assert recorded["answered"] == {"id": posted[0]["id"], "date": posted[0]["date"]}
    assert recorded["delivered"] == {"description": True, "comment": _date(fake, question), "attachments": []}
    assert recorded["inputs"] == []
    monkeypatch.delenv("KANKAN_FAULT")
    posts = _posts(fake)
    assert qa_answer.complete_pending(project, lease, task) == "answer"
    assert _posts(fake) == posts
    assert seen["entry"] == recorded == _entry(project, card["id"])
    assert qa_answer.read_pending(project.paths, task["conversation"]) is None


def test_drop_removes_entries(env):
    project, fake, qa_list = env
    one, two = fake.add_card(qa_list, "one"), fake.add_card(qa_list, "two")
    qapool.seed(project, _card(project, one["id"]))
    qapool.seed(project, _card(project, two["id"]))
    qapool.drop(project, [one["id"], "missing"])
    assert set(qapool.load_record(project.paths)) == {two["id"]}


def test_reconciled_context_attachment_is_not_listed_again(env):
    """An attachment a first turn counts as context leaves ``inputs`` for good: the next round does not list it again,
    which would reserve the card's cold agent for an input it never delivers (final review, Codex #7)."""
    project, fake, qa_list = env
    card = fake.add_card(qa_list, "question")
    qapool.seed(project, _card(project, card["id"]))
    att = fake.upload(card["id"], "log.txt", b"x\n", "text/plain")["id"]
    attachments = _card(project, card["id"]).attachment_items
    qapool.observe(project, card["id"], [], None, attachments)
    assert _ids(_entry(project, card["id"])) == [att]
    assert qapool.reconcile(project, card["id"], {att}, [], [att], context_ids=[att]) == [att]
    qapool.observe(project, card["id"], [], None, attachments)
    assert _ids(_entry(project, card["id"])) == []


def test_wait_key_keeps_creation_while_the_description_is_pending():
    """A card whose description was never delivered waits since its creation, even with a later comment (final review,
    Codex #11)."""
    created = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    card_id = f"{int(created.timestamp()):08x}" + "0" * 16
    entry = {"delivered": {"description": False, "comment": None, "attachments": []}, "answered": None,
             "checked": None, "inputs": [{"id": "b", "kind": "comment", "date": "2026-11-18T09:00:00.500Z"}]}
    assert qapool.wait_key(card_id, entry) == "2026-11-17T10:00:00.000Z"
    entry["delivered"]["description"] = True
    assert qapool.wait_key(card_id, entry) == "2026-11-18T09:00:00.500Z"
