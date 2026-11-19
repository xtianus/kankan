"""Feedback on invalid agent headings: the project-level record ``card-sections.json`` (card instructions T§3).

No seat holds a card refused for its headings, so the report lives here, not in a seat state. Every Trello write is
a pending action performed by ``drain``: the reads it needs (the card, its comments, the mention) happen first,
without the project lock; then, under the lock, the action is checked to be still pending for the same report and
its one write is made and acknowledged. ``sweep`` only schedules removals; ``drain`` confirms them on the reread card.
"""

import json
import re
from typing import Optional

from kankan import boardconfig, clock, messages, notify
from kankan.adapters import adapter_types
from kankan.cardsections import fingerprint, parse
from kankan.config import atomic_write_json, project_lock
from kankan.errors import KankanError, Status
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
    data["cards"] = {card_id: record for card_id, record in data["cards"].items() if _well_formed(record)}
    return data


def _well_formed(record) -> bool:
    """A record as ``record_invalid_locked`` writes it; anything else is dropped at load (it was never valid)."""
    if not isinstance(record, dict) or not isinstance(record.get("report"), str) or not isinstance(record.get("at"), str):
        return False
    if not (record.get("fingerprint") is None or isinstance(record.get("fingerprint"), str)):
        return False
    if record.get("state") not in ("reported", "clearing") or not isinstance(record.get("pending"), list):
        return False
    if not all(isinstance(record.get(flag, False), bool) for flag in ("owned_error", "error_write_started", "error_confirmed")):
        return False
    return all(isinstance(a, dict) and isinstance(a.get("id"), str) and a.get("kind") in ("comment", "add_error", "remove_error")
               and isinstance(a.get("text", ""), str) for a in record["pending"])


def save(paths, data) -> None:
    atomic_write_json(path(paths), data)


def _claimable(project) -> set:
    return {project.cfg.backlog} | {seat.implemented for seat in project.cfg.work_seats().values()}


def _types(project) -> list:
    return adapter_types(project.paths)


def _code(text: str) -> str:
    """``text`` as a Markdown code span, whatever backticks it holds."""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _comment_text(problems, types) -> str:
    shown = []
    for problem in problems[:MAX_PROBLEMS]:
        heading = problem.heading if len(problem.heading) <= HEADING_CHARS else problem.heading[:HEADING_CHARS - 1] + "…"
        shown.append(f"{_code(heading)} (line {problem.line}): {problem.reason}")
    text = "; ".join(shown)
    if len(problems) > MAX_PROBLEMS:
        text += f"; and {len(problems) - MAX_PROBLEMS} more"
    return messages.SECTIONS_INVALID.format(problems=text, targets=", ".join(sorted(types) + ["all"]))


def _seat_cause(project, card_id: str) -> bool:
    """Whether a seat state records a cause for ``Error`` on the card (the ``sections`` cause never is one)."""
    return any(label_cause(load_state(project.paths, seat), card_id, project.cfg.label_error) for seat in project.cfg.seats)


def _hand_over_to_settings(project, card_id: str) -> bool:
    """A refused card moved into the ``kankan`` list and invalid there: the board settings take over kankan's
    ``Error`` and remove it when the card becomes valid or leaves the list (card instructions T§3, T§10).
    Saves ``board-settings.json`` before the caller drops the record, so a crash in between repeats the handover.
    The caller holds the project lock. True when the label was handed over."""
    state = boardconfig.load(project.paths)
    entry = state.cards.get(card_id)
    if not (entry and entry.get("invalid")):
        return False
    entry.update(owned_error=True, error_confirmed=True, observed_error=True)
    atomic_write_json(project.paths.kankan_dir / "board-settings.json", state.to_dict())
    project.log("sections_error_handed_over", card=card_id)
    return True


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
    if project.cfg.label_error not in card.label_ids or _seat_cause(project, card.id):
        return True
    if _hand_over_to_settings(project, card.id):
        return True
    project.trello.remove_label(card.id, project.cfg.label_error)  # 400/404: the label is not on the card
    return True


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


def _current(project, card_id: str, report: str, action_id: str):
    """Under the lock: the record and action, if the action is still pending for the same report; else (None, None)."""
    data = load(project.paths)
    record = data["cards"].get(card_id)
    if record is None or record.get("report") != report:
        return data, None, None
    action = next((a for a in record["pending"] if a["id"] == action_id), None)
    return data, (record if action else None), action


def _perform(project, card_id: str, action_id: str) -> None:
    """One pending action; raises KankanError on a Trello failure (the action stays pending)."""
    record = load(project.paths)["cards"].get(card_id)
    action = next((a for a in (record or {}).get("pending", []) if a["id"] == action_id), None)
    if action is None:
        return
    report = record["report"]
    card = project.trello.card(card_id)  # every read before the lock
    types = _types(project)
    if card is None:  # deleted
        with project_lock(project.paths):
            data, current, _ = _current(project, card_id, report, action_id)
            if current is not None:
                data["cards"].pop(card_id, None)
                save(project.paths, data)
        return
    if _clear_confirmed(project, card, types):
        # Fixed, moved or archived: any pending feedback is stale; remove kankan's Error and forget the card.
        with project_lock(project.paths):
            data, current, _ = _current(project, card_id, report, action_id)
            if current is not None and _remove_owned(project, current, card):
                data["cards"].pop(card_id, None)
                save(project.paths, data)
        return
    if fingerprint(card.desc, types) != record.get("fingerprint"):
        # Edited to another invalid version before the report went out: drop it, keep the ownership flags,
        # and let the next claim attempt report the current version.
        with project_lock(project.paths):
            data, current, _ = _current(project, card_id, report, action_id)
            if current is not None:
                current.update(fingerprint=None, state="reported", pending=[])
                save(project.paths, data)
        return
    if action["kind"] == "remove_error":  # still invalid at the reported version: the clear is cancelled
        with project_lock(project.paths):
            data, current, _ = _current(project, card_id, report, action_id)
            if current is not None:
                current["pending"] = [a for a in current["pending"] if a["kind"] != "remove_error"]
                current["state"] = "reported"
                save(project.paths, data)
        return
    if action["kind"] == "comment":
        me = notify.kankan_member_id(project)
        posted = any(c.member_id == me and c.text.rstrip().endswith(marker(action_id)) for c in project.trello.comments(card_id))
        mention = "" if posted else notify.mention(project, card_id)
        with project_lock(project.paths):
            data, current, _ = _current(project, card_id, report, action_id)
            if current is None:
                return
            if not posted:
                project.trello.add_comment(card_id, f"{action['text']} {mention}".rstrip(), action_id, member_id=me)
            current["pending"] = [a for a in current["pending"] if a["id"] != action_id]
            save(project.paths, data)
        return
    with project_lock(project.paths):  # add_error
        data, current, _ = _current(project, card_id, report, action_id)
        if current is None:
            return
        has_error = project.cfg.label_error in card.label_ids
        if has_error and not (current["error_write_started"] or current["error_confirmed"]):
            current["owned_error"] = False  # the label appeared before kankan's first write
        elif has_error:
            current["error_confirmed"] = True
        else:
            owned_before = current["owned_error"]
            current.update(owned_error=True, error_write_started=True)  # kankan performs the POST: the label is its own
            save(project.paths, data)
            try:
                project.trello.add_label(card_id, project.cfg.label_error)
            except KankanError as err:
                if err.status == Status.TRELLO_ERROR:  # Trello answered and refused: nothing was written
                    current.update(owned_error=owned_before, error_write_started=False)
                    save(project.paths, data)
                raise
            current["error_confirmed"] = True
        current["pending"] = [a for a in current["pending"] if a["id"] != action_id]
        save(project.paths, data)


MAX_FAILED_CARDS = 2


def drain(project) -> None:
    """Perform the pending actions. A failing card is skipped for the rest of the pass and the others go on; the pass
    ends when Trello does not answer or after ``MAX_FAILED_CARDS`` failed cards. Never raises."""
    try:
        work = [(card_id, action["id"]) for card_id, record in load(project.paths)["cards"].items()
                for action in record["pending"]]
    except OSError as err:
        project.log("sections_action_failed", kind="drain", error=str(err))
        return
    failed = set()
    for card_id, action_id in work:
        if card_id in failed:
            continue
        try:
            _perform(project, card_id, action_id)
        except KankanError as err:
            project.log("sections_action_failed", card=card_id, error=err.message)
            failed.add(card_id)
            if err.status != Status.TRELLO_ERROR or len(failed) >= MAX_FAILED_CARDS:
                return
        except OSError as err:
            project.log("sections_action_failed", card=card_id, error=str(err))
            return
