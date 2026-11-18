import pytest

from kankan import cards, faults, seats
from kankan.cards import AdminActor, CardMeta, converge, target_list_for, transition
from kankan.errors import KankanError, Status
from kankan.labels import clear_kankan_labels
from kankan.refstore import card_ref, seat_ref


@pytest.fixture
def setup(kproject, fake_trello):
    lease = seats.acquire(kproject, "claude")
    kproject.reload_config()
    card = fake_trello.add_card(kproject.cfg.backlog, "Task A")
    return kproject, fake_trello, lease, kproject.trello.card(card["id"])


def claim_meta(project, lease, card):
    return CardMeta(phase="impl", card=card.id, short=card.short, owner=lease.seat, session=lease.session,
                    implementer=lease.seat, implementer_type=lease.type, branch=f"26kankan-{card.short}",
                    target_list=target_list_for(project.cfg, "impl", lease.seat, None))


def drive(project, lease, card, step):
    """Bring the card to the phase before ``step`` and return (expect, new meta) for ``step``."""
    cfg = project.cfg
    claim = claim_meta(project, lease, card)
    if step == "claim":
        return None, claim
    res = transition(project, lease, card, None, claim, None)
    if step == "done":
        new = CardMeta.from_dict(res.ref.meta).evolve(phase="implemented", owner=None,
                                                       target_list=target_list_for(cfg, "implemented", None, lease.seat))
    elif step == "yield":
        new = CardMeta.from_dict(res.ref.meta).evolve(phase="parked", owner=None, cause="yield", target_list=cfg.backlog)
    else:
        new = CardMeta.from_dict(res.ref.meta).evolve(phase="parked", owner=None, cause="block", target_list=cfg.blocked)
    return res.ref, new


@pytest.mark.parametrize("step", ["claim", "done", "yield", "block"])
def test_crash_after_ref_tx_converges(setup, monkeypatch, step):
    project, fake, lease, card = setup
    expect, new = drive(project, lease, card, step)
    monkeypatch.setenv("KANKAN_FAULT", "after_ref_tx")
    with pytest.raises(faults.SimulatedCrash):
        transition(project, lease, card, expect, new, "comment")
    monkeypatch.delenv("KANKAN_FAULT")
    other = seats.acquire(project, "claude")
    with pytest.raises(KankanError):
        transition(project, other, card, None, claim_meta(project, other, card), None)
    events = converge(project, lease)
    assert f"moved:{card.id}" in events
    assert fake.card_list(card.id) == new.target_list


def test_owned_phase_converged_only_by_owner(setup, monkeypatch):
    project, fake, lease, card = setup
    other = seats.acquire(project, "claude")
    monkeypatch.setenv("KANKAN_FAULT", "after_ref_tx")
    with pytest.raises(faults.SimulatedCrash):
        transition(project, lease, card, None, claim_meta(project, lease, card), None)
    monkeypatch.delenv("KANKAN_FAULT")
    assert converge(project, other) == []
    assert fake.card_list(card.id) == project.cfg.backlog


def test_integrated_in_done_deletes_ref(setup):
    project, fake, lease, card = setup
    res = transition(project, lease, card, None, claim_meta(project, lease, card), None)
    integ = CardMeta.from_dict(res.ref.meta).evolve(phase="integrated", owner=None, merge="a" * 40, tested="b" * 40,
                                                     target_list=project.cfg.done)
    fake.user_label(card.id, project.cfg.label_error)
    fake.user_label(card.id, project.cfg.label_hold)
    transition(project, lease, card, res.ref, integ, None)
    assert fake.card_list(card.id) == project.cfg.done
    events = converge(project, lease)
    assert events == [f"deleted:{card.id}"]
    assert project.refs.read(card_ref(card.id)) is None
    assert fake.state["cards"][card.id]["idLabels"] == [project.cfg.label_hold]


def test_move_timeout_no_task_no_duplicate_comment(setup, virtual_time):
    project, fake, lease, card = setup
    fake.timeout_delay = 0.6
    fake.inject("PUT", "^/1/cards/", "timeout", times=20)
    res = transition(project, lease, card, None, claim_meta(project, lease, card), "claimed")
    assert res.moved is False
    assert fake.card_list(card.id) == project.cfg.backlog
    fake.faults.clear()
    assert converge(project, lease) == [f"moved:{card.id}"]
    comments = fake.card_comments(card.id)
    assert len(comments) == 1 and res.ref.oid in comments[0]


def test_transition_after_seat_replaced_not_owner(setup):
    project, fake, lease, card = setup
    res = transition(project, lease, card, None, claim_meta(project, lease, card), None)
    other_oid = project.refs.write_blob({"schema": 1, "kind": "seat", "session": "x"})
    assert project.refs.update(seat_ref(lease.seat), other_oid, lease.seat_oid)
    new = CardMeta.from_dict(res.ref.meta).evolve(phase="parked", cause="yield", owner=None,
                                                   target_list=project.cfg.backlog)
    with pytest.raises(KankanError) as exc:
        transition(project, lease, card, res.ref, new, None)
    assert exc.value.status == Status.NOT_OWNER
    assert project.refs.read(card_ref(card.id)).oid == res.ref.oid


def test_converge_never_reverts_human_move(setup):
    project, fake, lease, card = setup
    res = transition(project, lease, card, None, claim_meta(project, lease, card), None)
    parked = CardMeta.from_dict(res.ref.meta).evolve(phase="parked", owner=None, cause="block",
                                                      target_list=project.cfg.blocked)
    transition(project, lease, card, res.ref, parked, None)
    fake.user_move(card.id, project.cfg.backlog)
    assert converge(project, lease) == [f"human_moved:{card.id}"]
    assert fake.card_list(card.id) == project.cfg.backlog


def test_converge_completes_lagging_claim_not_human(setup, monkeypatch):
    project, fake, lease, card = setup
    monkeypatch.setenv("KANKAN_FAULT", "after_ref_tx")
    with pytest.raises(faults.SimulatedCrash):
        transition(project, lease, card, None, claim_meta(project, lease, card), None)
    monkeypatch.delenv("KANKAN_FAULT")
    assert converge(project, lease) == [f"moved:{card.id}"]
    assert fake.card_list(card.id) == project.cfg.seats[lease.seat].in_progress


def test_converge_error_after_repeated_failures(setup, virtual_time):
    project, fake, lease, card = setup
    fake.inject("PUT", "^/1/cards/", "500", times=1000)
    transition(project, lease, card, None, claim_meta(project, lease, card), None)
    for _ in range(3):
        converge(project, lease)
    assert project.cfg.label_error in fake.state["cards"][card.id]["idLabels"]


def test_admin_actor_transition(setup):
    project, fake, lease, card = setup
    res = transition(project, lease, card, None, claim_meta(project, lease, card), None)
    parked = CardMeta.from_dict(res.ref.meta).evolve(phase="parked", owner=None, cause="admin_release", target_list=None)
    transition(project, AdminActor("admin"), card, res.ref, parked, None)
    assert project.refs.read(card_ref(card.id)).meta["phase"] == "parked"


def test_done_card_keeps_hold(setup):
    project, fake, lease, card = setup
    for label in (project.cfg.label_error, project.cfg.label_hold, project.cfg.label_question):
        fake.user_label(card.id, label)
    clear_kankan_labels(project, project.trello.card(card.id))
    assert fake.state["cards"][card.id]["idLabels"] == [project.cfg.label_hold]


# ---------------------------------------------------------------------- Q&A (questions T§3)
from kankan import qa  # noqa: E402
from tests.helpers import add_qa_list, put_card_ref  # noqa: E402

CONV = "0123456789abcdef"


@pytest.fixture
def qa_setup(setup):
    project, fake, lease, card = setup
    qa_list = add_qa_list(project, fake)
    fake.user_move(card.id, qa_list)
    return project, fake, lease, project.trello.card(card.id), qa_list


def test_target_list_for_qa_and_handoff(qa_setup):
    project, fake, lease, card, qa_list = qa_setup
    assert cards.Phase.QA.value == "qa"
    assert target_list_for(project.cfg, "qa", "claude-qa", None) == qa_list
    assert target_list_for(project.cfg, "parked", None, None, cause="handoff") == project.cfg.backlog


def test_card_meta_carries_qa_fields():
    meta = CardMeta.from_dict({"phase": "qa", "card": "a" * 24, "short": "abcdefgh", "conversation": CONV,
                               "covered": {"comment": "c1", "attachments": ["a1"]}, "handoff": ["h1"],
                               "pos": "bottom"})
    data = meta.evolve(owner="codex-qa").to_dict()
    assert data["conversation"] == CONV and data["covered"] == {"comment": "c1", "attachments": ["a1"]}
    assert data["handoff"] == ["h1"] and data["pos"] == "bottom"


@pytest.mark.parametrize("where", ["done", "archived", "deleted", "qa"])
def test_converge_never_moves_or_reports_qa_ref(qa_setup, where):
    project, fake, lease, card, qa_list = qa_setup
    value = put_card_ref(project, card.id, card.short, phase="qa", owner="claude-qa", target_list=qa_list,
                         from_list=project.cfg.backlog, conversation=CONV)
    if where == "done":
        fake.user_move(card.id, project.cfg.done)
    elif where == "archived":
        fake.user_archive(card.id)
    elif where == "deleted":
        fake.user_delete(card.id)
    else:
        fake.user_move(card.id, project.cfg.backlog)  # its from_list: still never moved
    assert converge(project, lease) == []
    assert project.refs.read(card_ref(card.id)).oid == value.oid
    if where in ("done", "qa"):
        assert fake.card_list(card.id) == (project.cfg.done if where == "done" else project.cfg.backlog)


def _handoff_ref(project, card, qa_list):
    return put_card_ref(project, card.id, card.short, phase="parked", cause="handoff",
                        target_list=project.cfg.backlog, from_list=qa_list, pos="bottom", handoff=["h1", "h2"],
                        conversation=CONV)


def test_converge_moves_parked_handoff_to_bottom_and_confirms(qa_setup):
    project, fake, lease, card, qa_list = qa_setup
    other = fake.add_card(project.cfg.backlog, "already there")
    _handoff_ref(project, card, qa_list)
    assert converge(project, lease) == [f"moved:{card.id}"]
    assert fake.card_list(card.id) == project.cfg.backlog
    assert fake.state["cards"][card.id]["pos"] > fake.state["cards"][other["id"]]["pos"]
    meta = project.refs.read(card_ref(card.id)).meta
    assert "from_list" not in meta and "pos" not in meta
    assert meta["handoff"] == ["h1", "h2"] and meta["conversation"] == CONV and meta["cause"] == "handoff"
    fake.user_move(card.id, qa_list)
    assert converge(project, lease) == [f"human_moved:{card.id}"]  # a later user move is never replayed
    assert fake.card_list(card.id) == qa_list


def test_converge_does_not_move_handoff_card_the_user_moved(qa_setup):
    project, fake, lease, card, qa_list = qa_setup
    value = _handoff_ref(project, card, qa_list)
    fake.user_move(card.id, project.cfg.done)
    assert converge(project, lease) == [f"human_moved:{card.id}"]
    assert fake.card_list(card.id) == project.cfg.done
    assert project.refs.read(card_ref(card.id)).oid == value.oid


def test_converge_ref_single_step(qa_setup):
    project, fake, lease, card, qa_list = qa_setup
    value = _handoff_ref(project, card, qa_list)
    ref = card_ref(card.id)
    assert cards.converge_ref(project, lease, ref, value, project.trello.card(card.id)) == f"moved:{card.id}"
    assert fake.card_list(card.id) == project.cfg.backlog
    value = project.refs.read(ref)
    assert cards.converge_ref(project, lease, ref, value, project.trello.card(card.id)) is None
    qa_value = put_card_ref(project, "f" * 24, "zzzzzzzz", phase="qa", owner="claude-qa", target_list=qa_list,
                            conversation=CONV)
    assert cards.converge_ref(project, lease, card_ref("f" * 24), qa_value, None) is None


def test_delete_integrated_ref_discards_conversation_dir(qa_setup):
    project, fake, lease, card, qa_list = qa_setup
    fake.user_move(card.id, project.cfg.done)
    value = put_card_ref(project, card.id, card.short, phase="integrated", target_list=project.cfg.done,
                         merge="a" * 40, conversation=CONV, handoff=["h1"])
    directory = qa.conversation_dir(project.paths, CONV)
    directory.mkdir(parents=True)
    (directory / "turn.json").write_text("{}")
    assert cards.delete_integrated_ref(project, card_ref(card.id), value)
    assert not directory.exists()


def test_recovery_comment_covers_qa_refs(qa_setup):
    project, fake, lease, card, qa_list = qa_setup
    put_card_ref(project, card.id, card.short, phase="qa", owner="claude-qa", target_list=qa_list,
                 conversation=CONV)
    seats._comment_recovery(project, "claude-qa", "c" * 40)
    assert any("claude-qa" in text for text in fake.card_comments(card.id))
