"""Q&A posting (questions T§6): chunking, pending operations, `answer`, `handoff`, commit checks."""

import threading

import pytest

from kankan import auditlog, cards, faults, messages, qa, qa_answer
from kankan.adapters import ModelEffort
from kankan.errors import KankanError, Status
from kankan.refstore import Update, Verify, card_ref
from kankan.seatstate import load_state
from kankan.trello import COMMENT_MAX, comment_text
from tests.conftest import git
from tests.flow import agent_ctx
from tests.helpers import add_qa_list

PAIR = ModelEffort("sonnet", "high")
OPS = [f"qa-0123456789abcdef-0123456789abcdef-{i}" for i in range(1, 40)]


# ---------------------------------------------------------------------- chunking

def _fits(pieces, ops=OPS):
    return all(len(comment_text(piece, op)) <= COMMENT_MAX for piece, op in zip(pieces, ops))


def test_short_text_is_one_comment_without_prefix():
    assert qa_answer.chunks("the answer", mention="@bob", op_ids=OPS) == ["the answer\n\n@bob"]
    assert qa_answer.chunks("the answer", mention="", op_ids=OPS) == ["the answer"]


def test_long_text_gets_numbered_chunks_within_the_limit_and_one_mention():
    paragraph = ("word " * 199 + "word") + "\n\n"
    text = (paragraph * 40).rstrip()
    pieces = qa_answer.chunks(text, mention="@bob @alice", op_ids=OPS)
    count = len(pieces)
    assert count >= 3 and _fits(pieces)
    for index, piece in enumerate(pieces, 1):
        assert piece.startswith(f"{index}/{count}\n\n")
        assert ("@bob @alice" in piece) == (index == count)
    assert pieces[-1].endswith("\n\n@bob @alice")


def _bodies(pieces):
    count = len(pieces)
    result = [piece[len(f"{index}/{count}\n\n"):] for index, piece in enumerate(pieces, 1)]
    return result


def test_split_prefers_a_blank_line():
    text = "A" * 10000 + "\n\n" + "B" * 5000 + "\nC" + "C" * 5000
    pieces = qa_answer.chunks(text, mention="", op_ids=OPS)
    assert _fits(pieces) and _bodies(pieces) == ["A" * 10000, "B" * 5000 + "\nC" + "C" * 5000]


def test_split_falls_back_to_a_newline():
    text = "A" * 10000 + "\n" + "B" * 10000
    pieces = qa_answer.chunks(text, mention="", op_ids=OPS)
    assert _fits(pieces) and _bodies(pieces) == ["A" * 10000, "B" * 10000]


def test_split_falls_back_to_a_hard_cut():
    text = "A" * 40000
    pieces = qa_answer.chunks(text, mention="@bob", op_ids=OPS)
    assert len(pieces) == 3 and _fits(pieces)
    bodies = _bodies(pieces)
    bodies[-1] = bodies[-1][:-len("\n\n@bob")]
    assert "".join(bodies) == text
    # the budget reserves room for the longest prefix the op-ids allow (``99/99``): within 4 characters
    assert COMMENT_MAX - 4 <= len(comment_text(pieces[0], OPS[0])) <= COMMENT_MAX


def test_split_counts_utf16_units_as_trello_does():
    # a character outside the BMP is two UTF-16 code units in Trello's length
    text = "\U0001F600" * 20000
    pieces = qa_answer.chunks(text, mention="", op_ids=OPS)
    assert len(pieces) == 3
    assert all(len(comment_text(piece, op).encode("utf-16-le")) // 2 <= COMMENT_MAX
               for piece, op in zip(pieces, OPS))
    assert "".join(_bodies(pieces)) == text


def test_chunks_refuses_an_empty_text():
    with pytest.raises(KankanError) as err:
        qa_answer.chunks("  \n", mention="@bob", op_ids=OPS)
    assert err.value.status == Status.USAGE_ERROR


# ---------------------------------------------------------------------- fixtures

@pytest.fixture
def conv(kproject, fake_trello, monkeypatch):
    """A claimed Q&A card with one delivered question; returns a namespace."""
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    lease = qa_answer_seat(project)
    card = fake.add_card(qa_list, "question", desc="how does it work?")
    question = fake.user_comment(card["id"], "and what about X?")["id"]
    task = qa.claim(project, lease, project.trello.card(card["id"]), PAIR)
    token = qa.start_turn(project, lease, task, qa.Inputs([question], []))
    ctx = agent_ctx(project, lease, monkeypatch)

    class Env:
        pass
    env = Env()
    env.project, env.fake, env.lease, env.qa_list, env.card, env.task = project, fake, lease, qa_list, card, ctx.task
    env.token, env.ctx, env.question, env.monkeypatch = token, ctx, question, monkeypatch
    env.worktree = project.paths.seat_worktree(lease.seat)
    return env


def qa_answer_seat(project):
    from kankan import seats
    return seats.acquire_qa(project, "claude", None)


def fresh_ctx(env):
    return agent_ctx(env.project, env.lease, env.monkeypatch)


def meta(env):
    value = env.project.refs.read(card_ref(env.card["id"]))
    return value.meta if value else None


def marked(env, prefix="qa"):
    return [text for text in env.fake.card_comments(env.card["id"]) if f"`kankan:{prefix}-" in text]


def signals(env):
    return [e for e in auditlog.read(env.project.paths) if e["event"] == "signal" and e.get("seat") == env.lease.seat]


def long_text(paragraphs=40):
    return (("word " * 199 + "word") + "\n\n") * paragraphs


# ---------------------------------------------------------------------- answer

def test_answer_posts_updates_covered_and_signals(conv):
    result = qa_answer.answer(conv.ctx, "It works like this.", conv.token)
    comments = marked(conv)
    assert len(comments) == 1 and comments[0].startswith("It works like this.\n\n@")
    assert comments[0].endswith(f"`kankan:qa-{conv.task['conversation']}-{conv.token}-1`")
    assert result["comments"] and result["card"] == conv.card["id"]
    assert meta(conv)["covered"] == {"comment": conv.question, "attachments": []}
    assert meta(conv)["phase"] == "qa"
    signal = load_state(conv.project.paths, conv.lease.seat).signal
    assert signal["kind"] == "answer" and signal["task_id"] == conv.task["id"]
    assert signal["payload"]["turn"] == conv.token
    assert not (qa.conversation_dir(conv.project.paths, conv.task["conversation"]) / "pending.json").exists()
    assert conv.fake.card_list(conv.card["id"]) == conv.qa_list


def test_a_comment_posted_during_the_answer_is_not_covered(conv, monkeypatch):
    real = conv.ctx.project.trello.comment_once
    late = {}

    def comment_once(card_id, text, op_id):
        late.setdefault("id", conv.fake.user_comment(conv.card["id"], "one more thing")["id"])
        return real(card_id, text, op_id)
    monkeypatch.setattr(conv.ctx.project.trello, "comment_once", comment_once)
    qa_answer.answer(conv.ctx, "answer", conv.token)
    covered = meta(conv)["covered"]
    assert covered["comment"] == conv.question
    card = conv.project.trello.card(conv.card["id"])
    cands = qa.candidates(conv.project, card, conv.project.trello.comments(conv.card["id"]))
    assert qa.to_deliver(cands, covered, qa.Inputs()) == qa.Inputs([late["id"]], [])


def test_covered_follows_turn_coverage_with_context_and_previous(conv):
    # a first turn with context: the newest of inputs and context, and the union of the attachments
    att = conv.fake.add_attachment(conv.card["id"], "https://example.com/a")["id"]
    newer = conv.fake.user_comment(conv.card["id"], "context comment")["id"]
    token = qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs([conv.question], [att]),
                          qa.Inputs([newer], []))
    qa_answer.answer(fresh_ctx(conv), "answer", token)
    assert meta(conv)["covered"] == {"comment": newer, "attachments": [att]}
    # a later turn whose inputs are older than the previous covered comment keeps the newer one
    token = qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs([conv.question], []))
    qa_answer.answer(fresh_ctx(conv), "again", token)
    assert meta(conv)["covered"] == {"comment": newer, "attachments": [att]}


# ---------------------------------------------------------------------- pending operations

def test_crash_after_chunk_one_resumes_with_the_remaining_chunks(conv, monkeypatch):
    text = long_text()
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, text, conv.token)
    assert len(marked(conv)) == 1
    pending = qa_answer.read_pending(conv.project.paths, conv.task["conversation"])
    assert (pending["kind"], pending["turn"], pending["hash"]) == ("answer", conv.token, qa_answer.text_hash(text))
    assert len(pending["chunks"]) == 3 and len(pending["posted"]) == 1
    monkeypatch.delenv("KANKAN_FAULT")
    posts_before = sum(1 for m, p in conv.fake.requests if m == "POST" and p.endswith("/actions/comments"))
    qa_answer.answer(fresh_ctx(conv), text, conv.token)
    posts = sum(1 for m, p in conv.fake.requests if m == "POST" and p.endswith("/actions/comments")) - posts_before
    comments = marked(conv)
    assert posts == 2 and len(comments) == 3
    assert [c.split("\n", 1)[0] for c in comments] == ["1/3", "2/3", "3/3"]
    assert sum("@" in c.rsplit("\n\n", 2)[-2] for c in comments) == 1


def test_another_text_for_the_same_turn_while_pending_gives_needs_action(conv, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, long_text(), conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    with pytest.raises(KankanError) as err:
        qa_answer.answer(fresh_ctx(conv), "something else", conv.token)
    assert err.value.status == Status.NEEDS_ACTION
    # the identity includes the kind: a handoff with the same text is another operation
    with pytest.raises(KankanError) as err:
        qa_answer.handoff(fresh_ctx(conv), long_text(), conv.token)
    assert err.value.status == Status.NEEDS_ACTION
    assert len(marked(conv)) == 1 and not marked(conv, "qh")


def test_another_text_after_the_turn_was_answered_gives_needs_action(conv):
    qa_answer.answer(conv.ctx, "first", conv.token)
    again = qa_answer.answer(fresh_ctx(conv), "first", conv.token)  # the same answer: nothing new
    assert len(marked(conv)) == 1 and again["comments"]
    with pytest.raises(KankanError) as err:
        qa_answer.answer(fresh_ctx(conv), "second", conv.token)
    assert err.value.status == Status.NEEDS_ACTION
    assert len(marked(conv)) == 1 and len(signals(conv)) == 1


def test_two_concurrent_answers_post_each_chunk_once(conv):
    text = long_text()
    errors = []

    def run():
        try:
            qa_answer.answer(fresh_ctx(conv), text, conv.token)
        except Exception as err:  # pragma: no cover - reported below
            errors.append(err)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert [c.split("\n", 1)[0] for c in marked(conv)] == ["1/3", "2/3", "3/3"]
    assert len(signals(conv)) == 1


@pytest.mark.parametrize("fault", ["qa_before_covered", "qa_after_covered"])
def test_crash_around_the_covered_transaction_is_completed_by_complete_pending(conv, monkeypatch, fault):
    monkeypatch.setenv("KANKAN_FAULT", fault)
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, "answer", conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    assert signals(conv) == []
    posts_before = len(conv.fake.requests)
    task = load_state(conv.project.paths, conv.lease.seat).task
    assert qa_answer.complete_pending(conv.project, conv.lease, task) == "answer"
    assert not any(m == "POST" for m, _ in conv.fake.requests[posts_before:])
    assert len(marked(conv)) == 1 and len(signals(conv)) == 1
    assert meta(conv)["covered"] == {"comment": conv.question, "attachments": []}
    assert load_state(conv.project.paths, conv.lease.seat).signal["payload"]["turn"] == conv.token
    assert qa_answer.complete_pending(conv.project, conv.lease, task) is None
    assert len(signals(conv)) == 1


def _stale(lease):
    from dataclasses import replace
    return replace(lease, seat_oid="0" * 39 + "1")


def test_complete_pending_without_a_transaction_checks_the_seat_before_signalling(conv, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_covered")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, "answer", conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    task = load_state(conv.project.paths, conv.lease.seat).task
    with pytest.raises(KankanError) as err:
        qa_answer.complete_pending(conv.project, _stale(conv.lease), task)
    assert err.value.status == Status.NOT_OWNER
    assert signals(conv) == [] and load_state(conv.project.paths, conv.lease.seat).signal is None


def test_crash_after_the_signal_is_completed_without_a_second_answer(conv, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_signal")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, "answer", conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    assert load_state(conv.project.paths, conv.lease.seat).signal["payload"]["turn"] == conv.token
    posts_before = len(conv.fake.requests)
    task = load_state(conv.project.paths, conv.lease.seat).task
    assert qa_answer.complete_pending(conv.project, conv.lease, task) == "answer"
    assert not any(m == "POST" for m, _ in conv.fake.requests[posts_before:])
    assert len(marked(conv)) == 1
    assert meta(conv)["covered"] == {"comment": conv.question, "attachments": []}
    assert not (qa.conversation_dir(conv.project.paths, task["conversation"]) / "pending.json").exists()
    assert qa_answer.complete_pending(conv.project, conv.lease, task) is None


def test_complete_pending_posts_the_remaining_chunks(conv, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, long_text(), conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    task = load_state(conv.project.paths, conv.lease.seat).task
    assert qa_answer.complete_pending(conv.project, conv.lease, task) == "answer"
    assert [c.split("\n", 1)[0] for c in marked(conv)] == ["1/3", "2/3", "3/3"]
    assert meta(conv)["covered"]["comment"] == conv.question


def test_complete_pending_without_pending_is_none(conv):
    assert qa_answer.complete_pending(conv.project, conv.lease, conv.task) is None
    assert qa.conversations(conv.project.paths) == [conv.task["conversation"]]


def test_complete_pending_after_an_end_racing_the_lock_leaves_no_directory(conv, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, long_text(), conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    task = load_state(conv.project.paths, conv.lease.seat).task
    real = qa.conversation_lock

    def ending_first(paths, conversation, timeout):
        # the conversation ends between the pending.json check and the lock: the lock re-creates the directory
        monkeypatch.setattr(qa, "conversation_lock", real)
        qa.end(conv.project, conv.lease, task)
        return real(paths, conversation, timeout)
    monkeypatch.setattr(qa, "conversation_lock", ending_first)
    assert qa_answer.complete_pending(conv.project, conv.lease, task) is None
    assert qa.conversations(conv.project.paths) == []


def test_card_moved_to_done_between_chunks_gives_not_owner(conv, monkeypatch):
    real = conv.ctx.project.trello.comment_once
    seen = []

    def comment_once(card_id, text, op_id):
        comment = real(card_id, text, op_id)
        seen.append(op_id)
        conv.fake.user_move(conv.card["id"], conv.project.cfg.done)
        return comment
    monkeypatch.setattr(conv.ctx.project.trello, "comment_once", comment_once)
    with pytest.raises(KankanError) as err:
        qa_answer.answer(conv.ctx, long_text(), conv.token)
    assert err.value.status == Status.NOT_OWNER
    assert len(seen) == 1 and len(marked(conv)) == 1
    assert signals(conv) == []


def test_answer_after_the_end_gives_not_owner_and_leaves_no_directory(conv):
    qa.end(conv.project, conv.lease, conv.task)
    with pytest.raises(KankanError) as err:
        qa_answer.answer(fresh_ctx(conv), "late", conv.token)
    assert err.value.status == Status.NOT_OWNER
    assert qa.conversations(conv.project.paths) == [] and marked(conv) == []


def test_answer_refuses_a_non_qa_task(conv):
    def make_impl(state):
        state.task = dict(state.task, kind="impl")
    from kankan.seatstate import update_state
    update_state(conv.project.paths, conv.lease.seat, make_impl)
    with pytest.raises(KankanError) as err:
        qa_answer.answer(fresh_ctx(conv), "x", conv.token)
    assert err.value.status == Status.USAGE_ERROR


# ---------------------------------------------------------------------- checks

def test_stale_turn_gives_needs_action(conv):
    qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs([conv.question], []))
    with pytest.raises(KankanError) as err:
        qa_answer.answer(fresh_ctx(conv), "answer", conv.token)
    assert err.value.status == Status.NEEDS_ACTION and "kankan task" in err.value.message
    assert marked(conv) == []


def test_dirty_checkout_gives_needs_action_listing_the_files(conv):
    (conv.worktree / "scratch.txt").write_text("x")
    with pytest.raises(KankanError) as err:
        qa_answer.answer(conv.ctx, "answer", conv.token)
    assert err.value.status == Status.NEEDS_ACTION
    assert "scratch.txt" in err.value.message and err.value.data["files"] == ["scratch.txt"]
    assert marked(conv) == []


def test_moved_head_gives_needs_action_with_the_restore_command(conv):
    base = conv.task["base"]
    (conv.worktree / "f.txt").write_text("x")
    git(conv.worktree, "add", "f.txt")
    git(conv.worktree, "commit", "-qm", "stray")
    with pytest.raises(KankanError) as err:
        qa_answer.answer(conv.ctx, "answer", conv.token)
    assert err.value.status == Status.NEEDS_ACTION
    assert f"git checkout --detach {base}" in err.value.message
    assert marked(conv) == []


def test_a_commit_with_head_restored_posts_the_answer_and_sets_error(conv):
    base = conv.task["base"]
    (conv.worktree / "f.txt").write_text("x")
    git(conv.worktree, "add", "f.txt")
    git(conv.worktree, "commit", "-qm", "stray")
    git(conv.worktree, "checkout", "-q", "--detach", base)
    qa_answer.answer(conv.ctx, "answer", conv.token)
    assert len(marked(conv)) == 1
    assert conv.project.cfg.label_error in conv.fake.state["cards"][conv.card["id"]]["idLabels"]
    state = load_state(conv.project.paths, conv.lease.seat)
    assert state.label_causes[conv.card["id"]][conv.project.cfg.label_error]["cause"] == "qa_commit"
    expected = messages.QA_COMMITTED.format(seat=conv.lease.seat)
    assert any(text.startswith(expected) for text in conv.fake.card_comments(conv.card["id"]))
    # reported once: the next answer does not repeat the warning
    token = qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs([conv.question], []))
    qa_answer.answer(fresh_ctx(conv), "next", token)
    assert sum(text.startswith(expected) for text in conv.fake.card_comments(conv.card["id"])) == 1


def test_a_commit_before_a_handoff_is_reported_with_handoff_wording(conv):
    base = conv.task["base"]
    (conv.worktree / "f.txt").write_text("x")
    git(conv.worktree, "add", "f.txt")
    git(conv.worktree, "commit", "-qm", "stray")
    git(conv.worktree, "checkout", "-q", "--detach", base)
    qa_answer.handoff(conv.ctx, "implement X", conv.token)
    texts = conv.fake.card_comments(conv.card["id"])
    assert any(text.startswith(messages.QA_COMMITTED_HANDOFF.format(seat=conv.lease.seat)) for text in texts)
    assert not any(text.startswith(messages.QA_COMMITTED.format(seat=conv.lease.seat)) for text in texts)


def test_a_plain_checkout_is_not_reported_as_a_commit(conv):
    base = conv.task["base"]
    git(conv.worktree, "checkout", "-q", "--detach", "HEAD")
    git(conv.worktree, "checkout", "-q", "--detach", base)
    qa_answer.answer(conv.ctx, "answer", conv.token)
    assert conv.project.cfg.label_error not in conv.fake.state["cards"][conv.card["id"]]["idLabels"]


# ---------------------------------------------------------------------- handoff

def _handed_off_meta(env, ids):
    current = meta(env)
    assert current["phase"] == "parked" and current["cause"] == "handoff"
    assert current["target_list"] == env.project.cfg.backlog and current["handoff"] == ids
    assert current["conversation"] == env.task["conversation"]
    assert "owner" not in current and "covered" not in current
    return current


def test_handoff_posts_the_summary_and_moves_the_card_to_the_bottom_of_backlog(conv):
    other = conv.fake.add_card(conv.project.cfg.backlog, "already there")
    result = qa_answer.handoff(conv.ctx, "Implement X as discussed.", conv.token)
    summary = marked(conv, "qh")
    assert len(summary) == 1 and summary[0].startswith("Implement X as discussed.\n\n@")
    assert summary[0].endswith(f"`kankan:qh-{conv.task['conversation']}-{conv.token}-1`")
    backlog = sorted((c for c in conv.fake.state["cards"].values() if c["idList"] == conv.project.cfg.backlog),
                     key=lambda c: c["pos"])
    assert [c["id"] for c in backlog] == [other["id"], conv.card["id"]]
    current = _handed_off_meta(conv, result["comments"])
    assert "from_list" not in current and "pos" not in current
    assert conv.card["id"] in qa.recorded(conv.project.paths)
    assert qa.conversation_dir(conv.project.paths, conv.task["conversation"]).is_dir()
    assert not (qa.conversation_dir(conv.project.paths, conv.task["conversation"]) / "pending.json").exists()
    signal = load_state(conv.project.paths, conv.lease.seat).signal
    assert signal["kind"] == "handoff" and signal["payload"]["turn"] == conv.token


def test_handoff_transition_lock_hold_is_bounded(conv, monkeypatch):
    real_lock = qa.project_lock
    seen = {"requests": [], "tx": [], "inside": False}

    class Counting:
        def __init__(self, paths):
            self.inner = real_lock(paths)

        def __enter__(self):
            self.inner.__enter__()
            seen["inside"], seen["start"] = True, len(conv.fake.requests)

        def __exit__(self, *exc):
            seen["inside"] = False
            seen["requests"] += conv.fake.requests[seen["start"]:]
            return self.inner.__exit__(*exc)
    real_tx = conv.project.refs.transaction

    def tx(ops):
        if seen["inside"]:
            seen["tx"].append(ops)
        return real_tx(ops)
    monkeypatch.setattr(qa, "project_lock", Counting)
    monkeypatch.setattr(conv.project.refs, "transaction", tx)
    value = conv.project.refs.read(card_ref(conv.card["id"]))
    qa.handoff_transition(conv.project, conv.lease, conv.task, ["c1"])
    # one card read, one transaction (verifying the seat ref), one move with its verification read, and the
    # confirmation (a conditional update of the card ref alone)
    assert [m for m, _ in seen["requests"]] == ["GET", "PUT", "GET"]
    assert len(seen["tx"]) == 2 and any(isinstance(op, Verify) for op in seen["tx"][0])
    assert [type(op) for op in seen["tx"][1]] == [Update]
    assert value.oid != conv.project.refs.read(card_ref(conv.card["id"])).oid


@pytest.mark.parametrize("change", ["hold", "moved"])
def test_handoff_transition_refuses_a_card_on_hold_or_moved(conv, change):
    if change == "hold":
        conv.fake.user_label(conv.card["id"], conv.project.cfg.label_hold)
    else:
        conv.fake.user_move(conv.card["id"], conv.project.cfg.done)
    with pytest.raises(KankanError) as err:
        qa_answer.handoff(conv.ctx, "summary", conv.token)
    assert err.value.status == Status.NOT_OWNER
    assert meta(conv)["phase"] == "qa"
    assert conv.fake.card_list(conv.card["id"]) != conv.project.cfg.backlog


def test_crash_after_the_summary_is_completed_by_complete_pending(conv, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_handoff_summary")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.handoff(conv.ctx, "summary", conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    assert meta(conv)["phase"] == "qa" and len(marked(conv, "qh")) == 1
    task = load_state(conv.project.paths, conv.lease.seat).task
    assert qa_answer.complete_pending(conv.project, conv.lease, task) == "handoff"
    assert len(marked(conv, "qh")) == 1 and len(signals(conv)) == 1
    current = _handed_off_meta(conv, [qa_answer_comment_id(conv, "qh")])
    assert "from_list" not in current
    assert conv.fake.card_list(conv.card["id"]) == conv.project.cfg.backlog


def qa_answer_comment_id(env, prefix):
    for comment in env.project.trello.comments(env.card["id"]):
        if f"`kankan:{prefix}-" in comment.text:
            return comment.id
    return None


@pytest.mark.parametrize("phase", ["parked", "impl"])
def test_complete_pending_after_the_transition_only_removes_pending_and_signals(conv, monkeypatch, phase):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_handoff_tx")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.handoff(conv.ctx, "summary", conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    ids = [qa_answer_comment_id(conv, "qh")]
    if phase == "impl":
        value = conv.project.refs.read(card_ref(conv.card["id"]))
        data = dict(value.meta, phase="impl", owner="claude-1")
        data.pop("nonce", None)
        assert conv.project.refs.update(card_ref(conv.card["id"]), conv.project.refs.write_blob(data), value.oid)
    before = conv.project.refs.read(card_ref(conv.card["id"])).oid
    requests = len(conv.fake.requests)
    task = load_state(conv.project.paths, conv.lease.seat).task
    assert qa_answer.complete_pending(conv.project, conv.lease, task) == "handoff"
    assert conv.project.refs.read(card_ref(conv.card["id"])).oid == before
    assert conv.fake.requests[requests:] == []
    assert meta(conv)["handoff"] == ids and len(signals(conv)) == 1
    assert qa_answer.read_pending(conv.project.paths, conv.task["conversation"]) is None


def _converge(env):
    value = env.project.refs.read(card_ref(env.card["id"]))
    with qa.project_lock(env.project.paths):
        return cards.converge_ref(env.project, env.lease, card_ref(env.card["id"]), value,
                                  env.project.trello.card(env.card["id"]))


def test_crash_after_the_transaction_is_completed_by_convergence(conv, monkeypatch):
    other = conv.fake.add_card(conv.project.cfg.backlog, "already there")
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_handoff_tx")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.handoff(conv.ctx, "summary", conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    current = meta(conv)
    assert current["from_list"] == conv.qa_list and current["pos"] == "bottom"
    assert conv.fake.card_list(conv.card["id"]) == conv.qa_list
    # an unfinished handoff is never re-claimed
    snap = conv.project.trello.snapshot(conv.project.cfg.board_id)
    from kankan.workflow import card_refs
    assert qa.eligible(conv.project, snap, card_refs(conv.project)) is None
    assert _converge(conv) == f"moved:{conv.card['id']}"
    backlog = sorted((c for c in conv.fake.state["cards"].values() if c["idList"] == conv.project.cfg.backlog),
                     key=lambda c: c["pos"])
    assert [c["id"] for c in backlog] == [other["id"], conv.card["id"]]
    assert "from_list" not in meta(conv) and "pos" not in meta(conv)
    # the user drags the card back into Q&A: convergence does not move it again
    conv.fake.user_move(conv.card["id"], conv.qa_list)
    requests = len(conv.fake.requests)
    assert _converge(conv) == f"human_moved:{conv.card['id']}"
    assert not any(m == "PUT" for m, _ in conv.fake.requests[requests:])
    assert conv.fake.card_list(conv.card["id"]) == conv.qa_list


def test_hold_before_the_outstanding_handoff_move_cancels_it(conv, monkeypatch):
    from kankan.workflow import card_refs
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_handoff_tx")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.handoff(conv.ctx, "summary", conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    conv.fake.user_label(conv.card["id"], conv.project.cfg.label_hold)
    requests = len(conv.fake.requests)
    assert _converge(conv) is None
    assert not any(m == "PUT" for m, _ in conv.fake.requests[requests:])  # no move
    assert conv.fake.card_list(conv.card["id"]) == conv.qa_list
    current = meta(conv)
    assert current["phase"] == "parked" and "from_list" not in current and "pos" not in current
    assert current["target_list"] == conv.qa_list
    assert any(e["event"] == "qa_handoff_cancelled" for e in auditlog.read(conv.project.paths))
    assert _converge(conv) is None  # nothing left to do: no human_moved report
    snap = conv.project.trello.snapshot(conv.project.cfg.board_id)
    assert qa.eligible(conv.project, snap, card_refs(conv.project)) is None  # on HOLD
    conv.fake.user_label(conv.card["id"], conv.project.cfg.label_hold, add=False)
    snap = conv.project.trello.snapshot(conv.project.cfg.board_id)
    card = qa.eligible(conv.project, snap, card_refs(conv.project))
    assert card is not None and card.id == conv.card["id"]
    task = qa.claim(conv.project, conv.lease, card, PAIR)
    assert task is not None and task["conversation"] != conv.task["conversation"]
    assert meta(conv)["phase"] == "qa" and conv.fake.card_list(conv.card["id"]) == conv.qa_list


def test_crash_after_the_move_is_converged_without_moving_again(conv, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_handoff_move")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.handoff(conv.ctx, "summary", conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    assert meta(conv)["from_list"] == conv.qa_list
    assert conv.fake.card_list(conv.card["id"]) == conv.project.cfg.backlog
    requests = len(conv.fake.requests)
    assert _converge(conv) is None
    assert not any(m == "PUT" for m, _ in conv.fake.requests[requests:])
    assert "from_list" not in meta(conv)
    conv.fake.user_move(conv.card["id"], conv.qa_list)
    requests = len(conv.fake.requests)
    assert _converge(conv) == f"human_moved:{conv.card['id']}"
    assert not any(m == "PUT" for m, _ in conv.fake.requests[requests:])
    assert conv.fake.card_list(conv.card["id"]) == conv.qa_list


def test_a_pending_operation_of_an_older_turn_is_completed_first_without_its_signal(conv, monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, long_text(), conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    token = qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs([], []))
    task = load_state(conv.project.paths, conv.lease.seat).task
    assert qa_answer.complete_pending(conv.project, conv.lease, task) is None  # nothing of the current turn
    assert qa_answer.read_pending(conv.project.paths, conv.task["conversation"]) is None
    assert len(marked(conv)) == 3 and meta(conv)["covered"]["comment"] == conv.question
    assert signals(conv) == [] and load_state(conv.project.paths, conv.lease.seat).signal is None
    later = conv.fake.user_comment(conv.card["id"], "one more")["id"]
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(fresh_ctx(conv), long_text(), token)
    monkeypatch.delenv("KANKAN_FAULT")
    token3 = qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs([later], []))
    qa_answer.answer(fresh_ctx(conv), "a different text", token3)  # the older turn's pending is completed first
    assert len(marked(conv)) == 7
    assert meta(conv)["covered"]["comment"] == later
    assert [e["kind"] for e in signals(conv)] == ["answer"]
    assert load_state(conv.project.paths, conv.lease.seat).signal["payload"]["turn"] == token3
