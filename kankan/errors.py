"""Status codes, the kankan exception and the JSON envelope (spec §16.4)."""

from __future__ import annotations

import enum
from typing import Any, Optional


class Status(enum.Enum):
    SUCCESS = "success"
    NO_WORK = "no_work"
    ALREADY_CLAIMED = "already_claimed"
    INELIGIBLE = "ineligible"
    MAIN_MOVED = "main_moved"
    NEEDS_ACTION = "needs_action"
    ON_HOLD = "on_hold"
    NETWORK_ERROR = "network_error"
    TRELLO_ERROR = "trello_error"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    NOT_OWNER = "not_owner"
    CONFIG_ERROR = "config_error"
    USAGE_ERROR = "usage_error"
    INTERNAL_ERROR = "internal_error"

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self]


_EXIT_CODES = {
    Status.SUCCESS: 0,
    Status.NO_WORK: 10,
    Status.ALREADY_CLAIMED: 11,
    Status.INELIGIBLE: 12,
    Status.MAIN_MOVED: 13,
    Status.NEEDS_ACTION: 14,
    Status.ON_HOLD: 15,
    Status.NETWORK_ERROR: 20,
    Status.TRELLO_ERROR: 21,
    Status.NEEDS_RECONCILIATION: 30,
    Status.NOT_OWNER: 31,
    Status.CONFIG_ERROR: 40,
    Status.USAGE_ERROR: 41,
    Status.INTERNAL_ERROR: 50,
}


class KankanError(Exception):
    """An error with a spec status; ``data`` is optional structured detail."""

    def __init__(self, status: Status, message: str, data: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data


def envelope(err_or_data: Any, message: str = "") -> dict:
    """Build the fixed JSON envelope from a KankanError or from success data."""
    if isinstance(err_or_data, KankanError):
        return {"ok": False, "status": err_or_data.status.value,
                "message": err_or_data.message, "data": err_or_data.data}
    return {"ok": True, "status": Status.SUCCESS.value, "message": message, "data": err_or_data}
