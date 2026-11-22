"""seat T§3: the `Implemented` destination of a card whose implementer seat is gone or being removed."""

import copy

import pytest

from kankan import agentcmds, cards, integrate, seats, workflow
from kankan.config import SeatConfig, load_config, save_config
from kankan.refstore import card_ref
from tests.flow import agent_ctx, commit, implement, review_setup


def _cfg_with(kproject, entries):
    """A copy of the project configuration whose seats are ``{name: (type, implemented list, removing)}``."""
    cfg = copy.deepcopy(kproject.cfg)
    cfg.seats = {name: SeatConfig(type=t, implemented=lst, removing=rem) for name, (t, lst, rem) in entries.items()}
    return cfg


def _drop_seat(project, seat):
    cfg = project.reload_config()
    del cfg.seats[seat]
    save_config(project.paths, cfg)
    project.reload_config()


def _mark_removing(project, seat):
    cfg = project.reload_config()
    cfg.seats[seat].removing = "graceful"
    save_config(project.paths, cfg)
    project.reload_config()


def test_implemented_list_own_seat(kproject):
    cfg = _cfg_with(kproject, {"claude-1": ("claude", "L1", None), "claude-2": ("claude", "L2", None)})
    assert cards.implemented_list(cfg, "claude-1") == "L1"


def test_implemented_list_falls_back_same_type_by_number(kproject):
    cfg = _cfg_with(kproject, {"claude-1": ("claude", "L1", "graceful"), "claude-10": ("claude", "L10", None),
                               "claude-2": ("claude", "L2", None), "codex-1": ("codex", "C1", None)})
    assert cards.implemented_list(cfg, "claude-1") == "L2"


def test_implemented_list_then_any_type_then_none(kproject):
    cfg = _cfg_with(kproject, {"codex-2": ("codex", "C2", None), "codex-1": ("codex", "C1", None),
                               "claude-qa": ("claude", "", None)})
    cfg.seats["claude-qa"].role = "qa"
    assert cards.implemented_list(cfg, "claude-1") == "C1"  # removed: another type, by number; never the Q&A seat
    cfg = _cfg_with(kproject, {"codex-1": ("codex", "C1", "immediate")})
    assert cards.implemented_list(cfg, "claude-1") is None
    assert cards.implemented_destination(cfg, "claude-1") == ("parked", cfg.backlog, "seat_removed")
    cfg = _cfg_with(kproject, {"claude-1": ("claude", "L1", None)})
    assert cards.implemented_destination(cfg, "claude-1") == ("implemented", "L1", None)


def test_target_list_for_seat_removed_cause_is_backlog(kproject):
    assert cards.target_list_for(kproject.cfg, "parked", None, None, "seat_removed") == kproject.cfg.backlog


def test_review_yield_after_implementer_removed(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    _drop_seat(project, "claude-1")
    result = agentcmds.yield_(agent_ctx(project, codex, monkeypatch))
    meta = project.refs.read(card_ref(card["id"])).meta
    assert result["phase"] == "implemented" and meta["phase"] == "implemented"
    assert meta["implementer"] == "claude-1" and meta["implementer_type"] == "claude"
    assert fake.card_list(card["id"]) == project.cfg.seats["codex-1"].implemented


def test_hold_rollback_after_implementer_removed(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    claude = seats.acquire(project, "claude")
    codex = seats.acquire(project, "codex")
    project.reload_config()
    card = implement(project, fake, claude)
    _mark_removing(project, "claude-1")  # its Implemented list still counts for review until the removal moves it
    real = workflow.transition

    def transition(project_, actor, card_, expect, new, comment):
        result = real(project_, actor, card_, expect, new, comment)
        if new is not None and new.phase == "review":
            fake.user_label(card["id"], project.cfg.label_hold)
        return result
    monkeypatch.setattr(workflow, "transition", transition)
    assert workflow.tick(project, codex) is None
    meta = project.refs.read(card_ref(card["id"])).meta
    assert meta["phase"] == "implemented"
    assert fake.card_list(card["id"]) == project.cfg.seats["codex-1"].implemented


def test_resolve_done_after_implementer_removed(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    card, codex, ctx = review_setup(project, fake, monkeypatch)
    integrate.block(ctx, "spec unclear")
    _drop_seat(project, "claude-1")
    data, message = integrate.resolve(project, card["shortLink"], True)
    target = project.cfg.seats["codex-1"].implemented
    assert project.refs.read(card_ref(card["id"])).meta["phase"] == "implemented"
    assert fake.card_list(card["id"]) == target
    assert "codex-1" in message


def test_done_of_removing_seat_goes_to_other_implemented(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    claude1 = seats.acquire(project, "claude")
    seats.acquire(project, "claude")
    project.reload_config()
    card = fake.add_card(project.cfg.backlog, "feature", pos=0)
    workflow.tick(project, claude1)
    commit(project.paths.seat_worktree("claude-1"), "feature.txt")
    _mark_removing(project, "claude-1")
    result = agentcmds.done(agent_ctx(project, claude1, monkeypatch))
    assert result["phase"] == "implemented"
    assert fake.card_list(card["id"]) == project.cfg.seats["claude-2"].implemented


def test_done_last_seat_parks_and_retry_succeeds(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    claude1 = seats.acquire(project, "claude")
    project.reload_config()
    card = fake.add_card(project.cfg.backlog, "feature", pos=0)
    workflow.tick(project, claude1)
    commit(project.paths.seat_worktree("claude-1"), "feature.txt")
    _mark_removing(project, "claude-1")
    ctx = agent_ctx(project, claude1, monkeypatch)
    result = agentcmds.done(ctx)
    meta = project.refs.read(card_ref(card["id"])).meta
    assert result["phase"] == "parked" and meta["phase"] == "parked" and meta["cause"] == "seat_removed"
    assert fake.card_list(card["id"]) == project.cfg.backlog
    assert agentcmds.done(agent_ctx(project, claude1, monkeypatch))["phase"] == "parked"


def test_old_config_without_removing_loads(kproject):
    raw = kproject.cfg.to_dict()
    assert all("removing" not in seat and "remove_error" not in seat for seat in raw["seats"].values())
    cfg = load_config(kproject.paths)
    assert all(sc.removing is None and sc.remove_error is None for sc in cfg.seats.values())
