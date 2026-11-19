"""Feedback on invalid agent headings: the project-level record ``card-sections.json`` (card instructions T§3).

No seat holds a card refused for its headings, so the report lives here, not in a seat state. Every Trello write is
a pending action performed by ``drain`` one at a time under the project lock, after rereading the card, as for the
setting cards (``boardsettings.drain_actions``). ``sweep`` only schedules removals; ``drain`` confirms them.
"""

import json
from typing import Optional

from kankan import clock, messages, notify
from kankan.adapters import adapter_types
from kankan.cardsections import parse
from kankan.config import atomic_write_json, project_lock
from kankan.errors import KankanError
from kankan.labels import label_cause
from kankan.seatstate import load_state
from kankan.trello import marker

FILE = "card-sections.json"
MAX_PROBLEMS = 10
HEADING_CHARS = 80


def path(paths):
    return paths.kankan_dir / FILE


def load(paths) -> dict:
    try:
        data = json.loads(path(paths).read_text())
    except (OSError, ValueError):
        return {"cards": {}}
    if not isinstance(data, dict) or not isinstance(data.get("cards"), dict):
        return {"cards": {}}
    return data


def save(paths, data) -> None:
    atomic_write_json(path(paths), data)


def _claimable(project) -> set:
    return {project.cfg.backlog} | {seat.implemented for seat in project.cfg.work_seats().values()}


def _types(project) -> list:
    return adapter_types(project.paths)


def _comment_text(problems, types) -> str:
    shown = []
    for problem in problems[:MAX_PROBLEMS]:
        heading = problem.heading if len(problem.heading) <= HEADING_CHARS else problem.heading[:HEADING_CHARS - 1] + "…"
        shown.append(f"`{heading}` (line {problem.line}): {problem.reason}")
    text = "; ".join(shown)
    if len(problems) > MAX_PROBLEMS:
        text += f"; and {len(problems) - MAX_PROBLEMS} more"
    return messages.SECTIONS_INVALID.format(problems=text, targets=", ".join(sorted(types) + ["all"]))


def _other_cause(project, card_id: str) -> bool:
    """Whether a seat state records a cause for ``Error`` on the card (the ``sections`` cause never is)."""
    return any(label_cause(load_state(project.paths, seat), card_id, project.cfg.label_error)
               for seat in project.cfg.seats)


def record_invalid_locked(project, card, fp: str, problems) -> None:
    """Record an invalid version of ``card``'s description; the caller holds the project lock."""
    data = load(project.paths)
    record = data["cards"].get(card.id)
    if record and record.get("fingerprint") == fp:
        if record.get("state") == "clearing":  # a reported version restored before its removal ran
            record["state"] = "reported"
            record["pending"] = [a for a in record.get("pending", []) if a["kind"] != "remove_error"]
            save(project.paths, data)
        return
    has_error = project.cfg.label_error in card.label_ids
    owned = (not has_error) or bool(record and record.get("owned_error"))
    report = clock.now().strftime("%Y%m%dT%H%M%S%f")
    if record and record.get("report") == report:
        report += "a"
    data["cards"][card.id] = {
        "fingerprint": fp, "report": report, "at": clock.iso(clock.now()), "state": "reported",
        "owned_error": owned,
        "error_write_started": bool(record and record.get("error_write_started")),
        "error_confirmed": bool(record and record.get("error_confirmed")),
        "pending": [{"id": f"sections-{card.id}-{report}", "kind": "comment", "text": _comment_text(list(problems), _types(project))},
                    {"id": f"sections-add-{card.id}-{report}", "kind": "add_error", "text": ""}],
    }
    save(project.paths, data)
    project.log("sections_invalid", card=card.id, fingerprint=fp)


def _remove_owned(project, record, card) -> bool:
    """Remove the ``Error`` kankan owns on ``card`` if no other cause needs it; True when nothing remains to do."""
    if not (record.get("owned_error") and (record.get("error_write_started") or record.get("error_confirmed"))):
        return True
    if project.cfg.label_error not in card.label_ids or _other_cause(project, card.id):
        return True
    project.trello.remove_label(card.id, project.cfg.label_error)
    fresh = project.trello.card(card.id)
    return fresh is None or project.cfg.label_error not in fresh.label_ids


def clear_at_claim_locked(project, card) -> None:
    """A valid card is being claimed: remove kankan's ``Error`` before the ref transition (caller holds the lock)."""
    data = load(project.paths)
    record = data["cards"].get(card.id)
    if record is None:
        return
    try:
        done = _remove_owned(project, record, card)
    except KankanError as err:
        project.log("sections_action_failed", card=card.id, kind="remove_error", error=err.message)
        done = False
    if done:
        data["cards"].pop(card.id, None)
    else:
        record["state"] = "clearing"
        if not any(a["kind"] == "remove_error" for a in record.get("pending", [])):
            record["pending"].append({"id": f"sections-remove-{card.id}-{record['report']}", "kind": "remove_error", "text": ""})
    save(project.paths, data)


def _clear_confirmed(project, card, types) -> bool:
    return card.closed or card.list_id not in _claimable(project) or parse(card.desc, types).valid


def sweep(project, snap) -> None:
    """Schedule the removal for reported cards that the board read shows fixed, moved, archived or deleted."""
    try:
        if not any(r.get("state") == "reported" for r in load(project.paths)["cards"].values()):
            return
        types = _types(project)
        with project_lock(project.paths):
            data = load(project.paths)
            changed = False
            for card_id, record in data["cards"].items():
                if record.get("state") != "reported":
                    continue
                card = snap.cards.get(card_id)
                if card is not None and card.list_id in _claimable(project) and not parse(card.desc, types).valid:
                    continue
                record["state"] = "clearing"
                record["pending"].append({"id": f"sections-remove-{card_id}-{record['report']}", "kind": "remove_error", "text": ""})
                changed = True
            if changed:
                save(project.paths, data)
    except (KankanError, OSError) as err:
        project.log("sections_action_failed", kind="sweep", error=getattr(err, "message", str(err)))


def _perform(project, card_id: str, action_id: str) -> None:
    """One pending action under the project lock; raises KankanError on a Trello failure (the action stays)."""
    with project_lock(project.paths):
        data = load(project.paths)
        record = data["cards"].get(card_id)
        action = next((a for a in (record or {}).get("pending", []) if a["id"] == action_id), None)
        if action is None:
            return
        card = project.trello.card(card_id)
        if card is None:  # deleted
            data["cards"].pop(card_id, None)
            save(project.paths, data)
            return
        removal = next((a for a in record["pending"] if a["kind"] == "remove_error"), None)
        if removal is not None:
            # A pending clear is decided first: confirmed, it drops the stale feedback; cancelled, the feedback goes on.
            if _clear_confirmed(project, card, _types(project)):
                if _remove_owned(project, record, card):
                    data["cards"].pop(card_id, None)
                save(project.paths, data)
                return
            record["pending"].remove(removal)
            record["state"] = "reported"
            save(project.paths, data)
            if action is removal:
                return
        if action["kind"] == "comment":
            me = notify.kankan_member_id(project)
            posted = any(c.member_id == me and c.text.rstrip().endswith(marker(action_id))
                         for c in project.trello.comments(card_id))
            if not posted:
                mention = notify.mention(project, card_id)
                project.trello.add_comment(card_id, f"{action['text']} {mention}".rstrip(), action_id, member_id=me)
        elif action["kind"] == "add_error":
            has_error = project.cfg.label_error in card.label_ids
            if has_error and not (record.get("error_write_started") or record.get("error_confirmed")):
                record["owned_error"] = False  # the label appeared before kankan's first write
            elif has_error:
                record["error_confirmed"] = True
            else:
                record["owned_error"] = True  # kankan performs the POST: the label is its own
                record["error_write_started"] = True
                save(project.paths, data)
                project.trello.add_label(card_id, project.cfg.label_error)
                record["error_confirmed"] = True
        record["pending"] = [a for a in record["pending"] if a["id"] != action_id]
        save(project.paths, data)


def drain(project) -> None:
    """Perform the pending actions; stop at the first failure, which stays pending for the next read. Never raises."""
    try:
        work = [(card_id, action["id"]) for card_id, record in load(project.paths)["cards"].items()
                for action in record.get("pending", [])]
        for card_id, action_id in work:
            _perform(project, card_id, action_id)
    except (KankanError, OSError) as err:
        project.log("sections_action_failed", kind="drain", error=getattr(err, "message", str(err)))
