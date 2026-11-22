"""seat T§4–T§7: classification and moves of a removed seat's cards, completion, autostart, immediate removal."""

import pytest

from kankan import agents_cmd, labels, messages, seatremove, seats, stopall, workflow, worktrees
from kankan.config import save_config
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref
from tests.conftest import git
from tests.flow import implement, review_setup
from tests.helpers import hold, put_card_ref
from tests.test_stopall import systemctl  # noqa: F401 - fixture


def _register(project, *names):
    for name in names:
        seats.release(project, seats.acquire(project, seats.seat_type(name), seat=name))
    project.reload_config()


def _removing(project, seat, mode="immediate"):
    cfg = project.reload_config()
    cfg.seats[seat].removing = mode
    save_config(project.paths, cfg)
    project.reload_config()


def _apply_all(project, seat):
    return [done for move in seatremove.plan_moves(project, seat)
            if (done := seatremove.apply_move(project, seat, move)) is not None]


def _comments(fake, card_id, seat="claude-1"):
    return [c for c in fake.card_comments(card_id) if f"seat {seat} was removed" in c]


def _meta(project, card):
    value = project.refs.read(card_ref(card["id"]))
    return value.meta if value else None


def _pos_order(project, list_id):
    snap = project.trello.snapshot(project.cfg.board_id)
    return [c.id for c in sorted(snap.cards.values(), key=lambda c: c.pos) if c.list_id == list_id]


# ---------------------------------------------------------------------- the rows of T§4.1

def test_row1_integrated_card_goes_to_done(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _register(project, "claude-1", "codex-1")
    card = fake.add_card(project.cfg.seats["codex-1"].review, "merged")
    put_card_ref(project, card["id"], card["shortLink"], phase="integrated", implementer="claude-1",
                 implementer_type="claude", target_list=project.cfg.done)
    _removing(project, "codex-1")
    moves = _apply_all(project, "codex-1")
    assert [m.row for m in moves] == [1]
    assert fake.card_list(card["id"]) == project.cfg.done and _meta(project, card) is None
    assert len(_comments(fake, card["id"], "codex-1")) == 1


def test_row2_impl_card_parked_to_backlog_top(kproject, fake_trello):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    other = fake.add_card(project.cfg.backlog, "waiting", pos=0)
    _removing(project, "claude-1")
    assert [m.row for m in _apply_all(project, "claude-1")] == [2]
    meta = _meta(project, card)
    assert meta["phase"] == "parked" and meta["cause"] == "seat_removed" and meta.get("owner") is None
    assert _pos_order(project, project.cfg.backlog)[0] == card["id"] and other["id"] in _pos_order(project, project.cfg.backlog)
    assert len(_comments(fake, card["id"])) == 1


def test_row3_review_card_back_to_implementer(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    _removing(project, "codex-1")
    assert [m.row for m in _apply_all(project, "codex-1")] == [3]
    assert _meta(project, card)["phase"] == "implemented" and _meta(project, card).get("owner") is None
    assert fake.card_list(card["id"]) == project.cfg.seats["claude-1"].implemented


def test_row4_card_held_by_other_seat_to_its_list(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _register(project, "claude-1", "claude-2")
    card = fake.add_card(project.cfg.seats["claude-1"].in_progress, "theirs")
    put_card_ref(project, card["id"], card["shortLink"], phase="impl", owner="claude-2", implementer="claude-2",
                 implementer_type="claude", target_list=project.cfg.seats["claude-2"].in_progress)
    _removing(project, "claude-1")
    assert [m.row for m in _apply_all(project, "claude-1")] == [4]
    assert fake.card_list(card["id"]) == project.cfg.seats["claude-2"].in_progress
    assert _meta(project, card)["owner"] == "claude-2"


def test_row5_block_pending_goes_to_blocked_and_keeps_worktree(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _register(project, "claude-1", "codex-1")
    card = fake.add_card(project.cfg.seats["codex-1"].review, "blocked")
    put_card_ref(project, card["id"], card["shortLink"], phase="parked", cause="block", implementer="claude-1",
                 implementer_type="claude", target_list=project.cfg.blocked,
                 from_list=project.cfg.seats["codex-1"].review)
    blocked = project.paths.blocked_worktree(card["shortLink"])
    blocked.mkdir(parents=True)
    (blocked / "keep.txt").write_text("x")
    _removing(project, "codex-1")
    assert [m.row for m in _apply_all(project, "codex-1")] == [5]
    assert fake.card_list(card["id"]) == project.cfg.blocked and (blocked / "keep.txt").exists()
    assert "from_list" not in _meta(project, card)


def test_row5_handoff_pending_keeps_summary_and_bottom_position(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _register(project, "claude-1")
    first = fake.add_card(project.cfg.backlog, "first")
    card = fake.add_card(project.cfg.seats["claude-1"].in_progress, "handed off")
    put_card_ref(project, card["id"], card["shortLink"], phase="parked", cause="handoff", handoff=["c1", "c2"],
                 conversation="0123456789abcdef", target_list=project.cfg.backlog, pos="bottom",
                 from_list=project.cfg.seats["claude-1"].in_progress)
    _removing(project, "claude-1")
    assert [m.row for m in _apply_all(project, "claude-1")] == [5]
    assert _pos_order(project, project.cfg.backlog) == [first["id"], card["id"]]
    meta = _meta(project, card)
    assert meta["handoff"] == ["c1", "c2"] and meta["conversation"] == "0123456789abcdef"


def test_row6_implemented_to_same_type_seat(kproject, fake_trello):
    project, fake = kproject, fake_trello
    claude1 = seats.acquire(project, "claude")
    _register(project, "claude-2", "codex-1")
    card = implement(project, fake, claude1)
    _removing(project, "claude-1")
    assert [m.row for m in _apply_all(project, "claude-1")] == [6]
    assert fake.card_list(card["id"]) == project.cfg.seats["claude-2"].implemented
    assert _meta(project, card)["implementer"] == "claude-1"


def test_remove_last_work_seat_parks_implemented_to_backlog(kproject, fake_trello):
    project, fake = kproject, fake_trello
    claude1 = seats.acquire(project, "claude")
    card = implement(project, fake, claude1)
    _removing(project, "claude-1")
    _apply_all(project, "claude-1")
    meta = _meta(project, card)
    assert meta["phase"] == "parked" and meta["cause"] == "seat_removed"
    assert fake.card_list(card["id"]) == project.cfg.backlog


def test_cross_type_fallback_reviewed_by_other_type(kproject, fake_trello):
    project, fake = kproject, fake_trello
    claude1 = seats.acquire(project, "claude")
    codex = seats.acquire(project, "codex")
    project.reload_config()
    card = implement(project, fake, claude1)
    _removing(project, "claude-1")
    _apply_all(project, "claude-1")
    assert fake.card_list(card["id"]) == project.cfg.seats["codex-1"].implemented
    task = workflow.tick(project, codex)
    assert task["kind"] == "review" and task["card"] == card["id"]


def test_row7_card_without_ref_to_backlog_with_comment(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _register(project, "claude-1")
    card = fake.add_card(project.cfg.seats["claude-1"].review, "dragged here")
    _removing(project, "claude-1")
    assert [m.row for m in _apply_all(project, "claude-1")] == [7]
    assert fake.card_list(card["id"]) == project.cfg.backlog and len(_comments(fake, card["id"])) == 1


def test_row8_closed_held_card_parked_not_moved(kproject, fake_trello):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    in_progress = fake.card_list(card["id"])
    fake.user_archive(card["id"])
    _removing(project, "claude-1")
    assert [m.row for m in _apply_all(project, "claude-1")] == [8]
    meta = _meta(project, card)
    assert meta["phase"] == "parked" and meta.get("owner") is None and meta.get("target_list") is None
    assert fake.card_list(card["id"]) == in_progress


def test_ref_targeting_seat_list_from_elsewhere_is_found(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _register(project, "claude-1", "claude-2", "codex-1")
    card = fake.add_card(project.cfg.seats["codex-1"].review, "yield not moved yet")
    put_card_ref(project, card["id"], card["shortLink"], phase="implemented", implementer="claude-1",
                 implementer_type="claude", target_list=project.cfg.seats["claude-1"].implemented,
                 from_list=project.cfg.seats["codex-1"].review)
    _removing(project, "claude-1")
    assert [m.row for m in _apply_all(project, "claude-1")] == [6]
    assert fake.card_list(card["id"]) == project.cfg.seats["claude-2"].implemented
    assert _meta(project, card)["target_list"] == project.cfg.seats["claude-2"].implemented


def test_hold_kept(kproject, fake_trello):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    fake.user_label(card["id"], project.cfg.label_hold)
    _removing(project, "claude-1")
    _apply_all(project, "claude-1")
    assert project.cfg.label_hold in project.trello.card(card["id"]).label_ids


def test_labels_of_removed_seat_cleared(kproject, fake_trello):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    labels.set_label(project, card["id"], project.cfg.label_question, "ask", "claude-1")
    fake.user_label(card["id"], project.cfg.label_hold)
    _removing(project, "claude-1")
    _apply_all(project, "claude-1")
    ids = project.trello.card(card["id"]).label_ids
    assert project.cfg.label_question not in ids and project.cfg.label_hold in ids


def test_malformed_ref_refuses(kproject, fake_trello):
    project = kproject
    _register(project, "claude-1")
    git(project.paths.repo, "update-ref", card_ref("a" * 24), git(project.paths.repo, "rev-parse", "HEAD"))
    _removing(project, "claude-1")
    with pytest.raises(KankanError) as err:
        seatremove.plan_moves(project, "claude-1")
    assert err.value.status == Status.NEEDS_ACTION and "a" * 24 in err.value.message


def test_card_claimed_meanwhile_is_reclassified(kproject, fake_trello):
    project, fake = kproject, fake_trello
    claude1 = seats.acquire(project, "claude")
    codex = seats.acquire(project, "codex")
    project.reload_config()
    card = implement(project, fake, claude1)
    _removing(project, "claude-1")
    moves = seatremove.plan_moves(project, "claude-1")
    assert [m.row for m in moves] == [6]
    assert workflow.tick(project, codex)["kind"] == "review"  # the reviewer wins before the move
    assert seatremove.apply_move(project, "claude-1", moves[0]) is None
    assert _meta(project, card)["owner"] == "codex-1"
    assert fake.card_list(card["id"]) == project.cfg.seats["codex-1"].review and _comments(fake, card["id"]) == []


def test_failed_trello_move_not_commented(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    _removing(project, "claude-1")

    def fail(*args, **kwargs):
        raise KankanError(Status.TRELLO_ERROR, "down")
    monkeypatch.setattr(project.trello, "move_card", fail)
    assert _apply_all(project, "claude-1") == []
    assert _meta(project, card)["phase"] == "parked" and _comments(fake, card["id"]) == []


def test_rerun_does_not_duplicate_comments(kproject, fake_trello):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    _removing(project, "claude-1")
    move = seatremove.plan_moves(project, "claude-1")[0]
    assert seatremove.apply_move(project, "claude-1", move) is not None
    assert seatremove.apply_move(project, "claude-1", move) is None
    assert _apply_all(project, "claude-1") == []
    assert len(_comments(fake, card["id"])) == 1


# ---------------------------------------------------------------------- completion (T§6)

def test_complete_entry_deleted_last(kproject, fake_trello, monkeypatch):
    project = kproject
    _register(project, "claude-1", "claude-2")
    _removing(project, "claude-1")
    real, calls = project.trello.archive_list, []

    def archive(list_id):
        calls.append(list_id)
        if len(calls) == 2:
            raise KankanError(Status.TRELLO_ERROR, "down")
        real(list_id)
    monkeypatch.setattr(project.trello, "archive_list", archive)
    with pytest.raises(KankanError) as err:
        seatremove.complete(project, "claude-1")
    assert err.value.status == Status.NEEDS_ACTION
    seat = project.reload_config().seats["claude-1"]
    assert seat.removing == "immediate" and "down" in seat.remove_error
    monkeypatch.setattr(project.trello, "archive_list", real)
    report = seatremove.complete(project, "claude-1")
    assert report["seat"] == "claude-1" and "claude-1" not in project.reload_config().seats


def test_failed_move_keeps_lists_and_sets_remove_error(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    seats.release(project, lease)
    _removing(project, "claude-1")
    lists = seatremove.seat_lists(project.cfg, "claude-1")

    def fail(*args, **kwargs):
        raise KankanError(Status.TRELLO_ERROR, "down")
    monkeypatch.setattr(project.trello, "move_card", fail)
    with pytest.raises(KankanError) as err:
        seatremove.complete(project, "claude-1")
    assert err.value.status == Status.NEEDS_ACTION and card["shortLink"] in err.value.message
    assert all(not project.trello.list_info(lst).closed for lst in lists)
    assert card["shortLink"] in project.reload_config().seats["claude-1"].remove_error


def test_crash_after_archive_rerun_completes(kproject, fake_trello, monkeypatch):
    project = kproject
    _register(project, "claude-1")
    _removing(project, "claude-1")
    monkeypatch.setattr(seatremove, "_forget_seat", lambda project, seat: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        seatremove.complete(project, "claude-1")
    monkeypatch.undo()
    assert seatremove.complete(project, "claude-1")["seat"] == "claude-1"
    assert "claude-1" not in project.reload_config().seats


def test_complete_dead_seat_recovers_first(kproject, fake_trello):
    project = kproject
    lease = seats.acquire(project, "claude")
    seats.release(project, lease, retain=True)
    assert seats.liveness(project, "claude-1") == "dead"
    _removing(project, "claude-1")
    seatremove.complete(project, "claude-1")
    assert "claude-1" not in project.reload_config().seats


def test_complete_alive_seat_refused(kproject, fake_trello, holders):
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    _removing(project, "claude-1")
    with pytest.raises(KankanError) as err:
        seatremove.complete(project, "claude-1")
    assert err.value.status == Status.NEEDS_ACTION and "claude-1" in project.reload_config().seats


def test_worktree_removed_before_moves(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    branch = project.refs.read(card_ref(card["id"])).meta["branch"]
    (project.paths.seat_worktree("claude-1") / "wip.txt").write_text("uncommitted")
    seats.release(project, lease)
    _removing(project, "claude-1")
    real, seen = seatremove.apply_move, []

    def apply_move(project_, seat, move):
        seen.append(worktrees.where_checked_out(project.paths.repo, branch))
        return real(project_, seat, move)
    monkeypatch.setattr(seatremove, "apply_move", apply_move)
    report = seatremove.complete(project, "claude-1")
    assert seen == [None] and report["discarded"] == ["wip.txt"]
    assert not project.paths.seat_worktree("claude-1").exists()


def test_lock_files_kept(kproject, fake_trello):
    project = kproject
    _register(project, "claude-1")
    locks = sorted(p.name for p in project.paths.seats_dir.glob("claude-1*.lock"))
    assert locks
    _removing(project, "claude-1")
    seatremove.complete(project, "claude-1")
    assert all((project.paths.seats_dir / name).exists() for name in locks)
    assert not (project.paths.seats_dir / "claude-1.state.json").exists()


def test_label_causes_swept_except_blocked(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _register(project, "claude-1")
    elsewhere = fake.add_card(project.cfg.done, "asked once")
    blocked = fake.add_card(project.cfg.blocked, "blocked")
    for card in (elsewhere, blocked):
        labels.set_label(project, card["id"], project.cfg.label_error, "fail", "claude-1")
    _removing(project, "claude-1")
    seatremove.complete(project, "claude-1")
    assert project.cfg.label_error not in project.trello.card(elsewhere["id"]).label_ids
    assert project.cfg.label_error in project.trello.card(blocked["id"]).label_ids


def test_second_completion_returns_same_report(kproject, fake_trello):
    project = kproject
    _register(project, "claude-1")
    _removing(project, "claude-1")
    first = seatremove.complete(project, "claude-1")
    assert seatremove.complete(project, "claude-1") == first


def test_complete_qa_seat_ends_conversation_card_stays(kproject, fake_trello):
    from kankan import qa
    from kankan.adapters import ModelEffort
    from tests.helpers import add_qa_list
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "claude", None)
    card = fake.add_card(qa_list, "question")
    task = qa.claim(project, lease, project.trello.card(card["id"]), ModelEffort("sonnet", "high"))
    seats.release(project, lease)
    _removing(project, "claude-qa")
    seatremove.complete(project, "claude-qa")
    assert "claude-qa" not in project.reload_config().seats
    assert project.refs.read(card_ref(card["id"])) is None and fake.card_list(card["id"]) == qa_list
    assert not qa.conversation_dir(project.paths, task["conversation"]).exists()


# ---------------------------------------------------------------------- autostart and stop record (T§7)

def _unit(project, args):
    from tests.test_stopall import install_unit
    return install_unit(project, args)


def test_autostart_lowered_to_seats_left(kproject, systemctl):
    project = kproject
    _register(project, "claude-1", "claude-2", "codex-1")
    _unit(project, "claude=2 codex=1")
    _removing(project, "claude-1")
    assert seatremove.adjust_autostart(project, "claude-1") == "claude=1 codex=1"
    assert "up claude=1 codex=1" in agents_cmd.unit_path(project).read_text()


def test_autostart_never_raised(kproject, systemctl):
    project = kproject
    _register(project, "claude-1", "claude-2", "claude-3")
    _unit(project, "claude=1")
    _removing(project, "claude-3")
    assert seatremove.adjust_autostart(project, "claude-3") == "unchanged"


def test_autostart_qa_dropped(kproject, systemctl):
    project = kproject
    _register(project, "claude-1")
    cfg = project.reload_config()
    from kankan.config import SeatConfig
    cfg.seats["codex-qa"] = SeatConfig(type="codex", role="qa")
    save_config(project.paths, cfg)
    _unit(project, "claude=1 qa=codex")
    _removing(project, "codex-qa")
    assert seatremove.adjust_autostart(project, "codex-qa") == "claude=1"


def test_autostart_empty_disables(kproject, systemctl):
    project = kproject
    _register(project, "claude-1")
    _unit(project, "claude=1")
    _removing(project, "claude-1")
    assert seatremove.adjust_autostart(project, "claude-1") == "disabled"
    assert not agents_cmd.unit_path(project).exists()


def test_autostart_preview_writes_nothing(kproject, systemctl):
    project = kproject
    _register(project, "claude-1", "claude-2")
    path = _unit(project, "claude=2")
    before = path.read_text()
    assert seatremove.adjust_autostart(project, "claude-1", apply=False) == "claude=1"
    assert path.read_text() == before and not systemctl[0].exists()


def test_autostart_systemctl_failure_reported(kproject, systemctl):
    project = kproject
    _register(project, "claude-1", "claude-2")
    _unit(project, "claude=2")
    systemctl[1].write_text("fail")
    _removing(project, "claude-1")
    assert seatremove.adjust_autostart(project, "claude-1").startswith("error: ")


def test_stop_record_adjusted_or_deleted_without_unit(kproject):
    from kankan.config import atomic_write_json
    project = kproject
    _register(project, "claude-1", "claude-2")
    path = stopall.record_path(project.paths)
    atomic_write_json(path, {"up": "claude=2", "autostart": False, "seats": ["claude-1", "claude-2"]})
    _removing(project, "claude-1")
    assert seatremove.adjust_autostart(project, "claude-1") == "unchanged"
    assert stopall.load_record(project.paths)["up"] == "claude=1"
    _removing(project, "claude-2")
    seatremove.adjust_autostart(project, "claude-2")
    assert not path.exists()


# ---------------------------------------------------------------------- immediate removal (T§4)

def _no(text):
    raise AssertionError("no question expected: " + text)


def _dead_with_card(project, fake):
    """claude-1 claims a card, then dies (its flock is released, its seat ref stays)."""
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    workflow.tick(project, lease)
    seats._release_fd(lease.fd)
    return card


def test_remove_idle_seat_without_cards_no_question(kproject, fake_trello, holders):
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    report = seatremove.remove(project, "claude-1", graceful=False, confirm=_no, say=lambda t: None)
    assert report["seat"] == "claude-1" and "claude-1" not in project.reload_config().seats
    assert holders[-1].wait(10) is not None


def test_remove_alive_seat_with_card_asks_then_kills(kproject, fake_trello, holders):
    project, fake = kproject, fake_trello
    holders.append(hold(project, "claude-1", mode="ignore"))
    project.reload_config()
    card = fake.add_card(project.cfg.seats["claude-1"].in_progress, "held")
    put_card_ref(project, card["id"], card["shortLink"], phase="impl", owner="claude-1", implementer="claude-1",
                 implementer_type="claude", target_list=project.cfg.seats["claude-1"].in_progress)
    asked = []
    report = seatremove.remove(project, "claude-1", graceful=False, confirm=lambda t: asked.append(t) or False,
                               say=lambda t: None)
    assert report["cancelled"] and seats.liveness(project, "claude-1") == "alive"
    assert "held" in asked[0] or card["shortLink"] in asked[0]
    assert "--graceful" in asked[0] and project.reload_config().seats["claude-1"].removing is None
    seatremove.remove(project, "claude-1", graceful=False, confirm=lambda t: True, say=lambda t: None)
    assert "claude-1" not in project.reload_config().seats and fake.card_list(card["id"]) == project.cfg.backlog


def test_next_implementer_continues_from_the_branch(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    branch = project.refs.read(card_ref(card["id"])).meta["branch"]
    wt = project.paths.seat_worktree("claude-1")
    (wt / "done.txt").write_text("x\n")
    git(wt, "add", "done.txt")
    git(wt, "commit", "-qm", "work")
    tip = git(project.paths.repo, "rev-parse", branch)
    seatremove.remove(project, "claude-1", graceful=False, confirm=lambda t: True, say=lambda t: None)
    other = seats.acquire(project, "claude")
    assert workflow.tick(project, other)["card"] == card["id"]
    assert git(project.paths.seat_worktree(other.seat), "rev-parse", "HEAD") == tip


def test_mark_before_kill_moves_late_claim(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    late = fake.add_card(project.cfg.seats["claude-1"].review, "late")

    def confirm(text):
        put_card_ref(project, late["id"], late["shortLink"], phase="review", owner="claude-1",
                     implementer="codex-1", implementer_type="codex",
                     target_list=project.cfg.seats["claude-1"].review)
        return True
    report = seatremove.remove(project, "claude-1", graceful=False, confirm=confirm, say=lambda t: None)
    assert {m["card"] for m in report["moved"]} == {card["id"], late["id"]}


def test_uncommitted_changes_discarded_merge_aborted(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    branch = project.refs.read(card_ref(card["id"])).meta["branch"]
    repo, wt = project.paths.repo, project.paths.seat_worktree("claude-1")
    git(repo, "branch", "side", project.cfg.integration_branch)
    git(wt, "merge", "--no-commit", "--no-ff", "side")
    (wt / "wip.txt").write_text("unsaved")
    before = git(repo, "rev-parse", branch)
    report = seatremove.remove(project, "claude-1", graceful=False, confirm=lambda t: True, say=lambda t: None)
    assert git(repo, "rev-parse", branch) == before and "wip.txt" in report["discarded"]
    assert not wt.exists()


def test_survivors_block_before_moves(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    monkeypatch.setattr(seats, "recover_dead", lambda project, seat, kill_alive=False: [4242])
    with pytest.raises(KankanError) as err:
        seatremove.remove(project, "claude-1", graceful=False, confirm=lambda t: True, say=lambda t: None)
    assert err.value.status == Status.NEEDS_ACTION and "kankan seat recover claude-1 --kill" in err.value.message
    assert fake.card_list(card["id"]) == project.cfg.seats["claude-1"].in_progress


def test_remove_dead_seat(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    seatremove.remove(project, "claude-1", graceful=False, confirm=lambda t: True, say=lambda t: None)
    assert "claude-1" not in project.reload_config().seats and fake.card_list(card["id"]) == project.cfg.backlog


def test_no_terminal_without_yes_is_usage_error(kproject, fake_trello, monkeypatch):
    import sys
    from kankan import cli
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    monkeypatch.chdir(project.paths.repo)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["seat", "remove", "claude-1"]) == Status.USAGE_ERROR.exit_code
    assert "claude-1" in project.reload_config().seats and project.reload_config().seats["claude-1"].removing is None
    assert cli.main(["seat", "remove", "claude-1", "--yes"]) == 0
    assert fake.card_list(card["id"]) == project.cfg.backlog


def test_interrupted_immediate_completed_without_question(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    _removing(project, "claude-1", "immediate")
    for graceful in (True,):
        seatremove.remove(project, "claude-1", graceful=graceful, confirm=_no, say=lambda t: None)
    assert "claude-1" not in project.reload_config().seats and fake.card_list(card["id"]) == project.cfg.backlog


def test_systemctl_failure_through_seat_remove(kproject, fake_trello, systemctl):
    project = kproject
    _register(project, "claude-1", "claude-2")
    _unit(project, "claude=2")
    systemctl[1].write_text("fail")
    with pytest.raises(KankanError) as err:
        seatremove.remove(project, "claude-1", graceful=False, confirm=_no, say=lambda t: None)
    assert err.value.status == Status.NEEDS_ACTION and "claude-1" not in project.reload_config().seats
    assert err.value.data["autostart"].startswith("error: ")


def test_report_text(kproject):
    text = seatremove.report_text({"seat": "claude-1", "moved": [{"card": "x", "short": "AbC", "to": kproject.cfg.backlog}],
                                   "discarded": ["wip.txt"], "lists_archived": 3, "worktree_removed": True,
                                   "autostart": "claude=1"}, kproject.cfg)
    assert "claude-1 removed" in text and "AbC → Backlog" in text and "wip.txt" in text and "claude=1" in text
    assert "nothing changed" in seatremove.report_text({"cancelled": True, "seat": "claude-1"}, kproject.cfg)


# ---------------------------------------------------------------------- graceful removal and repeated request (T§5)

@pytest.fixture
def starts(monkeypatch):
    """agents_cmd.agent replaced: records the starts; the wait for the started seat returns at once."""
    calls = []

    def agent(project, agent_type, seat=None, attach=False, tmux=None, preflight=True, qa=False, create=True):
        calls.append((agent_type, seat, create))
        return f"ai-proj-{seat}"
    monkeypatch.setattr(agents_cmd, "agent", agent)
    monkeypatch.setattr(seatremove, "await_start", lambda project, seat, session: None)
    return calls


def test_graceful_request_sets_flag_and_autostart(kproject, fake_trello, holders, systemctl, monkeypatch):
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    _register(project, "claude-2")
    _unit(project, "claude=2")
    seen = {}
    monkeypatch.setattr(seatremove, "_wait", lambda project_, seat, say, poll, **kw: seen.setdefault("seat", seat) and {"seat": seat})
    report = seatremove.remove(project, "claude-1", graceful=True, confirm=_no, say=lambda t: None)
    assert seen["seat"] == "claude-1" and report["autostart"] == "claude=1"
    assert project.reload_config().seats["claude-1"].removing == "graceful"


def test_graceful_stopped_seat_with_card_is_started_and_awaited(kproject, fake_trello, starts, monkeypatch):
    project, fake = kproject, fake_trello
    _dead_with_card(project, fake)
    monkeypatch.setattr(seatremove, "_wait", lambda project_, seat, say, poll, **kw: {"seat": seat})
    seatremove.remove(project, "claude-1", graceful=True, confirm=_no, say=lambda t: None)
    assert starts == [("claude", "claude-1", False)]


def test_graceful_preflight_failure_keeps_flag(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    _dead_with_card(project, fake)

    def agent(*args, **kwargs):
        raise KankanError(Status.CONFIG_ERROR, "claude CLI missing")
    monkeypatch.setattr(agents_cmd, "agent", agent)
    with pytest.raises(KankanError) as err:
        seatremove.remove(project, "claude-1", graceful=True, confirm=_no, say=lambda t: None)
    assert err.value.status == Status.NEEDS_ACTION and "claude CLI missing" in err.value.message
    assert project.reload_config().seats["claude-1"].removing == "graceful"


def test_graceful_seat_without_work_completes_now(kproject, fake_trello, starts):
    project = kproject
    _register(project, "claude-1")
    report = seatremove.remove(project, "claude-1", graceful=True, confirm=_no, say=lambda t: None)
    assert report["seat"] == "claude-1" and "claude-1" not in project.reload_config().seats and starts == []


def test_graceful_other_code_refused(kproject, fake_trello, holders, monkeypatch):
    from kankan import build
    from tests.test_stopall import record_launcher_code
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    record_launcher_code(project, "claude-1")
    monkeypatch.setattr(build, "code_id", lambda: "0" * 12)
    with pytest.raises(KankanError) as err:
        seatremove.remove(project, "claude-1", graceful=True, confirm=_no, say=lambda t: None)
    assert err.value.status == Status.USAGE_ERROR and "without --graceful" in err.value.message
    assert project.reload_config().seats["claude-1"].removing is None


def test_wait_reports_agent_stopped_with_work(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _dead_with_card(project, fake)
    _removing(project, "claude-1", "graceful")
    with pytest.raises(KankanError) as err:
        seatremove._wait(project, "claude-1", lambda t: None, 0.01, start_grace=0)
    assert err.value.status == Status.NEEDS_ACTION and "kankan up" in err.value.message


def test_wait_reports_remove_error(kproject, fake_trello, holders):
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    _removing(project, "claude-1", "graceful")
    cfg = project.reload_config()
    cfg.seats["claude-1"].remove_error = "card X left"
    save_config(project.paths, cfg)
    with pytest.raises(KankanError) as err:
        seatremove._wait(project, "claude-1", lambda t: None, 0.01, start_grace=0)
    assert "card X left" in err.value.message


def test_wait_ctrl_c_keeps_flag(kproject, fake_trello, holders, monkeypatch):
    from kankan import clock
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    _removing(project, "claude-1", "graceful")

    def interrupt(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(clock, "sleep", interrupt)
    with pytest.raises(KankanError) as err:
        seatremove._wait(project, "claude-1", lambda t: None, 0.01, start_grace=0)
    assert err.value.status == Status.NEEDS_ACTION
    assert project.reload_config().seats["claude-1"].removing == "graceful"


def test_repeated_graceful_needs_action_nothing_written(kproject, fake_trello, holders):
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    _removing(project, "claude-1", "graceful")
    before = (project.paths.kankan_dir / "config.json").read_text()
    with pytest.raises(KankanError) as err:
        seatremove.remove(project, "claude-1", graceful=True, confirm=_no, say=lambda t: None)
    assert err.value.status == Status.NEEDS_ACTION and "still running" in err.value.message
    assert "without --graceful" in err.value.message
    assert (project.paths.kankan_dir / "config.json").read_text() == before


def test_repeated_without_graceful_declined_keeps_graceful(kproject, fake_trello, holders):
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    _removing(project, "claude-1", "graceful")
    asked = []
    report = seatremove.remove(project, "claude-1", graceful=False, confirm=lambda t: asked.append(t) or False,
                               say=lambda t: None)
    assert report["cancelled"] and report["graceful_running"] and "still running" in asked[0]
    assert "now instead" in asked[0]
    assert project.reload_config().seats["claude-1"].removing == "graceful"


def test_repeated_without_graceful_confirmed_removes_now(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    _removing(project, "claude-1", "graceful")
    asked = []
    seatremove.remove(project, "claude-1", graceful=False, confirm=lambda t: asked.append(t) or True, say=lambda t: None)
    assert asked and "claude-1" not in project.reload_config().seats
    assert fake.card_list(card["id"]) == project.cfg.backlog
    _register(project, "claude-2")
    _removing(project, "claude-2", "graceful")
    asked.clear()
    seatremove.remove(project, "claude-2", graceful=False, confirm=lambda t: asked.append(t) or True, say=lambda t: None)
    assert asked and "claude-2" not in project.reload_config().seats  # asked even with nothing to move


def test_up_resumes_removals_once(kproject, fake_trello, starts):
    project, fake = kproject, fake_trello
    _dead_with_card(project, fake)
    _removing(project, "claude-1", "graceful")
    _register(project, "claude-2")
    _removing(project, "claude-2", "immediate")
    assert sorted(agents_cmd.resume_removals(project)) == ["claude-1", "claude-2"]
    assert starts == [("claude", "claude-1", False)] and "claude-2" not in project.reload_config().seats


def test_bare_up_with_only_removing_seat_starts_it(kproject, fake_trello, starts, monkeypatch):
    from kankan import cli
    project, fake = kproject, fake_trello
    _dead_with_card(project, fake)
    _removing(project, "claude-1", "graceful")
    monkeypatch.chdir(project.paths.repo)
    assert cli.main(["up"]) == 0
    assert starts == [("claude", "claude-1", False)]


def test_start_without_record_resumes_removals(kproject, fake_trello, starts, monkeypatch):
    from kankan import cli
    project, fake = kproject, fake_trello
    _dead_with_card(project, fake)
    _removing(project, "claude-1", "graceful")
    monkeypatch.chdir(project.paths.repo)
    assert cli.main(["start"]) == Status.NEEDS_ACTION.exit_code
    assert starts == [("claude", "claude-1", False)]


def test_has_work_counts_a_held_card(kproject, fake_trello):
    project, fake = kproject, fake_trello
    card = _dead_with_card(project, fake)
    assert seatremove.has_work(project, "claude-1")
    project.refs.delete(card_ref(card["id"]), project.refs.read(card_ref(card["id"])).oid)
    assert not seatremove.has_work(project, "claude-1")


# ---------------------------------------------------------------------- the Q&A seat (F§3.4)

def _qa_seat(project, fake, conversation=True):
    from kankan import qa
    from kankan.adapters import ModelEffort
    from tests.helpers import add_qa_list
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "claude", None)
    card = fake.add_card(qa_list, "question")
    if conversation:
        qa.claim(project, lease, project.trello.card(card["id"]), ModelEffort("sonnet", "high"))
    seats._release_fd(lease.fd)  # its launcher died
    return qa_list, card


def test_remove_qa_seat_immediate_card_stays_in_qa(kproject, fake_trello):
    project, fake = kproject, fake_trello
    qa_list, card = _qa_seat(project, fake)
    asked = []
    seatremove.remove(project, "claude-qa", graceful=False, confirm=lambda t: asked.append(t) or True, say=lambda t: None)
    assert asked and card["shortLink"] in asked[0]
    assert "claude-qa" not in project.reload_config().seats
    assert project.refs.read(card_ref(card["id"])) is None and fake.card_list(card["id"]) == qa_list


def test_remove_qa_seat_without_conversation_no_question(kproject, fake_trello):
    project, fake = kproject, fake_trello
    _qa_seat(project, fake, conversation=False)
    seatremove.remove(project, "claude-qa", graceful=False, confirm=_no, say=lambda t: None)
    assert "claude-qa" not in project.reload_config().seats
