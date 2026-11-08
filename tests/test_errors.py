import pytest

from kankan import clock, faults
from kankan.errors import KankanError, Status, envelope

SPEC_TABLE = {
    "success": 0, "no_work": 10, "already_claimed": 11, "ineligible": 12,
    "main_moved": 13, "needs_action": 14, "on_hold": 15, "network_error": 20,
    "trello_error": 21, "needs_reconciliation": 30, "not_owner": 31,
    "config_error": 40, "usage_error": 41, "internal_error": 50,
}


def test_exit_codes_match_spec():
    assert {s.value: s.exit_code for s in Status} == SPEC_TABLE


def test_envelope_error():
    assert envelope(KankanError(Status.NOT_OWNER, "x")) == {
        "ok": False, "status": "not_owner", "message": "x", "data": None}


def test_envelope_success():
    assert envelope({"a": 1}) == {"ok": True, "status": "success", "message": "", "data": {"a": 1}}


def test_fault_point(monkeypatch):
    monkeypatch.setenv("KANKAN_FAULT", "a")
    with pytest.raises(faults.SimulatedCrash):
        faults.point("a")
    faults.point("b")


def test_clock_now_is_aware_utc():
    assert clock.now().utcoffset().total_seconds() == 0
