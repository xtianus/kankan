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
