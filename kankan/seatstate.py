"""Per-seat state ``<gcd>/kankan/seats/<seat>.state.json``.

The only writer is ``update_state``: it holds ``<seat>.state.lock`` around a fresh
read, the caller's change and an atomic save.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, TypeVar

from kankan.config import Paths, atomic_write_json, file_lock, read_json

T = TypeVar("T")


@dataclass
class SeatState:
    task: Optional[dict] = None
    running_selection: Optional[dict] = None
    planned_restart: Optional[dict] = None
    launcher_state: str = "stopped"
    convergence_failures: Dict[str, int] = field(default_factory=dict)
    pending_forward: Optional[dict] = None
    block_journal: Optional[dict] = None
    signal: Optional[dict] = None
    label_causes: Dict[str, dict] = field(default_factory=dict)
    comment_cursor: Dict[str, str] = field(default_factory=dict)
    verifications: Dict[str, dict] = field(default_factory=dict)
    stop_requested: bool = False
    main_moved_count: int = 0
    suspend_requested: Optional[dict] = None
    notes: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "SeatState":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def _path(paths: Paths, seat: str):
    return paths.seats_dir / f"{seat}.state.json"


def load_state(paths: Paths, seat: str) -> SeatState:
    return SeatState.from_dict(read_json(_path(paths, seat), {}))


def update_state(paths: Paths, seat: str, fn: Callable[[SeatState], T]) -> T:
    with file_lock(paths.seats_dir / f"{seat}.state.lock", timeout=30, what="seat state lock"):
        state = load_state(paths, seat)
        result = fn(state)
        atomic_write_json(_path(paths, seat), dataclasses.asdict(state))
        return result
