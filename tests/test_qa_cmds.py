"""Agent commands in Q&A tasks (questions F§4, T§5–T§7): refusals, `fail`, `note`, `yield`, `task --json`, and the
`comments`/`handoff` fields of implementation tasks."""

import pytest

from kankan import agentcmds, integrate, messages, qa, qa_answer, seats, workflow
from kankan.adapters import ModelEffort
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref
from kankan.seatstate import load_state
from tests.flow import agent_ctx
from tests.helpers import add_qa_list

PAIR = ModelEffort("sonnet", "high")


class Env:
    pass


@pytest.fixture
def conv(kproject, fake_trello, monkeypatch):
    """A claimed Q&A card with one delivered question."""
    project, fake = kproject, fake_trello
    env = Env()
    env.qa_list = add_qa_list(project, fake)
    env.lease = seats.acquire_qa(project, "claude", None)
    env.card = fake.add_card(env.qa_list, "question", desc="how does it work?")
    env.question = fake.user_comment(env.card["id"], "and what about X?")["id"]
    task = qa.claim(project, env.lease, project.trello.card(env.card["id"]), PAIR)
    env.token = qa.start_turn(project, env.lease, task, qa.Inputs([env.question], []))
    env.project, env.fake, env.monkeypatch = project, fake, monkeypatch
    env.ctx = agent_ctx(project, env.lease, monkeypatch)
    env.task = env.ctx.task
    return env


def fresh(env):
    return agent_ctx(env.project, env.lease, env.monkeypatch)


def ref_meta(env):
    value = env.project.refs.read(card_ref(env.card["id"]))
    return value.meta if value else None


def status_of(call):
    with pytest.raises(KankanError) as err:
        call()
    return err.value.status


def comments_with(env, needle):
    return [text for text in env.fake.card_comments(env.card["id"]) if needle in text]


def delete_ref(env):
    value = env.project.refs.read(card_ref(env.card["id"]))
    assert env.project.refs.delete(card_ref(env.card["id"]), value.oid)


# ---------------------------------------------------------------------- refusals

QA_REFUSED = {
    "done": lambda ctx: agentcmds.done(ctx),
    "ask": lambda ctx: agentcmds.ask(ctx, "which one?"),
    "sync": lambda ctx: integrate.sync(ctx),
    "verify": lambda ctx: integrate.verify(ctx, "HEAD"),
    "integrate": lambda ctx: integrate.integrate(ctx, "HEAD"),
    "block": lambda ctx: agentcmds.block(ctx, "stuck"),
}


@pytest.mark.parametrize("command", sorted(QA_REFUSED))
def test_work_commands_refuse_a_qa_task(conv, command):
    with pytest.raises(KankanError) as err:
        QA_REFUSED[command](conv.ctx)
    assert err.value.status == Status.USAGE_ERROR
    assert f"`kankan {command}`" in err.value.message
    assert ref_meta(conv)["phase"] == "qa" and not comments_with(conv, "which one?")


@pytest.mark.parametrize("command", sorted(QA_REFUSED))
def test_work_commands_refuse_an_ended_qa_task_with_usage_error(conv, command):
    """Error precedence: the wrong command for the task kind comes before `not_owner`."""
    delete_ref(conv)
    assert status_of(lambda: QA_REFUSED[command](fresh(conv))) == Status.USAGE_ERROR


@pytest.mark.parametrize("command", ["answer", "handoff"])
def test_answer_and_handoff_refuse_an_implementation_task(kproject, fake_trello, monkeypatch, command):
    fake_trello.add_card(kproject.cfg.backlog, "feature")
    lease = seats.acquire(kproject, "claude")
    workflow.tick(kproject, lease)
    ctx = agent_ctx(kproject, lease, monkeypatch)
    with pytest.raises(KankanError) as err:
        getattr(qa_answer, command)(ctx, "text", "0123456789abcdef")
    assert err.value.status == Status.USAGE_ERROR
    assert f"`kankan {command}`" in err.value.message


Q_AND_A = {
    "task": lambda ctx, env: agentcmds.task(ctx),
    "note": lambda ctx, env: agentcmds.note(ctx, "looking"),
    "fail": lambda ctx, env: agentcmds.fail(ctx, "cannot read it"),
    "answer": lambda ctx, env: qa_answer.answer(ctx, "late answer", env.token),
    "handoff": lambda ctx, env: qa_answer.handoff(ctx, "late summary", env.token),
    "yield": lambda ctx, env: agentcmds.yield_(ctx),
}


@pytest.mark.parametrize("command", sorted(Q_AND_A))
def test_every_qa_command_is_not_owner_after_the_ref_is_deleted(conv, command):
    delete_ref(conv)
    assert status_of(lambda: Q_AND_A[command](fresh(conv), conv)) == Status.NOT_OWNER
    assert not comments_with(conv, "late") and not comments_with(conv, "cannot read it")


@pytest.mark.parametrize("command", sorted(set(Q_AND_A) - {"yield"}))
def test_every_qa_command_but_yield_is_not_owner_after_the_end(conv, command):
    assert qa.end(conv.project, conv.lease, conv.task)
    assert status_of(lambda: Q_AND_A[command](fresh(conv), conv)) == Status.NOT_OWNER


def test_yield_after_the_end_succeeds_and_signals(conv):
    assert qa.end(conv.project, conv.lease, conv.task)
    result = agentcmds.yield_(fresh(conv))
    assert result["card"] == conv.card["id"]
    signal = load_state(conv.project.paths, conv.lease.seat).signal
    assert signal["kind"] == "yield" and signal["task_id"] == conv.task["id"]


def test_qa_command_of_another_conversation_is_not_owner(conv):
    """The card was re-claimed (a new conversation) while this seat's task names the old one."""
    value = conv.project.refs.read(card_ref(conv.card["id"]))
    meta = dict(value.meta, conversation="feedfacefeedface")
    assert conv.project.refs.update(card_ref(conv.card["id"]), conv.project.refs.write_blob(meta), value.oid)
    for command in ("task", "note", "fail", "yield"):
        assert status_of(lambda: Q_AND_A[command](fresh(conv), conv)) == Status.NOT_OWNER, command


def test_task_of_a_card_moved_out_of_qa_is_not_owner(conv):
    conv.fake.user_move(conv.card["id"], conv.project.cfg.done)
    assert status_of(lambda: agentcmds.task(conv.ctx)) == Status.NOT_OWNER


# ---------------------------------------------------------------------- fail, note, yield

def test_fail_sets_error_mentions_and_keeps_cursor_and_covered(conv):
    before = load_state(conv.project.paths, conv.lease.seat)
    covered = ref_meta(conv).get("covered")
    agentcmds.fail(conv.ctx, "the attachment cannot be downloaded")
    card = conv.fake.state["cards"][conv.card["id"]]
    assert conv.project.cfg.label_error in card["idLabels"]
    posted = comments_with(conv, "the attachment cannot be downloaded")
    assert len(posted) == 1 and "@" in posted[0]
    state = load_state(conv.project.paths, conv.lease.seat)
    assert state.comment_cursor == before.comment_cursor
    assert state.label_causes[conv.card["id"]][conv.project.cfg.label_error]["cause"] == "fail"
    assert state.signal["kind"] == "fail" and state.signal["payload"]["turn"] == conv.token
    assert ref_meta(conv).get("covered") == covered and ref_meta(conv)["phase"] == "qa"


def test_fail_op_id_follows_the_turn(conv):
    agentcmds.fail(conv.ctx, "same reason")
    agentcmds.fail(fresh(conv), "same reason")
    assert len(comments_with(conv, "same reason")) == 1
    qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs())
    agentcmds.fail(fresh(conv), "same reason")
    assert len(comments_with(conv, "same reason")) == 2


def test_fail_with_a_stale_turn_is_needs_action(conv):
    qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs())
    assert status_of(lambda: agentcmds.fail(fresh(conv), "late", turn=conv.token)) == Status.NEEDS_ACTION
    assert not comments_with(conv, "late")


def test_note_works_in_a_qa_task(conv):
    agentcmds.note(conv.ctx, "reading the code")
    assert len(comments_with(conv, "reading the code")) == 1


def test_yield_ends_the_conversation_and_the_card_stays(conv):
    result = agentcmds.yield_(conv.ctx)
    assert result["card"] == conv.card["id"]
    assert ref_meta(conv) is None
    assert conv.fake.card_list(conv.card["id"]) == conv.qa_list
    state = load_state(conv.project.paths, conv.lease.seat)
    assert state.task["ended"] and state.signal["kind"] == "yield"
    assert not qa.conversation_dir(conv.project.paths, conv.task["conversation"]).exists()


# ---------------------------------------------------------------------- task --json

def test_qa_task_shape_and_new_flags_from_turn_json(conv):
    delivered = conv.fake.upload(conv.card["id"], "shot.png", b"png", "image/png")
    later = conv.fake.upload(conv.card["id"], "later.txt", b"txt", "text/plain")
    qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs([conv.question], [delivered["id"]]))
    after = conv.fake.user_comment(conv.card["id"], "one more thing")["id"]
    first = agentcmds.task(fresh(conv))
    assert set(first) == {"task_id", "kind", "card", "turn", "attachments", "comments", "worktree", "integration",
                          "instructions"}
    assert first["instructions"] == messages.INSTRUCTIONS["qa"]
    turn = qa.read_turn(conv.project.paths, conv.task["conversation"])
    assert first["kind"] == "qa" and first["turn"] == turn["token"] and first["task_id"] == conv.task["id"]
    assert first["card"] == {"title": "question", "desc": "how does it work?", "url": conv.card["url"],
                             "shortLink": conv.card["shortLink"]}
    assert first["worktree"] == str(conv.project.paths.seat_worktree(conv.lease.seat))
    assert first["integration"] == conv.task["base"]
    assert {a["id"]: a["new"] for a in first["attachments"]} == {delivered["id"]: True, later["id"]: False}
    assert {c["id"]: c["new"] for c in first["comments"]} == {conv.question: True, after: False}
    question = next(c for c in first["comments"] if c["id"] == conv.question)
    assert set(question) == {"id", "author", "date", "text", "role", "new"}
    assert question["text"] == "and what about X?" and question["author"] and question["date"]
    assert agentcmds.task(fresh(conv)) == first


def test_qa_task_comment_roles(conv):
    project, card_id = conv.project, conv.card["id"]
    answer = project.trello.add_comment(card_id, "an earlier answer",
                                        f"qa-{conv.task['conversation']}-{conv.token}-1").id
    summary = project.trello.add_comment(card_id, "an earlier summary", "qh-0123456789abcdef-0123456789abcdef-1").id
    notice = project.trello.add_comment(card_id, "kankan: forwarded to the agent", "fwd-1").id
    outsider = conv.fake.add_member("outsider")
    hidden = conv.fake.user_comment(card_id, "spam", member_id=outsider["id"])["id"]
    roles = {c["id"]: c["role"] for c in agentcmds.task(conv.ctx)["comments"]}
    assert roles == {conv.question: "user", answer: "agent", summary: "agent", notice: "kankan"}
    assert hidden not in roles


# ---------------------------------------------------------------------- implementation tasks

def _implementer(project, fake, monkeypatch, card_id):
    lease = seats.acquire(project, "codex")
    project.reload_config()
    task = workflow.tick(project, lease)
    assert task["card"] == card_id, task
    return agent_ctx(project, lease, monkeypatch)


def test_implementation_task_of_a_handed_off_card_has_summary_and_thread(conv):
    qa_answer.handoff(conv.ctx, "Implement X as discussed.\n\nKeep Y.", conv.token)
    ctx = _implementer(conv.project, conv.fake, conv.monkeypatch, conv.card["id"])
    assert ctx.project.refs.read(card_ref(conv.card["id"])).meta["handoff"]
    data = agentcmds.task(ctx)
    assert data["kind"] == "impl" and data["handoff"] == "Implement X as discussed.\n\nKeep Y."
    roles = [(c["role"], "new" in c) for c in data["comments"]]
    assert ("user", False) in roles and ("agent", False) in roles
    assert [c["id"] for c in data["comments"] if c["role"] == "user"] == [conv.question]


def test_implementation_task_handoff_from_ref_ids_wins_over_a_later_marked_comment(conv):
    qa_answer.handoff(conv.ctx, "The real summary.", conv.token)
    conv.project.trello.add_comment(conv.card["id"], "a later summary", "qh-0123456789abcdef-0123456789abcdef-1")
    ctx = _implementer(conv.project, conv.fake, conv.monkeypatch, conv.card["id"])
    assert agentcmds.task(ctx)["handoff"] == "The real summary."


def test_implementation_task_handoff_from_the_latest_marked_comments(kproject, fake_trello, monkeypatch):
    card = fake_trello.add_card(kproject.cfg.backlog, "feature")
    old = "qh-0123456789abcdef-0123456789abcdef"
    new = "qh-0123456789abcdef-fedcba9876543210"
    kproject.trello.add_comment(card["id"], "Old summary.", f"{old}-1")
    kproject.trello.add_comment(card["id"], "1/2\n\nNew summary,", f"{new}-1")
    kproject.trello.add_comment(card["id"], "2/2\n\nsecond part.\n\n@someone", f"{new}-2")
    fake_trello.user_comment(card["id"], "fake `kankan:qh-0123456789abcdef-0123456789abcdef-1` by a user")
    ctx = _implementer(kproject, fake_trello, monkeypatch, card["id"])
    assert agentcmds.task(ctx)["handoff"] == "New summary,\n\nsecond part."


def test_implementation_task_without_handoff(kproject, fake_trello, monkeypatch):
    card = fake_trello.add_card(kproject.cfg.backlog, "feature")
    fake_trello.user_comment(card["id"], "please hurry")
    ctx = _implementer(kproject, fake_trello, monkeypatch, card["id"])
    data = agentcmds.task(ctx)
    assert data["handoff"] is None
    assert [(c["text"], c["role"]) for c in data["comments"] if c["role"] == "user"] == [("please hurry", "user")]
    assert [c["role"] for c in data["comments"]] == ["user", "kankan"]  # then the claim notice


# ---------------------------------------------------------------------- the card ends during a command (C2, C3)

def _end_card(env, how):
    {"moved": lambda: env.fake.user_move(env.card["id"], env.project.cfg.done),
     "archived": lambda: env.fake.user_archive(env.card["id"]),
     "hold": lambda: env.fake.user_label(env.card["id"], env.project.cfg.label_hold)}[how]()


ENDS = ["moved", "archived", "hold"]


@pytest.mark.parametrize("how", ENDS)
def test_task_whose_card_ends_during_the_download_is_not_owner_and_drops_the_files(conv, monkeypatch, how):
    from kankan import attachments
    from kankan.trello import TrelloClient
    conv.fake.upload(conv.card["id"], "a.txt", b"content")
    real = TrelloClient.download

    def download(self, *args):
        size = real(self, *args)
        _end_card(conv, how)  # the launcher's poll has not seen it yet: the ref is still there
        return size
    monkeypatch.setattr(TrelloClient, "download", download)
    assert status_of(lambda: agentcmds.task(fresh(conv))) == Status.NOT_OWNER
    assert ref_meta(conv)["phase"] == "qa"
    assert not attachments.card_dir(conv.project.paths, conv.card["shortLink"]).exists()


@pytest.mark.parametrize("how", ENDS)
@pytest.mark.parametrize("command", ["fail", "note"])
def test_fail_and_note_on_a_card_that_ended_are_not_owner(conv, how, command):
    _end_card(conv, how)
    assert status_of(lambda: Q_AND_A[command](fresh(conv), conv)) == Status.NOT_OWNER
    assert conv.project.cfg.label_error not in conv.fake.state["cards"][conv.card["id"]]["idLabels"]
    assert not comments_with(conv, "cannot read it") and not comments_with(conv, "looking")
    assert load_state(conv.project.paths, conv.lease.seat).signal is None


def test_a_delayed_fail_never_overwrites_a_newer_turns_signal(conv, monkeypatch):
    """An old ``fail`` is delayed in Trello; meanwhile the launcher starts a new turn and its agent answers. The
    new turn's signal must survive: the fail and the turn change are serialized by the conversation lock."""
    import threading
    from kankan.trello import TrelloClient
    newer = {}

    def next_turn():
        newer["turn"] = qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs())
        agentcmds.write_signal(conv.project, conv.lease.seat, conv.task["id"], "answer", {"turn": newer["turn"]})
    thread = threading.Thread(target=next_turn, daemon=True)
    real = TrelloClient.add_label

    def add_label(self, *args):
        if not thread.is_alive() and "turn" not in newer:
            thread.start()
            thread.join(1.0)  # the new turn runs now, unless the conversation lock keeps it waiting
        return real(self, *args)
    monkeypatch.setattr(TrelloClient, "add_label", add_label)
    agentcmds.fail(fresh(conv), "cannot read it", turn=conv.token)
    thread.join(30)
    signal = load_state(conv.project.paths, conv.lease.seat).signal
    assert signal["kind"] == "answer" and signal["payload"]["turn"] == newer["turn"]


def test_an_old_task_ending_late_keeps_the_files_of_a_newer_task_of_the_same_seat(conv, monkeypatch):
    """The old ``task`` downloaded and waits on the thread read; meanwhile the conversation ends, the same seat
    takes the card again and its new ``task`` returns attachment paths. The old command is not_owner and must not
    delete the newer task's files."""
    from pathlib import Path
    from kankan.trello import TrelloClient
    conv.fake.upload(conv.card["id"], "a.txt", b"content")
    real = TrelloClient.comments
    newer = {}

    def comments(self, *args, **kwargs):
        if not newer:
            newer["started"] = True
            assert qa.end(conv.project, conv.lease, conv.task)
            qa.claim(conv.project, conv.lease, conv.project.trello.card(conv.card["id"]), PAIR)
            newer["data"] = agentcmds.task(fresh(conv))
        return real(self, *args, **kwargs)
    monkeypatch.setattr(TrelloClient, "comments", comments)
    assert status_of(lambda: agentcmds.task(conv.ctx)) == Status.NOT_OWNER
    paths = [entry["path"] for entry in newer["data"]["attachments"]]
    assert paths and all(Path(path).exists() for path in paths)
