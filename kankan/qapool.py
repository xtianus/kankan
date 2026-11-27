"""The Q&A pool (qapool spec): the router ``<type>-qa`` and its members ``<type>-qa2``, ``<type>-qa3``, …, the
prompt cache readings and expiry, the answer record, offers, the routing round, opening and closing members, the
stopping mark and the title prefixes."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from kankan import faults, notify, qa
from kankan.boardreader import LIVE
from kankan.cards import Phase
from kankan.config import atomic_write_json, file_lock, read_json
from kankan.errors import KankanError, Status
from kankan.refstore import RefValue, validate_seat


# ---------------------------------------------------------------------- names (T§2)

def member_number(seat: str) -> int:
    """1 for the router ``<type>-qa``, N for a member ``<type>-qaN``."""
    suffix = validate_seat(seat).rsplit("-", 1)[1]
    if not suffix.startswith("qa"):
        raise ValueError(f"{seat} is not a Q&A seat")
    return int(suffix[2:] or 1)


def is_member(seat: str) -> bool:
    """A seat kankan opens and closes (``<type>-qaN``), never the router."""
    suffix = validate_seat(seat).rsplit("-", 1)[1]
    return suffix.startswith("qa") and suffix != "qa"


def router(cfg) -> Optional[str]:
    """The registered router ``<type>-qa``, if any."""
    return cfg.qa_seat()


def members(cfg) -> List[str]:
    """The registered members, by seat number."""
    return sorted((name for name, seat in cfg.seats.items() if seat.role == "qa" and is_member(name)),
                  key=member_number)


def pool_seats(cfg) -> List[str]:
    """The router (when registered) and the members, router first."""
    first = router(cfg)
    return ([first] if first else []) + members(cfg)


def next_member_name(cfg, agent_type: str) -> str:
    """The lowest free ``<type>-qaN`` (N ≥ 2)."""
    taken = {member_number(name) for name in cfg.seats if name.startswith(f"{agent_type}-qa")}
    number = 2
    while number in taken:
        number += 1
    return f"{agent_type}-qa{number}"


# ---------------------------------------------------------------------- prompt cache (T§3)

def lifetime_seconds(project, seat: str) -> float:
    """The estimated cache lifetime of the seat's agent type (``prompt_cache.lifetime_minutes``)."""
    from kankan.adapters import load_adapter
    from kankan.seats import seat_type
    adapter = load_adapter(project.paths, seat_type(seat), read_only=True)
    return float(adapter.prompt_cache["lifetime_minutes"]) * 60


def reading(project, seat: str) -> Optional[dict]:
    """The seat's cache reading when it belongs to its current agent process (same pid and start time) and the
    type's expiry is ``exact``; None otherwise."""
    from kankan.adapters import load_adapter
    from kankan.config import read_json
    from kankan.seats import read_proc, seat_type
    if load_adapter(project.paths, seat_type(seat), read_only=True).prompt_cache.get("expiry") != "exact":
        return None
    try:
        data = read_json(project.paths.seats_dir / f"{seat}.cache.json", None)
    except ValueError:
        return None
    agent = (read_proc(project, seat) or {}).get("agent")
    if not isinstance(data, dict) or not isinstance(agent, dict) or not isinstance(data.get("agent"), dict):
        return None
    if (data["agent"].get("pid"), data["agent"].get("start")) != (agent.get("pid"), agent.get("start")) \
            or agent.get("start") is None:
        return None
    return data


def expiry(project, seat: str) -> Tuple[Optional[float], bool]:
    """``(time, exact)``: the exact expiry reported for the current agent process (a non-null ``expires_at``), else
    the estimate ``turn_ended_at + lifetime``; ``(None, False)`` without a finished turn since the agent started."""
    from kankan.seatstate import load_state
    data = reading(project, seat)
    if data is not None and data.get("expires_at") is not None:
        return float(data["expires_at"]), True
    ended = load_state(project.paths, seat).turn_ended_at
    if ended is None:
        return None, False
    return float(ended) + lifetime_seconds(project, seat), False


def warm(project, seat: str, now: Optional[float] = None) -> bool:
    """The seat's cache expires in the future."""
    when, _ = expiry(project, seat)
    return when is not None and when > (time.time() if now is None else now)


# ---------------------------------------------------------------------- expiry note (T§7)

_ZONE_FILES = ("/etc/timezone", "/etc/localtime")


def zone_name() -> str:
    """The local zone's IANA name from ``TZ``, ``/etc/timezone`` or the target of ``/etc/localtime``, else the
    abbreviation of ``time.strftime('%Z')``."""
    import os
    tz = os.environ.get("TZ", "").lstrip(":")
    if "/" in tz and not tz.startswith("/"):
        return tz
    timezone_file, localtime = _ZONE_FILES
    try:
        with open(timezone_file) as handle:
            name = handle.read().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.readlink(localtime)
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.strftime("%Z")


def note_text(project, seat: str, now: Optional[float] = None) -> str:
    """The note ending an answer: the exact expiry of the seat's current reading when it is in the future, else
    the estimate ``now + lifetime``, as ``HH:MM`` local time with the zone."""
    from kankan import messages
    now = time.time() if now is None else now
    data = reading(project, seat)
    expires = data.get("expires_at") if data else None
    if expires is not None and float(expires) > now:
        when, template = float(expires), messages.QA_CACHE_EXACT
    else:
        when, template = now + lifetime_seconds(project, seat), messages.QA_CACHE_ESTIMATED
    return template.format(time=time.strftime("%H:%M", time.localtime(when)), zone=zone_name())


# ---------------------------------------------------------------------- routing choice (T§5 steps 4–5)

IDLE_STATES = ("idle", "qa_waiting", "closing")  # not answering, not limited, not starting (closing: on its way to idle)


@dataclass
class SeatView:
    """What the choice knows of one seat of the pool, read before the round's locks."""
    name: str
    state: str = "idle"  # the launcher state
    holds: Optional[str] = None  # the card of its conversation
    card_ended: bool = False  # its card's conversation ended (moved, archived, deleted, HOLD): available
    warm: bool = False
    expiry: Optional[float] = None
    pending: bool = False  # a pending answer or handoff not yet settled
    offer: Optional[str] = None  # an outstanding offer
    reserved: bool = False  # it holds a card with inputs: its own polling delivers them
    gone: bool = False
    removing: bool = False  # marked for removal (closing)

    @property
    def number(self) -> int:
        return member_number(self.name)

    @property
    def is_router(self) -> bool:
        return not is_member(self.name)

    def free(self) -> bool:
        """Idle, nothing pending or offered, not reserved, not gone or closing."""
        return self.state in IDLE_STATES and not self.pending and self.offer is None and not self.reserved \
            and not self.gone and not self.removing


@dataclass
class Decision:
    offers: List[Tuple[str, str]] = field(default_factory=list)  # (seat, card)
    openings: List[str] = field(default_factory=list)  # cards given to a new member each
    closings: List[str] = field(default_factory=list)  # members to close (gone ones included)


def choose(needing: List[Tuple[str, str]], seats: List[SeatView], *, stopping: bool = False,
           any_limited: bool = False, opening_failed: bool = False) -> Decision:
    """The round's choice (T§5 step 4–5): ``needing`` lists ``(card, wait key)`` of the cards needing an agent.

    Cards with an outstanding offer are skipped; the others, oldest wait key first, go to the available seat with
    the lowest number (free, and cold or with an ended card), else to a new member (never while a seat is limited
    or the pool is stopping), else, when an opening failed in the previous round, to the free warm seat whose cache
    expires soonest; otherwise they wait. Closing: when a card was searched for, every other free cold member; in
    every round the members whose card ended and got none, and the gone members. The router is never closed."""
    decision = Decision()
    offered = {s.offer for s in seats if s.offer is not None}
    taken = set()
    cards = [] if stopping else sorted((item for item in needing if item[0] not in offered),
                                       key=lambda item: (item[1], item[0]))  # a stopping pool takes no card
    by_number = sorted(seats, key=lambda s: s.number)
    for card, _ in cards:
        candidates = [s for s in by_number if s.name not in taken and s.free() and (not s.warm or s.card_ended)]
        if candidates:
            decision.offers.append((candidates[0].name, card))
            taken.add(candidates[0].name)
            continue
        if not stopping and not any_limited and not opening_failed:
            decision.openings.append(card)
            continue
        if opening_failed and not stopping:
            warm_seats = sorted((s for s in seats if s.name not in taken and s.free() and s.warm),
                                key=lambda s: (s.expiry if s.expiry is not None else float("inf"), s.number))
            if warm_seats:
                decision.offers.append((warm_seats[0].name, card))
                taken.add(warm_seats[0].name)
    for s in by_number:
        if s.is_router or s.name in taken:
            continue
        if s.gone or (s.card_ended and s.state in IDLE_STATES and s.offer is None and not s.pending):
            decision.closings.append(s.name)
        elif cards and s.free() and not s.warm:
            decision.closings.append(s.name)
    return decision


# ---------------------------------------------------------------------- locks and offers (T§4)

def pool_lock(paths):
    """The pool lock (``qa-pool.lock``), after the Q&A lifecycle lock and before the conversation lock; held only to
    read or rewrite the pool's files, never across a Trello request, a process start or a seat removal."""
    from kankan.config import file_lock
    return file_lock(paths.kankan_dir / "qa-pool.lock", timeout=60, what="Q&A pool lock")


def _offer_of(project, seat: str) -> Optional[dict]:
    from kankan.seatstate import load_state
    return load_state(project.paths, seat).qa_offer


def outstanding(project) -> dict:
    """``{card: seat}`` of the offers named in the registered pool seats' states."""
    result = {}
    for seat in pool_seats(project.reload_config()):
        offer = _offer_of(project, seat)
        if offer and offer.get("card"):
            result[offer["card"]] = seat
    return result


def write_offer(project, seat: str, card_id: str) -> bool:
    """Offer ``card_id`` to ``seat`` (pool lock, then the seat-state lock): False when the card already has an
    outstanding offer or the seat one for another card. The caller holds the lifecycle lock when it routes."""
    import uuid
    from kankan import clock, faults
    from kankan.seatstate import update_state
    with pool_lock(project.paths):
        current = outstanding(project)
        if card_id in current or _offer_of(project, seat):
            return False

        def put(state):
            state.qa_offer = {"card": card_id, "offer_id": uuid.uuid4().hex[:12], "at": clock.iso(clock.now())}
        update_state(project.paths, seat, put)
        project.log("qa_offer", seat=seat, card=card_id)
        faults.point("after_offer_written")
    return True


def clear_offer(project, seat: str, offer_id: Optional[str] = None) -> None:
    """Remove the seat's offer (only ``offer_id`` when given)."""
    from kankan.seatstate import update_state

    def clear(state):
        if state.qa_offer and (offer_id is None or state.qa_offer.get("offer_id") == offer_id):
            state.qa_offer = None
    update_state(project.paths, seat, clear)


# ---------------------------------------------------------------------- title prefixes (T§8)


def _prefixes_path(paths) -> Path:
    return paths.kankan_dir / "title-prefixes.json"


def recorded(paths) -> Dict[str, str]:
    """The prefix record: card ID → the prefix kankan wrote before that card's title."""
    data = read_json(_prefixes_path(paths), {})
    if not isinstance(data, dict):
        return {}
    return {card_id: prefix for card_id, prefix in data.items() if isinstance(prefix, str) and prefix}


def _record(paths, card_id: str, prefix: Optional[str]) -> None:
    data = recorded(paths)
    if prefix:
        data[card_id] = prefix
    else:
        data.pop(card_id, None)
    atomic_write_json(_prefixes_path(paths), data)


def prefix_value(cfg, kind: str) -> str:
    """The configured characters of a ``warm`` or ``cold`` prefix."""
    if kind == "warm":
        return cfg.qa_prefix_warm
    if kind == "cold":
        return cfg.qa_prefix_cold
    raise ValueError(f"unknown prefix kind {kind!r}")


def desired_prefix(held: bool, warm: bool, ended_in_qa: bool, in_qa: bool) -> Optional[str]:
    """The prefix a card should carry (T§8 "Desired prefix"): none once it left ``Q&A`` (or is archived); ``warm``
    or ``cold`` while a seat of the pool holds it, by that seat's cache; ``cold`` once its conversation ended by a
    switch or a closing; none when it was never held."""
    if not in_qa:
        return None
    if held:
        return "warm" if warm else "cold"
    return "cold" if ended_in_qa else None


def _base(name: str, prefix: Optional[str]) -> Optional[str]:
    """The title without ``prefix`` and its space, or ``None`` when the title does not start with them."""
    if prefix and name.startswith(prefix + " "):
        return name[len(prefix) + 1:]
    return None


def write_prefix(project, card_id: str, desired: Optional[str]) -> str:
    """Give the card the ``desired`` prefix (``"warm"``, ``"cold"`` or ``None``). Returns ``"unchanged"`` when the
    record already holds it (the card is neither read nor written: a user's edit stays until the next change of
    state), ``"written"`` after a title write, ``"kept"`` when the live title needed no write, ``"deleted"`` when the
    card is gone (its record dropped). A title that no longer starts with the recorded prefix and a space was edited
    by the user: the record is dropped and the whole title is the base. The record is updated only after the write
    is confirmed; a crash in between leaves a prefix the next write treats as the user's text."""
    paths = project.paths
    wanted = prefix_value(project.cfg, desired) if desired else None
    old = recorded(paths).get(card_id)
    if old == wanted:
        return "unchanged"
    card = project.board.card(card_id, LIVE)
    if card is None:
        _record(paths, card_id, None)
        return "deleted"
    base = _base(card.name, old)
    if base is None:
        base = card.name  # never written, or edited by the user: the title is theirs
    name = f"{wanted} {base}" if wanted else base
    outcome = "kept"
    if name != card.name:
        project.trello.set_card_name(card_id, name)
        faults.point("after_prefix_write")
        outcome = "written"
    _record(paths, card_id, wanted)
    return outcome


def _write_each(project, targets: Dict[str, Optional[str]]) -> Dict[str, str]:
    results = {}
    for card_id, desired in targets.items():
        try:
            results[card_id] = write_prefix(project, card_id, desired)
        except KankanError as err:  # the record keeps the card: a later call retries it
            project.log("qa_prefix_failed", card=card_id, desired=desired, error=err.message)
            results[card_id] = "failed"
    return results


def clear_all(project) -> Dict[str, str]:
    """Remove every recorded prefix (router start, T§9): per card what ``write_prefix`` did, or ``"failed"``."""
    return _write_each(project, {card_id: None for card_id in recorded(project.paths)})


def cold_all(project) -> Dict[str, str]:
    """Write the cold prefix on every card recorded warm (graceful stop of the router)."""
    warm = prefix_value(project.cfg, "warm")
    return _write_each(project, {card_id: "cold" for card_id, prefix in recorded(project.paths).items()
                                 if prefix == warm})


def plain_title(paths, card) -> str:
    """The card's title without the prefix kankan recorded for it; any other title unchanged."""
    base = _base(card.name, recorded(paths).get(card.id))
    return card.name if base is None else base

# ---------------------------------------------------------------------- answer record (T§5 step 3)
#
# ``<gcd>/kankan/qa-answered.json``: ``{"cards": {<card ID>: entry}}``, each entry
#   ``delivered``: ``{"description": bool, "comment": <date of the newest comment given> | None,
#                   "attachments": [IDs]}`` — what turns that ended were given;
#   ``answered``: ``{"id", "date"}`` of the newest complete answer comment, or None;
#   ``checked``: the comment date up to which the card's comments are known (None before any);
#   ``inputs``: ``[{"id", "kind": "comment" | "attachment", "date"}]`` found and not yet delivered.
# Rewritten atomically under the answer-record lock, which is held only to read and rewrite the file (never across
# a Trello request) and taken after every other lock but the board-cache locks (T§4).

RECORD_LOCK_TIMEOUT = 60


def _record_path(paths):
    return paths.kankan_dir / "qa-answered.json"


def record_lock(paths):
    """The answer-record lock (``qa-answered.lock``)."""
    return file_lock(paths.kankan_dir / "qa-answered.lock", timeout=RECORD_LOCK_TIMEOUT, what="answer-record lock")


def blank_entry() -> dict:
    """A card never delivered: nothing given, nothing answered, nothing known."""
    return {"delivered": {"description": False, "comment": None, "attachments": []}, "answered": None,
            "checked": None, "inputs": []}


def load_record(paths) -> Dict[str, dict]:
    """The entries by card ID (none when the file is missing or unreadable)."""
    data = read_json(_record_path(paths), None)
    cards = data.get("cards") if isinstance(data, dict) else None
    return cards if isinstance(cards, dict) else {}


def _change(paths, change: Callable[[Dict[str, dict]], bool]) -> Dict[str, dict]:
    """Under the answer-record lock: ``change`` edits the entries in place and returns whether it changed any; the
    file is then rewritten in one atomic write. Returns the entries."""
    with record_lock(paths):
        cards = load_record(paths)
        if change(cards):
            atomic_write_json(_record_path(paths), {"cards": cards})
        return cards


def _later(date: Optional[str], floor: Optional[str]) -> bool:
    """``date`` is after ``floor`` by Trello's dates: everything is after no floor, an unreadable date counts as
    after (as in ``qa.first_turn``)."""
    if not floor:
        return True
    when, limit = qa._when(date or ""), qa._when(floor)
    return when is None or limit is None or when > limit


def _max_date(dates: Iterable[Optional[str]]) -> Optional[str]:
    readable = [d for d in dates if d and qa._when(d) is not None]
    return max(readable, key=qa._when) if readable else None


def seed(project, card) -> dict:
    """The entry of a card not in the record, from one live read of its thread: ``answered`` is the newest complete
    answer (an answer or handoff comment by kankan's account that ``qa._complete`` accepts), ``delivered`` the
    description, that answer and the attachments dated before it, ``checked`` the newest comment date, ``inputs`` the
    candidates after that answer; without a complete answer, never delivered and every candidate is an input. A card
    already in the record keeps its entry. ``card`` carries ``attachment_items``."""
    comments = project.board.thread(card.id, LIVE)
    cands = qa.candidates(project, card, comments)
    me = notify.kankan_member_id(project)
    answers = [c for c in comments if c.member_id == me and qa.QA_MARKER.search(c.text) and qa._complete(c)]
    latest = max(answers, key=qa.by_date) if answers else None
    latest_when = qa._when(latest.date) if latest is not None else None
    entry = blank_entry()
    by_id = {c.id: c for c in comments}
    attachments = {a.id: a for a in card.attachment_items}

    def new(comment) -> bool:
        when = qa._when(comment.date)
        return latest_when is None or when is None \
            or (when, len(comment.id), comment.id) > (latest_when, len(latest.id), latest.id)
    if latest_when is not None:
        entry["answered"] = {"id": latest.id, "date": latest.date}
        entry["delivered"] = {"description": True, "comment": latest.date, "attachments": sorted(
            a.id for a in card.attachment_items if qa._when(a.date) is not None and qa._when(a.date) <= latest_when)}
    delivered = set(entry["delivered"]["attachments"])
    entry["inputs"] = [{"id": c, "kind": "comment", "date": by_id[c].date} for c in cands.comments if new(by_id[c])] \
        + [{"id": a, "kind": "attachment", "date": attachments[a].date} for a in cands.attachments if a not in delivered]
    entry["checked"] = _max_date(c.date for c in comments)

    def store(cards) -> bool:
        if card.id in cards:
            return False
        cards[card.id] = entry
        return True
    stored = _change(project.paths, store)[card.id]
    if stored is entry:
        project.log("qa_record_seeded", card=card.id, answered=entry["answered"], inputs=len(entry["inputs"]))
    return stored


def observe(project, card_id: str, comments: list, point: Optional[str], attachments: list,
            fresh=None) -> Optional[dict]:
    """A routing round's findings for a card of the record: the comments by input members dated after
    ``delivered.comment`` and the attachments (the card's ``attachment_items``) not in ``delivered.attachments`` are
    listed in ``inputs`` (once each), and ``checked`` becomes ``point``, in one atomic write. ``fresh`` is passed to
    ``notify.input_members``, read before the lock. None when the card is not in the record (``seed`` first)."""
    members = notify.input_members(project, fresh)
    found_comments = qa.input_comments(comments, members)
    found_attachments = qa.input_attachments(attachments, members)
    result = {}

    def change(cards) -> bool:
        entry = cards.get(card_id)
        if entry is None:
            return False
        delivered = entry["delivered"]
        listed = {item["id"] for item in entry["inputs"]}
        for comment in found_comments:
            if comment.id not in listed and _later(comment.date, delivered.get("comment")):
                entry["inputs"].append({"id": comment.id, "kind": "comment", "date": comment.date})
                listed.add(comment.id)
        seen = set(delivered.get("attachments") or [])
        for attachment in found_attachments:
            if attachment.id not in listed and attachment.id not in seen:
                entry["inputs"].append({"id": attachment.id, "kind": "attachment", "date": attachment.date})
                listed.add(attachment.id)
        entry["checked"] = point
        result["entry"] = entry
        return True
    _change(project.paths, change)
    return result.get("entry")


def turn_ended(project, card_id: str, given: "qa.Inputs", answered: Optional[dict] = None) -> dict:
    """A turn on the card ended (a signal accepted, or the agent ready again without one), or ``qa_answer`` completed
    an answer: exactly the ``given`` inputs leave ``inputs``; ``delivered`` gets the description, the newest given
    comment by date (from ``given.dates``, else the date of its input entry; it never moves back) and the given
    attachments. ``answered`` (``{"id", "date"}``) replaces an older one. A card not in the record gets an entry.
    Idempotent, so a completion and the launcher's turn end may both report the same turn."""
    given_ids = set(given.comments) | set(given.attachments)
    result = {}

    def change(cards) -> bool:
        entry = cards.setdefault(card_id, blank_entry())
        listed = {item["id"]: item.get("date") for item in entry["inputs"]}
        delivered = entry["delivered"]
        newest = _max_date([delivered.get("comment")] + [given.dates.get(c) or listed.get(c) for c in given.comments])
        delivered["comment"] = newest if newest is not None else delivered.get("comment")
        delivered["attachments"] = sorted(set(delivered.get("attachments") or []) | set(given.attachments))
        delivered["description"] = True
        entry["inputs"] = [item for item in entry["inputs"] if item["id"] not in given_ids]
        if answered is not None:
            old = entry.get("answered")
            if old is None or not old.get("date") or answered.get("id") == old.get("id") \
                    or _later(answered.get("date"), old["date"]):
                entry["answered"] = dict(answered)
        result["entry"] = entry
        return True
    _change(project.paths, change)
    return result["entry"]


def reconcile(project, card_id: str, listed_before: Iterable[str], thread_comment_ids: Iterable[str],
              card_attachment_ids: Iterable[str], context_ids: Iterable[str] = ()) -> List[str]:
    """Before a pool seat prompts a turn on the card, with its live reads: the entries listed before the reads were
    sent (``listed_before``, IDs snapshotted then) that the reads no longer have (a comment missing from the thread,
    an attachment missing from the card) are dropped, and so are the entries a first turn counts as context
    (``context_ids``). Entries listed after the reads stay. Returns the dropped IDs."""
    before, thread = set(listed_before), set(thread_comment_ids)
    attached, context = set(card_attachment_ids), set(context_ids)
    dropped: List[str] = []

    def gone(item) -> bool:
        if item["id"] in context:
            return True
        if item["id"] not in before:
            return False
        return item["id"] not in (thread if item.get("kind") == "comment" else attached)

    def change(cards) -> bool:
        entry = cards.get(card_id)
        if entry is None:
            return False
        dropped.extend(item["id"] for item in entry["inputs"] if gone(item))
        # attachments are tracked by ID: a context attachment counts as delivered, or the next round lists it again
        delivered = entry["delivered"]
        given = [item["id"] for item in entry["inputs"] if item["id"] in context and item.get("kind") == "attachment"]
        if given:
            delivered["attachments"] = sorted(set(delivered.get("attachments") or []) | set(given))
        entry["inputs"] = [item for item in entry["inputs"] if not gone(item)]
        return bool(dropped)
    _change(project.paths, change)
    if dropped:
        project.log("qa_record_reconciled", card=card_id, dropped=dropped)
    return dropped


def _parked_handoff(value) -> bool:
    return isinstance(value, RefValue) and value.meta.get("phase") == Phase.PARKED.value \
        and value.meta.get("cause", "handoff") == "handoff" and bool(value.meta.get("handoff"))


def needs_agent(entry: Optional[dict], ref_value, holder_alive: bool) -> bool:
    """A card of ``Q&A`` without ``HOLD`` (the caller's check) whose ``qa`` ref names no alive seat of the pool
    (``holder_alive`` false) needs an agent when it has inputs, or when its description was never delivered and it
    has no ``parked`` handoff ref (a new card: its description is the question). A card not in the record counts as
    never delivered."""
    if holder_alive:
        return False
    entry = entry or blank_entry()
    if entry.get("inputs"):
        return True
    return not entry["delivered"].get("description") and not _parked_handoff(ref_value)


def created(card_id: str) -> str:
    """A card's creation time from the first 8 hex digits of its ID (Unix seconds), as a Trello date."""
    when = datetime.fromtimestamp(int(card_id[:8], 16), timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def wait_key(card_id: str, entry: Optional[dict]) -> str:
    """The routing order of a card needing an agent: the date of its oldest input, or the card's creation."""
    oldest = [item["date"] for item in (entry or {}).get("inputs") or []
              if item.get("date") and qa._when(item["date"]) is not None]
    if not ((entry or {}).get("delivered") or {}).get("description"):
        oldest.append(created(card_id))  # the description, never answered, waits since the card's creation
    return min(oldest, key=qa._when) if oldest else created(card_id)


def drop(project, card_ids: Iterable[str]) -> List[str]:
    """Remove the entries of ``card_ids`` (``reconcile --fix``: archived or deleted cards). Returns those removed."""
    wanted = set(card_ids)
    removed: List[str] = []

    def change(cards) -> bool:
        for card_id in sorted(wanted & set(cards)):
            del cards[card_id]
            removed.append(card_id)
        return bool(removed)
    _change(project.paths, change)
    return removed

# ---------------------------------------------------------------------- gone members (T§5 step 2)

# Margin added to the adapter's start and model-check timeouts for the startup deadline of an opening: the tmux
# session, the supervisor, the launcher's board read and its seat occupation come before the agent's own start.
STARTUP_MARGIN_SECONDS = 60


def startup_deadline_seconds(project, seat: str) -> float:
    """How long after its opening a member may still be ``free`` (its launcher has not occupied the seat yet): the
    adapter's ``start_timeout_seconds`` and ``model_check_timeout_seconds`` plus ``STARTUP_MARGIN_SECONDS``."""
    from kankan.adapters import load_adapter
    from kankan.seats import seat_type
    adapter = load_adapter(project.paths, seat_type(seat), read_only=True)
    return adapter.start_timeout_seconds + adapter.model_check_timeout_seconds + STARTUP_MARGIN_SECONDS


def gone(project, seat: str, opened_at: Optional[float] = None, now: Optional[float] = None) -> bool:
    """A member that will not come back by itself (T§5 step 2): its seat is ``dead`` and its supervisor does not run
    (exited, gave up, or of another boot), or it is ``free`` past the startup deadline of its opening.

    ``opened_at`` and ``now`` are wall-clock times (``time.time()``), so the opening time can be persisted (an
    offer's ``at``). Without ``opened_at`` no opening is in progress and a ``free`` member is gone at once (the
    router's start, a member whose launcher exited). ``alive`` and ``unavailable`` members are never gone. The
    caller marks a gone member with ``mark_gone``, which checks again under the project lock."""
    from kankan import seats
    state = seats.liveness(project, seat)
    if state == "dead":
        return not seats.supervisor_running(project, seat)
    if state == "free":
        if seats.supervisor_running(project, seat):
            return False  # its launcher released the seat and its supervisor is about to restart it
        if opened_at is None:
            return True
        now = time.time() if now is None else now
        return now - opened_at > startup_deadline_seconds(project, seat)
    return False


def mark_gone(project, seat: str, opened_at: Optional[float] = None, now: Optional[float] = None) -> bool:
    """Mark a gone member for graceful removal (``removing: graceful``, the closing mark of T§6) after checking again,
    under the project lock, that it is a registered member, not marked yet and gone. Returns whether it marked."""
    from kankan.config import project_lock, save_config
    with project_lock(project.paths):
        cfg = project.reload_config()
        seat_cfg = cfg.seats.get(seat)
        if seat_cfg is None or seat_cfg.role != "qa" or not is_member(seat) or seat_cfg.removing:
            return False
        if not gone(project, seat, opened_at, now):
            return False
        seat_cfg.removing = "graceful"
        save_config(project.paths, cfg)
        project.reload_config()
    project.log("qa_member_gone", seat=seat)
    return True


# ---------------------------------------------------------------------- stopping mark (T§9)

def _pool_path(paths) -> Path:
    return paths.kankan_dir / "qa-pool.json"



def stopping(paths) -> bool:
    """The stopping mark is set: neither the router's nor a member's launcher starts, and no member opens. A lock-free
    read of the atomically written file (launchers check it under the project lock, which comes after the pool
    lock); an unreadable mark counts as set (a start command rewrites it)."""
    from kankan.config import read_json
    try:
        data = read_json(_pool_path(paths), {})
    except ValueError:
        return True
    return not isinstance(data, dict) or data.get("stopping") is True


def set_stopping(project, value: bool) -> None:
    """Write the stopping mark under the Q&A lifecycle lock, then the pool lock (T§4)."""
    from kankan import qa
    from kankan.config import atomic_write_json
    with qa.lifecycle_lock(project.paths), pool_lock(project.paths):
        atomic_write_json(_pool_path(project.paths), {"stopping": bool(value)})
    project.log("qa_pool_stopping", stopping=bool(value))


def clear_for_start(project) -> None:
    """Clear the stopping mark before a start command starts the router (``kankan start``, ``kankan up``,
    ``kankan agent <type> --qa``, ``kankan seat start <type>-qa``); nothing is written when it is not set."""
    if stopping(project.paths):
        set_stopping(project, False)


# ---------------------------------------------------------------------- routing round (T§5, T§6)

def _owned_pending(project, seat: str, refs) -> bool:
    """A conversation of ``seat`` has a pending answer or handoff."""
    for value in refs.values():
        meta = value.meta
        if meta.get("phase") == Phase.QA.value and meta.get("owner") == seat:
            conversation = meta.get("conversation")
            if qa.valid_conversation(conversation) \
                    and (qa.conversation_dir(project.paths, conversation) / "pending.json").exists():
                return True
    return False


def _held(refs, seat: str) -> Optional[str]:
    return next((value.meta.get("card") for value in refs.values()
                 if value.meta.get("phase") == Phase.QA.value and value.meta.get("owner") == seat), None)


class Router:
    """The routing round of the router's launcher (T§5): reads, ended cards and gone members, the answer record, the
    choice under the lifecycle and pool locks, then openings, closings and removals after them, then the prefixes.
    It keeps what must outlive one round: the openings' times and whether the last opening failed."""

    def __init__(self, project, seat: str, log: Callable[..., None]):
        self.project = project
        self.seat = seat
        self.log = log
        self.opened: Dict[str, float] = {}  # member -> wall time of its opening
        self.opening_failed = False
        self.removals: Dict[str, object] = {}  # member -> Popen of its background removal

    # ---------------------------------------------------------------- one round
    def round(self) -> None:
        from kankan import workflow
        from kankan.boardreader import max_age
        from kankan.seats import liveness
        from kankan.seatstate import load_state
        project = self.project
        cfg = project.reload_config()
        fresh = max_age(cfg.qa_poll_seconds)
        snap = project.board.snapshot(fresh)
        qa_cards = {c.id: c for c in snap.cards.values() if c.list_id == cfg.qa_list and not c.closed} \
            if cfg.qa_list else {}
        for card in qa_cards.values():  # step 3: the answer record of every card in Q&A
            record = load_record(project.paths)
            if card.id not in record:
                seed(project, card)
                continue
            comments, point = project.board.new_comments(card.id, record[card.id].get("checked") or "", fresh)
            observe(project, card.id, comments, point, card.attachment_items, fresh=fresh)
        record = load_record(project.paths)
        refs = workflow.card_refs(project)
        open_cards = {cid for cid, c in qa_cards.items() if cfg.label_hold not in c.label_ids}
        now = time.time()
        views, alive = [], set()
        for name in pool_seats(cfg):
            state = load_state(project.paths, name)
            live = liveness(project, name)
            if live == "alive":
                alive.add(name)
            holds = _held(refs, name)
            when, _ = expiry(project, name)
            member = is_member(name)
            seat_gone = member and live != "alive" and gone(project, name, self.opened.get(name), now)
            views.append(SeatView(
                name=name, state=state.launcher_state if live == "alive" else "stopped", holds=holds,
                card_ended=holds is None or holds not in open_cards, warm=when is not None and when > now,
                expiry=when, pending=_owned_pending(project, name, refs),
                offer=(state.qa_offer or {}).get("card"),
                reserved=bool(holds and holds in open_cards and (record.get(holds) or {}).get("inputs")),
                gone=seat_gone, removing=bool(cfg.seats[name].removing)))
        self.check_openings(views, now)
        needing = []
        malformed = {ref.rsplit("/", 1)[1] for ref, value in qa.parse_refs(project).items()
                     if not isinstance(value, RefValue)}
        for card_id in sorted(open_cards):
            value = refs.get(card_id)
            if card_id in malformed:
                continue  # reconcile reports it; no seat could claim it
            if value is not None and value.meta.get("phase") != Phase.QA.value and not qa.claimable(value):
                continue  # an implementation ref, or a handoff whose move is outstanding: never claimable
            holder = value.meta.get("owner") if value is not None and value.meta.get("phase") == Phase.QA.value \
                else None
            entry = record.get(card_id)
            if needs_agent(entry, value, holder_alive=holder in alive):
                needing.append((card_id, wait_key(card_id, entry)))
        decision = self.decide(needing, views)
        self.after_locks(decision, views)
        self.prefixes(qa_cards, open_cards, refs, views, now)

    # ---------------------------------------------------------------- choice (step 4)
    def decide(self, needing, views) -> Decision:
        """Under the lifecycle lock and the pool lock: the choice, the members' registrations and the offers; no
        Trello request and no process start while they are held."""
        from kankan.config import SeatConfig, project_lock, save_config
        from kankan.seats import seat_type
        project = self.project
        with qa.lifecycle_lock(project.paths), pool_lock(project.paths):
            is_stopping = stopping(project.paths)
            decision = choose(needing, views, stopping=is_stopping,
                              any_limited=any(v.state == "limited" for v in views),
                              opening_failed=self.opening_failed)
            self.opening_failed = False
            for seat, card in decision.offers:
                if write_offer(project, seat, card):
                    self.log("qa_routed", card=card, to=seat)
            opened = []
            for card in decision.openings:
                with project_lock(project.paths):
                    cfg = project.reload_config()
                    name = next_member_name(cfg, seat_type(self.seat))
                    cfg.seats[name] = SeatConfig(type=seat_type(self.seat), role="qa")
                    save_config(project.paths, cfg)
                    project.reload_config()
                self.opened[name] = time.time()
                faults.point("after_member_registered")
                write_offer(project, name, card)
                self.log("qa_member_registered", member=name, card=card)
                opened.append(name)
            decision.openings = opened  # the members to start, after the locks
        return decision

    # ---------------------------------------------------------------- after the locks (step 5)
    def after_locks(self, decision: Decision, views) -> None:
        from kankan import agents_cmd
        for name in decision.openings:
            try:
                agents_cmd.start_member(self.project, name)
            except KankanError as err:
                self.open_failed(name, err.message)
        gone_now = {view.name for view in views if view.gone}
        for name in decision.closings:
            if name in gone_now:  # T§5 step 2: marked only once its liveness is checked again under the lock
                mark_gone(self.project, name, self.opened.get(name))
            else:
                self.close(name)
        self.remove_gone()

    def open_failed(self, name: str, error: str) -> None:
        """The opening failed: the member is closed and its offer dropped at once, so the next round can give the
        card to a warm seat (T§5 step 4) without waiting for the member's removal."""
        self.log("qa_open_failed", member=name, error=error)
        self.opening_failed = True
        self.opened.pop(name, None)
        clear_offer(self.project, name)
        self.close(name)

    def check_openings(self, views, now: float) -> None:
        """An opened member whose launcher did not reach ``idle`` within the startup deadline failed to open."""
        for view in views:
            opened = self.opened.get(view.name)
            if opened is None:
                continue
            if view.state not in ("starting", "stopped"):
                self.opened.pop(view.name, None)  # reached idle (or beyond): opened
            elif now - opened > startup_deadline_seconds(self.project, view.name):
                self.open_failed(view.name, "the agent did not become ready before its startup deadline")
                view.offer, view.removing = None, True  # this round's choice already sees the card free

    def close(self, name: str) -> None:
        """Mark a member for graceful removal (seatremove's mark only, never the user's removal paths, which also
        lower the autostart unit and the stop record): its launcher exits once drained and completes the removal."""
        from kankan.config import project_lock, save_config
        project = self.project
        if not is_member(name):
            return
        with project_lock(project.paths):
            cfg = project.reload_config()
            if name not in cfg.seats or cfg.seats[name].removing:
                return
            cfg.seats[name].removing = "graceful"
            save_config(project.paths, cfg)
            project.reload_config()
        self.log("qa_member_closing", member=name)

    def remove_gone(self) -> None:
        """Complete, off the router's loop, the removal of each marked member whose launcher is gone (a background
        ``kankan`` process per member, never two at once): ``seatremove.complete`` completes its pending answer first
        and may wait for Trello; a failure leaves it registered and marked for the next round."""
        import subprocess
        from kankan import build
        from kankan.seats import liveness, supervisor_running
        project = self.project
        cfg = project.reload_config()
        for name, process in list(self.removals.items()):
            if process.poll() is not None:
                del self.removals[name]
        for name in members(cfg):
            if not cfg.seats[name].removing or name in self.removals:
                continue
            live = liveness(project, name)
            if live == "alive" or live == "unavailable" or (live == "dead" and supervisor_running(project, name)):
                continue
            self.removals[name] = subprocess.Popen(build.command("pool-remove", name), cwd=str(project.paths.repo),
                                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL, start_new_session=True)
            self.log("qa_member_removing", member=name, pid=self.removals[name].pid)

    def children(self) -> set:
        return {process.pid for process in self.removals.values()}

    # ---------------------------------------------------------------- prefixes (step 6)
    def prefixes(self, qa_cards, open_cards, refs, views, now: float) -> None:
        project = self.project
        by_seat = {v.name: v for v in views}
        wanted: Dict[str, Optional[str]] = {}
        for card_id in qa_cards:
            value = refs.get(card_id)
            holder = value.meta.get("owner") if value is not None and value.meta.get("phase") == Phase.QA.value \
                else None
            view = by_seat.get(holder)
            held = view is not None and view.state != "stopped"
            warm_now = held and (view.warm or view.state == "working")
            wanted[card_id] = desired_prefix(held, warm_now, card_id in recorded(project.paths), card_id in open_cards)
        for card_id in recorded(project.paths):
            wanted.setdefault(card_id, None)  # left Q&A, archived or deleted
        for card_id, desired in wanted.items():
            try:
                write_prefix(project, card_id, desired)
            except KankanError as err:
                self.log("qa_prefix_failed", card=card_id, error=err.message)


def remove_member_main(cwd: str, seat: str) -> int:
    """``kankan pool-remove <seat>`` (hidden, run by the router in the background): complete the removal of a
    member marked for removal; a failure stays recorded in the member's ``remove_error``."""
    from kankan import seatremove
    from kankan.project import Project
    project = Project.open(cwd)
    if not is_member(seat):
        return 0
    try:
        # checked again under the removal lock: meanwhile another remover may have finished and the router reused the
        # name for a new member, not marked
        with file_lock(seatremove._lock_path(project.paths, seat), timeout=seatremove.LOCK_WAIT_SECONDS,
                       what="seat removal lock"):
            seat_cfg = project.reload_config().seats.get(seat)
            if seat_cfg is None or not seat_cfg.removing or not removable(project, seat):
                return 0
            seatremove.complete(project, seat)
    except KankanError as err:
        project.log("qa_member_remove_failed", seat=seat, error=err.message)
        return 1
    project.log("qa_member_removed", seat=seat)
    return 0


# ---------------------------------------------------------------------- start, stop and type change (T§9)

def mark_closing(project, seat: str) -> bool:
    """Mark a member for graceful removal (the closing mark: ``removing: graceful``); never the router, never a seat
    already marked. True when it marked."""
    from kankan.config import project_lock, save_config
    if not is_member(seat):
        return False
    with project_lock(project.paths):
        cfg = project.reload_config()
        if seat not in cfg.seats or cfg.seats[seat].removing:
            return False
        cfg.seats[seat].removing = "graceful"
        save_config(project.paths, cfg)
        project.reload_config()
    project.log("qa_member_closing", member=seat)
    return True


def removable(project, seat: str) -> bool:
    """A member whose launcher is gone for good: not alive, and not between two restarts by its supervisor."""
    from kankan.seats import liveness, supervisor_running
    live = liveness(project, seat)
    return live in ("free", "dead") and not supervisor_running(project, seat)


def remove_members(project, only_removable: bool = True) -> Dict[str, str]:
    """``seatremove.complete`` each registered member (only those whose launcher is gone, with ``only_removable``):
    each completes its pending answer first. Returns ``{member: error}`` of the failures (the member stays registered
    and marked). Never called with the lifecycle lock or the pool lock held."""
    from kankan import seatremove
    failed: Dict[str, str] = {}
    for seat in members(project.reload_config()):
        if only_removable and not removable(project, seat):
            continue
        try:
            seatremove.complete(project, seat)
        except KankanError as err:
            failed[seat] = err.message
            project.log("qa_member_remove_failed", member=seat, error=err.message)
    return failed


def router_start(project, lease) -> None:
    """The router's start (T§9), once it occupies its seat: (1) under the lifecycle lock, complete the pending answer
    or handoff of its own conversations with its own lease (no signal) and end them, the cards staying in ``Q&A``
    (one whose completion fails is kept: its launcher takes it up again); (2) drop a seat replacement record of an
    earlier version and end the conversations of unregistered Q&A seats; (3) with the lifecycle lock released,
    remove every registered member whose launcher is gone (after a reboot: all of them); (4) remove the recorded
    title prefixes. Members whose launcher runs are left alone: they keep their cards."""
    from kankan import qa_answer
    from kankan.config import project_lock
    from kankan.seatstate import load_state
    seat = lease.seat
    from kankan import seatremove
    with qa.lifecycle_lock(project.paths):
        state_task = load_state(project.paths, seat).task or {}
        owned = {value.meta.get("conversation") for value in qa._owned(project, seat)}
        for task in seatremove._pending_tasks(project, seat):
            if task["conversation"] in owned:
                continue  # completed below, with the end of its conversation
            try:  # a handoff whose transaction committed before a crash: its ref is parked, without owner
                qa_answer.complete_pending(project, lease, dict(task, id=task.get("id") or "router-start"),
                                           admin=True)
            except KankanError as err:
                project.log("qa_router_start_pending_failed", card=task.get("card"), error=err.message)
        for value in qa._owned(project, seat):
            meta = value.meta
            task = dict(state_task) if state_task.get("conversation") == meta.get("conversation") else {}
            task.update(kind="qa", card=meta["card"], short=meta.get("short", ""),
                        conversation=meta.get("conversation"), id=task.get("id") or "router-start")
            try:
                qa_answer.complete_pending(project, lease, task, admin=True)
            except KankanError as err:
                if err.status != Status.NOT_OWNER:
                    project.log("qa_router_start_pending_failed", card=meta["card"], error=err.message)
                    continue
            try:
                qa.end(project, lease, task)
            except KankanError as err:
                project.log("qa_router_start_end_failed", card=meta["card"], error=err.message)
                continue
            project.log("qa_conversation_ended", seat=seat, card=meta["card"], reason="router_start")
        legacy = project.paths.kankan_dir / "qa-replacing.json"
        if legacy.exists():
            legacy.unlink(missing_ok=True)
            project.log("qa_replacement_record_dropped")
        _end_unregistered(project)
    failed = remove_members(project)
    if failed:
        project.log("qa_router_start_members_left", failed=failed)
    clear_all(project)


def _end_unregistered(project) -> None:
    """End the conversations whose owner is a Q&A seat no longer registered (an unfinished seat replacement of an
    earlier version, or a removal interrupted after the entry went): its seat ref goes first when it does not run."""
    from kankan.config import project_lock
    from kankan.refstore import seat_ref
    from kankan.seats import liveness
    cfg = project.reload_config()
    owners = {value.meta.get("owner") for value in qa.parse_refs(project).values()
              if isinstance(value, RefValue) and value.meta.get("phase") == Phase.QA.value}
    for owner in sorted(o for o in owners if o and o not in cfg.seats):
        try:
            validate_seat(owner)
        except KankanError:
            continue
        if liveness(project, owner) == "alive":
            continue
        with project_lock(project.paths):
            current = project.refs.read(seat_ref(owner))
            if current is not None:
                project.refs.delete(seat_ref(owner), current.oid)
        if qa.end_admin(project, owner):
            project.log("qa_unregistered_ended", owner=owner)


def other_type_seats(cfg, agent_type: str) -> List[str]:
    """The registered Q&A seats of another type than ``agent_type``: members first, the router last."""
    seats_ = [name for name, sc in cfg.seats.items() if sc.role == "qa" and name.rsplit("-", 1)[0] != agent_type]
    return sorted(seats_, key=lambda name: (not is_member(name), name))


def change_type(project, agent_type: str) -> List[str]:
    """The type change (T§9): with a pool of another type, under ``up.lock`` set the stopping mark and kill every
    seat of that pool; then, with ``up.lock`` released, remove them all with ``seatremove.complete``, members first
    and the old router last, each completing the answer it was posting. A failure stops with ``needs_action`` naming
    the seat, the mark staying set: a rerun resumes. The caller registers the new router under ``up.lock``, checks
    that no seat of the old pool is left (``check_no_other_type``) and clears the mark. Returns the removed seats."""
    from kankan import seatremove
    from kankan.config import file_lock
    from kankan import messages
    up_lock = project.paths.kankan_dir / "up.lock"
    with file_lock(up_lock, timeout=300, what="up lock"):
        if not other_type_seats(project.reload_config(), agent_type):
            return []
        set_stopping(project, True)
        olds = other_type_seats(project.reload_config(), agent_type)  # once marked: no member opens any more
        for seat in olds:
            seatremove.stop_seat(project, seat, kill_alive=True)
        project.log("qa_type_change", old=olds, new=agent_type)
    for seat in olds:
        try:
            seatremove.complete(project, seat)
        except KankanError as err:
            raise KankanError(Status.NEEDS_ACTION, messages.QA_TYPE_CHANGE_FAILED.format(
                seat=seat, error=err.message, type=agent_type), {"seat": seat}) from err
    return olds


def check_no_other_type(project, agent_type: str) -> None:
    """Under ``up.lock``, before the new router is registered: no seat of another type's pool is left."""
    from kankan import messages
    left = other_type_seats(project.reload_config(), agent_type)
    if left:
        raise KankanError(Status.NEEDS_ACTION, messages.QA_TYPE_CHANGE_RACE.format(seats=", ".join(left),
                                                                                    type=agent_type))
