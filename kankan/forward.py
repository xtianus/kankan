"""Forwarding the user's Trello answers to a waiting agent (spec §14.3)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from kankan import clock, faults, labels, messages
from kankan.boardreader import LIVE, max_age
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


ACTION_ID = re.compile(r"^[0-9a-f]{24}$")


def _cursor_date(project, card_id: str, action_id: str) -> str:
    """A cursor stored as an action ID by an earlier version: that comment's date, or the start of the second in its
    ID when the comment is gone (board reader T§7)."""
    found = next((c for c in project.board.thread(card_id, LIVE) if c.id == action_id), None)
    if found is not None and found.date:
        return found.date
    return datetime.fromtimestamp(int(action_id[:8], 16), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def poll(launcher) -> None:
    """Read new comments; keep the configured member's, never kankan's own; queue them."""
    project, seat, card_id = launcher.project, launcher.lease.seat, launcher.task["card"]
    state = load_state(project.paths, seat)
    pending = state.pending_forward
    if pending and pending.get("card") == card_id and pending.get("task_id") == launcher.task["id"]:
        return
    cursor = state.comment_cursor.get(card_id) or ""
    try:
        if ACTION_ID.match(cursor):
            cursor = _cursor_date(project, card_id, cursor)
        new, newest = project.board.new_comments(card_id, cursor, max_age(launcher.cfg.poll_seconds))
    except KankanError as err:
        launcher.log("comments_failed", error=err.message)
        return
    if not new:
        read_cursor = state.comment_cursor.get(card_id)
        if newest != read_cursor:  # a quiet card follows the log (board reader T§7)
            def follow(st):
                if st.comment_cursor.get(card_id) == read_cursor:  # never over a cursor saved meanwhile (an ask)
                    st.comment_cursor[card_id] = newest
            update_state(project.paths, seat, follow)
        return
    from kankan import notify
    wanted = notify.answers(project, new, max_age(launcher.cfg.poll_seconds))

    def save(st):
        st.comment_cursor[card_id] = newest
        if wanted:
            st.pending_forward = {"card": card_id, "task_id": launcher.task["id"],
                                  "text": "\n\n".join(c.text for c in wanted), "status": "queued", "cursor": newest}
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
        from kankan.launcher import poll_done
        poll_done(launcher, "comments")
    if launcher.suspend_pending():
        # HOLD or a user move while waiting: the suspension request goes first (§9.4, §14.6)
        if launcher.can_write(cls, queue=True):
            launcher.paste(messages.SUSPEND_REQUEST)
            launcher.clear_suspend_request()
            update_state(launcher.project.paths, launcher.lease.seat, lambda s: setattr(s, "pending_forward", None))
            _clear_waiting_label(launcher)
            launcher.ready_since = None
            launcher.set_state("suspending")
        return
    state = load_state(launcher.project.paths, launcher.lease.seat)
    pending = state.pending_forward
    if pending and (pending.get("card") != launcher.task["card"] or pending.get("task_id") != launcher.task["id"]):
        launcher.log("forward_dropped_stale", card=pending.get("card"))
        update_state(launcher.project.paths, launcher.lease.seat, lambda s: setattr(s, "pending_forward", None))
        pending = None
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
    if cls.state == "ready":
        launcher.waiting_saw_ready = True
    # an answer typed in the terminal: the agent stopped (ready) after asking and now works again; the "working"
    # right after `kankan ask` is the agent finishing its own turn
    if cls.state == "working" and getattr(launcher, "waiting_saw_ready", False):
        _clear_waiting_label(launcher)
        try:
            launcher.project.trello.add_comment(launcher.task["card"], messages.RESUMED,
                                                f"resumed-{launcher.task['id']}-{int(clock.monotonic())}")
        except KankanError as err:
            launcher.log("comment_failed", error=err.message)
        launcher.log("resumed_from_terminal", card=launcher.task["card"])
        launcher.ready_since = None
        launcher.set_state("working")
