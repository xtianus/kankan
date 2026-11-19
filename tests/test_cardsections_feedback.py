"""Feedback record of invalid agent headings, `card-sections.json` (card instructions T§3)."""

import threading

import pytest

from kankan import cardsections_feedback as fb
from kankan.adapters import adapter_types
from kankan.cardsections import fingerprint, parse
from kankan.config import project_lock
from kankan.seatstate import update_state
from kankan.trello import marker

BAD = "spec\n## agent: cluade\nx\n"
BAD_B = "spec\n## agent: claude deploy\nx\n"
GOOD = "spec\n## agent: claude\nx\n"


@pytest.fixture
def env(kproject, fake_trello, virtual_time):
    return kproject, fake_trello


def _card(project, fake, desc=BAD, list_id=None):
    return fake.add_card(list_id or project.cfg.backlog, "c", desc=desc)


def _record(project, card_id):
    card = project.trello.card(card_id)
    types = adapter_types(project.paths)
    with project_lock(project.paths):
        fb.record_invalid_locked(project, card, fingerprint(card.desc, types), parse(card.desc, types).problems)


def _labels(project, fake, card_id):
    return fake.state["cards"][card_id]["idLabels"]


def _comments(project, card_id):
    return [c for c in project.trello.comments(card_id) if "kankan:sections-" in c.text]


def _rec(project, card_id):
    return fb.load(project.paths)["cards"].get(card_id)


def _set_desc(fake, card_id, desc):
    fake.state["cards"][card_id]["desc"] = desc


def _fix_and_clear(project, fake, card_id):
    _set_desc(fake, card_id, GOOD)
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    fb.drain(project)


def test_record_once_per_version(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    report = _rec(project, card["id"])["report"]
    _record(project, card["id"])
    fb.drain(project)
    comments = _comments(project, card["id"])
    assert len(comments) == 1 and comments[0].text.rstrip().endswith(marker(f"sections-{card['id']}-{report}"))
    assert "cluade" in comments[0].text
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    rec = _rec(project, card["id"])
    assert rec["owned_error"] and rec["error_confirmed"] and rec["pending"] == []
    before = len(fake.requests)
    _record(project, card["id"])
    fb.drain(project)
    assert [r for r in fake.requests[before:] if card["id"] in r[1] and r[0] != "GET"] == []
    assert len([r for r in fake.requests[before:] if card["id"] in r[1]]) == 1  # the test's own card read


def test_overlapping_drains_post_one_comment(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    threads = [threading.Thread(target=fb.drain, args=(project,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(_comments(project, card["id"])) == 1


def test_failed_comment_and_label_stay_pending(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fake.inject("POST", r"/actions/comments$", "500", times=10)
    fake.inject("POST", r"/idLabels$", "500", times=10)
    fb.drain(project)
    assert {a["kind"] for a in _rec(project, card["id"])["pending"]} == {"comment", "add_error"}
    fake.faults.clear()
    fb.drain(project)
    assert _rec(project, card["id"])["pending"] == []
    assert len(_comments(project, card["id"])) == 1
    assert project.cfg.label_error in _labels(project, fake, card["id"])


def test_existing_error_only_comments(env):
    project, fake = env
    card = _card(project, fake)
    fake.user_label(card["id"], project.cfg.label_error)
    _record(project, card["id"])
    fb.drain(project)
    assert len(_comments(project, card["id"])) == 1 and not _rec(project, card["id"])["owned_error"]
    _fix_and_clear(project, fake, card["id"])
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    assert _rec(project, card["id"]) is None


def test_foreign_label_gone_before_drain_kankan_owns_its_post(env):
    project, fake = env
    card = _card(project, fake)
    fake.user_label(card["id"], project.cfg.label_error)
    _record(project, card["id"])
    fake.state["cards"][card["id"]]["idLabels"].remove(project.cfg.label_error)
    fb.drain(project)
    assert _rec(project, card["id"])["owned_error"]
    _fix_and_clear(project, fake, card["id"])
    assert project.cfg.label_error not in _labels(project, fake, card["id"])


def test_crash_after_label_post(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fake.user_label(card["id"], project.cfg.label_error)  # the POST reached Trello...
    data = fb.load(project.paths)
    rec = data["cards"][card["id"]]
    rec["error_write_started"] = True  # ...and the launcher died before saving error_confirmed
    rec["pending"] = [a for a in rec["pending"] if a["kind"] != "add_error"]
    fb.save(project.paths, data)
    _fix_and_clear(project, fake, card["id"])
    assert project.cfg.label_error not in _labels(project, fake, card["id"])


def test_ownership_survives_new_version(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    _set_desc(fake, card["id"], BAD_B)
    _record(project, card["id"])
    fb.drain(project)
    assert len(_comments(project, card["id"])) == 2 and _rec(project, card["id"])["owned_error"]
    _fix_and_clear(project, fake, card["id"])
    assert project.cfg.label_error not in _labels(project, fake, card["id"])


def test_cancelled_clear_keeps_pending_feedback(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    _set_desc(fake, card["id"], GOOD)
    stale = project.trello.snapshot(project.cfg.board_id)
    _set_desc(fake, card["id"], BAD)
    fb.sweep(project, stale)
    fb.drain(project)
    assert len(_comments(project, card["id"])) == 1
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    assert _rec(project, card["id"])["state"] == "reported"


def test_confirmed_clear_drops_stale_feedback(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    _fix_and_clear(project, fake, card["id"])
    assert _comments(project, card["id"]) == [] and _rec(project, card["id"]) is None
    assert project.cfg.label_error not in _labels(project, fake, card["id"])


def test_restored_version_cancels_clear(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    _set_desc(fake, card["id"], GOOD)
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    assert _rec(project, card["id"])["state"] == "clearing"
    _set_desc(fake, card["id"], BAD)
    _record(project, card["id"])
    rec = _rec(project, card["id"])
    assert rec["state"] == "reported" and rec["pending"] == []
    fb.drain(project)
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    assert len(_comments(project, card["id"])) == 1


def test_stale_snapshot_does_not_clear(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    _set_desc(fake, card["id"], GOOD)
    stale = project.trello.snapshot(project.cfg.board_id)
    _set_desc(fake, card["id"], BAD)
    fb.sweep(project, stale)
    fb.drain(project)
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    assert _rec(project, card["id"])["state"] == "reported"


def test_sweep_moved_archived_deleted(env):
    project, fake = env
    other = fake.add_list(project.cfg.board_id, "Ideas")
    moved, archived, deleted = (_card(project, fake) for _ in range(3))
    for card in (moved, archived, deleted):
        _record(project, card["id"])
    fb.drain(project)
    fake.state["cards"][moved["id"]]["idList"] = other["id"]
    fake.state["cards"][archived["id"]]["closed"] = True
    del fake.state["cards"][deleted["id"]]
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    before = len(fake.requests)
    fb.drain(project)
    assert project.cfg.label_error not in _labels(project, fake, moved["id"])
    assert project.cfg.label_error not in _labels(project, fake, archived["id"])
    assert fb.load(project.paths)["cards"] == {}
    assert [r for r in fake.requests[before:] if deleted["id"] in r[1] and r[0] != "GET"] == []


def test_fix_keeps_error_of_other_cause(env):
    project, fake = env
    from kankan import seats
    lease = seats.acquire(project, "claude")
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    def record(state):
        state.label_causes.setdefault(card["id"], {})[project.cfg.label_error] = {"cause": "branches", "at": "2026-11-19T00:00:00+00:00"}
    update_state(project.paths, lease.seat, record)
    _fix_and_clear(project, fake, card["id"])
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    assert _rec(project, card["id"]) is None


def test_remove_when_label_gone(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    fake.state["cards"][card["id"]]["idLabels"].remove(project.cfg.label_error)
    _set_desc(fake, card["id"], GOOD)
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    before = len(fake.requests)
    fb.drain(project)
    assert not [r for r in fake.requests[before:] if r[0] == "DELETE"]
    assert _rec(project, card["id"]) is None


def test_invalid_again_after_fix_is_reported_again(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    _fix_and_clear(project, fake, card["id"])
    _set_desc(fake, card["id"], BAD)
    _record(project, card["id"])
    fb.drain(project)
    markers = {c.text.rstrip().rsplit("kankan:", 1)[1] for c in _comments(project, card["id"])}
    assert len(markers) == 2
    assert project.cfg.label_error in _labels(project, fake, card["id"])


def test_lost_record_reports_again(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    fb.path(project.paths).unlink()
    _record(project, card["id"])
    fb.drain(project)
    assert len(_comments(project, card["id"])) == 2


def test_corrupted_record_loads_empty(env):
    project, _ = env
    fb.path(project.paths).parent.mkdir(parents=True, exist_ok=True)
    fb.path(project.paths).write_text("{not json")
    assert fb.load(project.paths) == {"cards": {}}


def test_comment_capped(env):
    project, fake = env
    token = "\U0001F600" * 2000
    desc = "spec\n" + "".join(f"## agent: {token}{i}\n" for i in range(40))
    card = _card(project, fake, desc=desc)
    _record(project, card["id"])
    fb.drain(project)
    text = _comments(project, card["id"])[0].text
    assert text.count("is not an agent type") == 10 and "and 30 more" in text
    assert len(text.encode("utf-16-le")) // 2 < 16384


def test_sweep_and_drain_never_raise(env):
    import json
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fake.inject("GET", r"/1/cards/", "500", times=100)
    fb.drain(project)
    assert {a["kind"] for a in _rec(project, card["id"])["pending"]} == {"comment", "add_error"}
    fake.faults.clear()
    snap = project.trello.snapshot(project.cfg.board_id)
    (project.paths.kankan_dir / "agents.json").write_text(json.dumps({"all": {}}))
    fb.sweep(project, snap)
    fb.drain(project)

# ---------------------------------------------------------------- final review fixes


def _lock_held(project):
    from kankan import config
    entry = config._LOCK_DEPTH.get(str(project.paths.kankan_dir / "project.lock"))
    return entry is not None and entry[1] == threading.get_ident()


def test_rejected_label_post_is_not_kankans(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fake.inject("POST", r"/idLabels$", "404", times=100)
    fb.drain(project)
    assert not _rec(project, card["id"])["error_write_started"]
    fake.faults.clear()
    fake.user_label(card["id"], project.cfg.label_error)
    fb.drain(project)
    _fix_and_clear(project, fake, card["id"])
    assert project.cfg.label_error in _labels(project, fake, card["id"])


def test_unanswered_label_post_stays_kankans(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fake.inject("POST", r"/idLabels$", "500", times=100)
    fb.drain(project)
    assert _rec(project, card["id"])["error_write_started"]
    fake.faults.clear()
    fake.user_label(card["id"], project.cfg.label_error)  # the unanswered POST had reached Trello
    _fix_and_clear(project, fake, card["id"])
    assert project.cfg.label_error not in _labels(project, fake, card["id"])


def test_fixed_before_drain_posts_nothing(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    _set_desc(fake, card["id"], GOOD)
    fb.drain(project)
    assert _comments(project, card["id"]) == [] and project.cfg.label_error not in _labels(project, fake, card["id"])
    assert _rec(project, card["id"]) is None


def test_edited_before_drain_reports_again(env):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    _set_desc(fake, card["id"], BAD_B)
    fb.drain(project)
    assert _comments(project, card["id"]) == []
    _set_desc(fake, card["id"], BAD)
    _record(project, card["id"])
    fb.drain(project)
    assert len(_comments(project, card["id"])) == 1
    assert project.cfg.label_error in _labels(project, fake, card["id"])


def test_failing_card_does_not_block_others(env):
    project, fake = env
    first, second = _card(project, fake), _card(project, fake)
    _record(project, first["id"])
    _record(project, second["id"])
    fake.inject("POST", rf"/1/cards/{first['id']}/actions/comments$", "404", times=1000)
    fb.drain(project)
    fb.drain(project)
    assert len(_comments(project, second["id"])) == 1
    assert project.cfg.label_error in _labels(project, fake, second["id"])


def test_reads_happen_outside_the_project_lock(env, monkeypatch):
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    client = type(project.trello)
    for name in ("card", "comments"):
        original = getattr(client, name)
        def guarded(self, *args, _original=original, **kwargs):
            assert not _lock_held(project), "a Trello read under the project lock"
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(client, name, guarded)
    fb.drain(project)
    _set_desc(fake, card["id"], GOOD)
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    fb.drain(project)
    assert len(_comments(project, card["id"])) == 1 and _rec(project, card["id"]) is None


def test_malformed_entries_dropped(env):
    from kankan import diag
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    data = fb.load(project.paths)
    data["cards"]["x" * 24] = None
    data["cards"]["y" * 24] = {"fingerprint": 3, "pending": "no"}
    fb.save(project.paths, data)
    assert list(fb.load(project.paths)["cards"]) == [card["id"]]
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    fb.drain(project)
    diag.reconcile(project)
    assert len(_comments(project, card["id"])) == 1


def test_moved_into_settings_list_keeps_settings_error(env):
    from kankan import boardconfig, boardsettings
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    state = boardconfig.load(project.paths)
    state.cards[card["id"]] = {"key": "instructions", "invalid": True, "observed_error": True}
    boardsettings._save(project, state)
    fake.state["cards"][card["id"]]["idList"] = project.cfg.settings_list or fake.add_list(project.cfg.board_id, "kankan")["id"]
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    fb.drain(project)
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    assert _rec(project, card["id"]) is None


def _settings_cycle(project, fake, card_id):
    from datetime import datetime, timedelta, timezone
    from kankan import boardsettings
    # board settings evaluate only cards whose last activity is at least 10 seconds old
    fake.state["cards"][card_id]["dateLastActivity"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    adapters = boardsettings.adapters_for(project.paths)
    read = boardsettings.from_snapshot(project, project.trello.snapshot(project.cfg.board_id))
    boardsettings.apply_read(project, read, adapters)
    boardsettings.drain_actions(project)


def _settings_list(project, fake):
    from kankan.config import save_config
    if not project.cfg.settings_list:
        project.cfg.settings_list = fake.add_list(project.cfg.board_id, "kankan")["id"]
        save_config(project.paths, project.cfg)
    return project.cfg.settings_list


def test_moved_into_settings_list_error_handed_over_and_removed(env):
    from kankan import boardconfig
    project, fake = env
    card = _card(project, fake)
    _record(project, card["id"])
    fb.drain(project)
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    fake.user_move(card["id"], _settings_list(project, fake))
    _settings_cycle(project, fake, card["id"])  # unknown setting name: invalid, but Error is already there
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    fb.drain(project)
    assert _rec(project, card["id"]) is None
    assert project.cfg.label_error in _labels(project, fake, card["id"])
    entry = boardconfig.load(project.paths).cards[card["id"]]
    assert entry["owned_error"] and entry["error_confirmed"]
    fake.user_move(card["id"], project.cfg.backlog)
    _settings_cycle(project, fake, card["id"])
    assert project.cfg.label_error not in _labels(project, fake, card["id"])


def test_moved_into_settings_list_user_error_not_handed_over(env):
    from kankan import boardconfig
    project, fake = env
    card = _card(project, fake)
    fake.user_label(card["id"], project.cfg.label_error)
    _record(project, card["id"])
    fb.drain(project)
    fake.user_move(card["id"], _settings_list(project, fake))
    _settings_cycle(project, fake, card["id"])
    fb.sweep(project, project.trello.snapshot(project.cfg.board_id))
    fb.drain(project)
    assert _rec(project, card["id"]) is None
    assert not boardconfig.load(project.paths).cards[card["id"]].get("owned_error")
    fake.user_move(card["id"], project.cfg.backlog)
    _settings_cycle(project, fake, card["id"])
    assert project.cfg.label_error in _labels(project, fake, card["id"])
