"""kankan's labels on cards, added and removed one at a time with recorded causes (spec §14.1)."""

from __future__ import annotations

from typing import Dict, Optional

from kankan import clock
from kankan.errors import KankanError
from kankan.seatstate import update_state

RESUME_PREFIX = "Resume "


def _mention(project) -> str:
    return f"@{project.cfg.notify_username}" if project.cfg.notify_username else ""


def set_label(project, card_id: str, label_id: str, cause: str, seat: Optional[str],
              comment: Optional[str] = None, op_id: Optional[str] = None, mention: bool = True) -> None:
    """Add one label, record why, and optionally comment (with a mention of the configured user)."""
    project.trello.add_label(card_id, label_id)
    if seat:
        def record(state):
            state.label_causes.setdefault(card_id, {})[label_id] = {"cause": cause, "at": clock.iso(clock.now())}
        update_state(project.paths, seat, record)
    if comment:
        text = f"{comment} {_mention(project)}".rstrip() if mention else comment
        project.trello.add_comment(card_id, text, op_id or f"label-{label_id}-{cause}-{clock.iso(clock.now())}")
    project.log("label_set", card=card_id, label=label_id, cause=cause, seat=seat)


def label_cause(state, card_id: str, label_id: str) -> Optional[str]:
    entry = state.label_causes.get(card_id, {}).get(label_id)
    return entry["cause"] if entry else None


def clear_label(project, card_id: str, label_id: str, cause: Optional[str], seat: Optional[str]) -> bool:
    """Remove one label if kankan set it for ``cause`` (any cause when None); return True if removed."""
    if seat and cause is not None:
        from kankan.seatstate import load_state
        if label_cause(load_state(project.paths, seat), card_id, label_id) != cause:
            return False
    project.trello.remove_label(card_id, label_id)
    if seat:
        def forget(state):
            state.label_causes.get(card_id, {}).pop(label_id, None)
            if not state.label_causes.get(card_id):
                state.label_causes.pop(card_id, None)
        update_state(project.paths, seat, forget)
    project.log("label_cleared", card=card_id, label=label_id, cause=cause, seat=seat)
    return True


def kankan_label_ids(project, card, board_labels: Optional[Dict] = None):
    """kankan's own labels on ``card``: Question, Error and Resume ones (never HOLD)."""
    if board_labels is None:
        board_labels = project.trello.snapshot(project.cfg.board_id).labels
    own = {project.cfg.label_question, project.cfg.label_error}
    result = []
    for label_id in card.label_ids:
        label = board_labels.get(label_id)
        if label_id in own or (label is not None and label.name.startswith(RESUME_PREFIX)):
            result.append(label_id)
    return result


def clear_kankan_labels(project, card, board_labels: Optional[Dict] = None, seat: Optional[str] = None) -> None:
    """Remove kankan's labels from a card reaching Done; HOLD always stays."""
    for label_id in kankan_label_ids(project, card, board_labels):
        try:
            clear_label(project, card.id, label_id, None, seat)
        except KankanError as err:
            project.log("label_failed", card=card.id, error=err.message)
