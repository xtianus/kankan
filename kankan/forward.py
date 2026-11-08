"""Forwarding the user's Trello answers to a waiting agent (spec §14.3)."""

from __future__ import annotations

from kankan import clock, faults, labels, messages
from kankan.errors import KankanError
from kankan.seatstate import load_state, update_state
from kankan.trello import MARKER_PREFIX


def _clear_waiting_label(launcher) -> None:
    project, seat, card_id = launcher.project, launcher.lease.seat, launcher.task["card"]
    for label_id, cause in ((project.cfg.label_question, "ask"), (project.cfg.label_error, "fail")):
        try:
            labels.clear_label(project, card_id, label_id, cause, seat)
        except KankanError as err:
            launcher.log("label_failed", error=err.message)


def poll(launcher) -> None:
    """Read new comments; keep the configured member's, never kankan's own; queue them."""
    project, seat, card_id = launcher.project, launcher.lease.seat, launcher.task["card"]
    state = load_state(project.paths, seat)
    if state.pending_forward:
        return
    cursor = state.comment_cursor.get(card_id)
    try:
        new = project.trello.comments(card_id, since=cursor)
    except KankanError as err:
        launcher.log("comments_failed", error=err.message)
        return
    if not new:
        return
    wanted = [c for c in new if c.member_id == project.cfg.notify_id and f"`{MARKER_PREFIX}" not in c.text]
    newest = max(c.id for c in new)

    def save(st):
        st.comment_cursor[card_id] = newest
        if wanted:
            st.pending_forward = {"card": card_id, "text": "\n\n".join(c.text for c in wanted), "status": "queued",
                                  "cursor": newest}
    update_state(project.paths, seat, save)
    faults.point("after_cursor_save")


def step(launcher, cls) -> None:
    sig = launcher.take_signal()
    if sig is not None:
        launcher.log("signal_received", kind=sig["kind"])
        if sig["kind"] not in ("ask", "fail"):
            launcher.set_state("closing")
            launcher.closing_since = clock.monotonic()
        return
    now = clock.monotonic()
    if now >= getattr(launcher, "next_comment_poll", 0.0):
        launcher.next_comment_poll = now + launcher.cfg.poll_seconds
        poll(launcher)
    state = load_state(launcher.project.paths, launcher.lease.seat)
    pending = state.pending_forward
    if pending and pending.get("status") == "queued":
        if not launcher.can_write(cls, queue=True):
            return
        update_state(launcher.project.paths, launcher.lease.seat,
                     lambda s: s.pending_forward.__setitem__("status", "sending"))
        faults.point("before_paste")
        launcher.paste(messages.FORWARD_PREFIX + pending["text"])
        update_state(launcher.project.paths, launcher.lease.seat, lambda s: setattr(s, "pending_forward", None))
        _clear_waiting_label(launcher)
        try:
            launcher.project.trello.add_comment(launcher.task["card"], messages.FORWARDED,
                                                f"forwarded-{pending.get('cursor')}")
        except KankanError as err:
            launcher.log("comment_failed", error=err.message)
        launcher.log("forwarded", card=launcher.task["card"])
        launcher.ready_since = None
        launcher.set_state("working")
        return
    if cls.state == "working":
        _clear_waiting_label(launcher)
        try:
            launcher.project.trello.add_comment(launcher.task["card"], messages.RESUMED,
                                                f"resumed-{launcher.task['id']}-{int(clock.monotonic())}")
        except KankanError as err:
            launcher.log("comment_failed", error=err.message)
        launcher.log("resumed_from_terminal", card=launcher.task["card"])
        launcher.ready_since = None
        launcher.set_state("working")
