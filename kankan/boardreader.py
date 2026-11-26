"""The board reader (board reader technical): the only caller of ``TrelloClient``'s read methods.

Every read states its freshness (``LIVE`` or ``max_age(seconds)``). Results are kept in a cache shared by the
processes of the project (``<gcd>/kankan/board-cache/``), each with the local time its request was sent; writes
report what they changed (``on_write``), which marks the units a reader could see it through, so no reader is
given the state before kankan's own write as fresh. The marks and the locks live outside the cache directory, so
deleting the cache only loses cached data.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kankan.config import atomic_write_json, file_lock
from kankan.errors import KankanError, Status
from kankan.trello import (Card, Comment, Read, SettingsRead, Snapshot, TList, WriteEffect, decode_card, decode_comment,
                           decode_snapshot)

FORMAT = 1
STORE_LOCK = "board-store.lock"
MARKS = "board-marks.json"
CACHE = "board-cache"
REFETCH_LIMIT = 3  # a fetch overtaken by writes this many times in a row is returned as it is
BOARD_PATCH_MAX = 3  # at most this many stale cards of the board view are read one by one (technical §3)
PRUNE_AGE = 86400  # card and thread files of cards absent from a board read are deleted after a day
LOG_PAGE = 1000  # M7's largest page
LOG_KEEP = 86400  # the comment log keeps a day of comments
FETCH_WAIT_EXTRA = 2.0  # a poller waits for another's fetch at most the client's budget plus this, then fetches
ORIGIN = "origin"  # the horizon of a log started on a board without comments: older than every point
_CARD_ID = re.compile(r"^[0-9a-f]{24}$")


@dataclass(frozen=True)
class Freshness:
    """``age`` None: live, a request sent after the call; otherwise the oldest accepted send time is ``age``
    seconds before the call (``max_age(0)`` is live)."""
    age: Optional[float]

    @property
    def live(self) -> bool:
        return self.age is None or self.age <= 0

    def not_before(self, called_at: float) -> float:
        return called_at if self.live else called_at - self.age


LIVE = Freshness(None)


def max_age(seconds: float) -> Freshness:
    return Freshness(float(seconds))


def _fresh(fresh: Freshness) -> Freshness:
    """Test switch ``KANKAN_TEST_NO_CACHE=1``: every read live (tests change the fake board directly, which sets no
    write mark); the reader's own tests unset it."""
    return LIVE if os.environ.get("KANKAN_TEST_NO_CACHE") == "1" else fresh


def _unit_path(cache: Path, key: str) -> Path:
    kind, _, ident = key.partition(":")
    if kind == "card":
        return cache / "cards" / f"{ident}.json"
    if kind == "thread":
        return cache / "threads" / f"{ident}.json"
    return cache / f"{kind}.json"


def _effect_keys(effect: WriteEffect) -> List[str]:
    """The units through which a reader could see a write (technical §4)."""
    card = effect.card_id
    if effect.kind == "card":
        return [f"card:{card}"] if card else ["board"]
    if effect.kind == "comment":
        return [f"thread:{card}", "log"]
    if effect.kind == "comment_edit":
        return [f"thread:{card}"]
    if effect.kind == "card_set":
        return ["board"] + ([f"card:{card}"] if card else [])
    return ["board"]


class BoardReader:
    def __init__(self, state_dir: Optional[Path], client, board_id: Optional[str]):
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.client = client
        self.board_id = board_id

    @classmethod
    def unbound(cls, client, board_id: Optional[str] = None) -> "BoardReader":
        """A reader without a cache (``kankan init`` before the binding exists, ``kankan destroy``): every read live,
        nothing stored or marked; ``board_id`` is the board found by ``board_info``, when known."""
        return cls(None, client, board_id)

    # ------------------------------------------------------------------ files
    @property
    def _cache(self) -> Path:
        return self.state_dir / CACHE

    def _ensure_cache(self) -> None:
        self._cache.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _load(self, key: str) -> Optional[Read]:
        if self.state_dir is None:
            return None
        try:
            with open(_unit_path(self._cache, key)) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("board_id") != self.board_id \
                or not isinstance(data.get("sent_at"), (int, float)):
            return None
        date = data.get("response_date")
        try:
            response_date = datetime.fromisoformat(date) if isinstance(date, str) else None
        except ValueError:
            response_date = None
        return Read(data.get("payload"), float(data["sent_at"]), response_date)

    def _marks(self) -> Dict[str, float]:
        if self.state_dir is None:
            return {}
        try:
            with open(self.state_dir / MARKS) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("board_id") != self.board_id:
            return {}
        marks = data.get("marks")
        return {k: float(v) for k, v in marks.items() if isinstance(v, (int, float))} if isinstance(marks, dict) else {}

    def _store(self, key: str, read: Read) -> bool:
        """Newest wins: a result replaces the stored one only when it was sent later. Returns False when the unit's
        mark rejects the result (sent before a write to it), checked under the store lock, where marks are written."""
        if self.state_dir is None:
            return True
        with file_lock(self.state_dir / STORE_LOCK, what="board cache store lock"):
            if read.sent_at <= self._marks().get(key, float("-inf")):
                return False
            stored = self._load(key)
            if stored is not None and stored.sent_at >= read.sent_at:
                return True
            document = {"format": FORMAT, "board_id": self.board_id, "sent_at": read.sent_at,
                        "response_date": read.response_date.isoformat() if read.response_date else None,
                        "payload": read.payload}
            for _ in range(2):  # the cache directory may be deleted meanwhile: harmless, write it again
                try:
                    self._ensure_cache()
                    path = _unit_path(self._cache, key)
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    _write_cache_file(path, document)
                    break
                except FileNotFoundError:
                    continue
            return True

    # ------------------------------------------------------------------ marks
    def on_write(self, effect: WriteEffect) -> None:
        """The client's write hook: mark the units of ``effect`` with the time the write returned."""
        if self.state_dir is None:
            return
        now = time.time()
        with file_lock(self.state_dir / STORE_LOCK, what="board cache store lock"):
            marks = self._marks()
            for key in _effect_keys(effect):
                marks[key] = max(now, marks.get(key, now))
            # without fsync: marks matter only while processes run, and a crash of the machine ends them all
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _write_cache_file(self.state_dir / MARKS, {"format": FORMAT, "board_id": self.board_id, "marks": marks})

    # ------------------------------------------------------------------ reads
    def _usable(self, read: Optional[Read], key: str, not_before: float, marks: Dict[str, float]) -> bool:
        return read is not None and read.sent_at >= not_before and read.sent_at > marks.get(key, float("-inf"))

    def _read(self, key: str, fresh: Freshness, fetch: Callable[[], Read]) -> Read:
        """One unit read: from the cache when it satisfies ``fresh`` and its mark, otherwise fetched; a fetch
        overtaken by a write to the unit is fetched again, so a result predating a write is returned only to a
        read called before that write (technical §4)."""
        called_at = time.time()
        not_before = fresh.not_before(called_at)
        if self.state_dir is None or fresh.live:
            return self._fetch(key, fetch)
        cached = self._load(key)
        if self._usable(cached, key, not_before, self._marks()):
            return cached
        try:
            with file_lock(self.state_dir / f"board-{_lock_kind(key)}.fetch.lock",
                           timeout=getattr(self.client, "budget", 15.0) + FETCH_WAIT_EXTRA, what="board fetch lock"):
                cached = self._load(key)  # another process may have fetched while this one waited
                if self._usable(cached, key, not_before, self._marks()):
                    return cached
                return self._fetch(key, fetch)
        except KankanError as err:
            if err.message.startswith("timed out waiting for the board fetch lock"):
                return self._fetch(key, fetch)  # the other fetch is stuck: one more request than needed
            raise

    def _fetch(self, key: str, fetch: Callable[[], Read]) -> Read:
        """Fetch until a result is not overtaken by a write to the unit; never return one that is (technical §4)."""
        for _ in range(REFETCH_LIMIT + 1):
            read = fetch()
            if self._store(key, read):
                return read
        raise KankanError(Status.TRELLO_ERROR, f"the board changed during every read of {key}; try again")

    # ------------------------------------------------------------------ board view (technical §3)
    def _fetch_board(self) -> Read:
        read = self.client.read_board(self.board_id)
        if self.state_dir is not None:
            self._prune(read.payload)
        return read

    def _prune(self, payload) -> None:
        present = {c.get("id") for c in (payload or {}).get("cards", []) if isinstance(c, dict)}
        limit = time.time() - PRUNE_AGE
        for sub in ("cards", "threads"):
            directory = self._cache / sub
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for path in entries:
                if path.stem in present:
                    continue
                try:
                    with file_lock(self.state_dir / STORE_LOCK, what="board cache store lock"):
                        if path.stat().st_mtime < limit:  # checked under the lock: never a file just replaced
                            path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _nested_card(payload: dict, card_id: str) -> Optional[dict]:
        """A card of a board read with its checklists (the board read gives them in a top-level array)."""
        raw = next((c for c in payload.get("cards", []) if isinstance(c, dict) and c.get("id") == card_id), None)
        if raw is None:
            return None
        return dict(raw, checklists=[c for c in payload.get("checklists", []) if c.get("idCard") == card_id])

    def snapshot(self, fresh: Freshness) -> Snapshot:
        """The board unit with each card replaced by its card unit when fresher; a view whose cards fall short of
        ``fresh`` or of their marks is patched card by card (at most ``BOARD_PATCH_MAX``) or read again."""
        fresh = _fresh(fresh)
        called_at = time.time()
        not_before = fresh.not_before(called_at)
        board = self._read("board", fresh, self._fetch_board)
        for attempt in range(2):
            marks = self._marks()
            snap = decode_snapshot(board.payload, board.response_date, self.board_id)
            dates = [board.response_date]
            stale = []
            for card_id in list(snap.cards):
                key = f"card:{card_id}"
                unit = self._load(key)
                part = unit if unit is not None and unit.sent_at > board.sent_at else board
                if part.sent_at < not_before or part.sent_at <= marks.get(key, float("-inf")):
                    stale.append(card_id)  # freshness first: a stale unit never removes a card
                    continue
                if part is unit:
                    dates.append(unit.response_date)
                    self._apply(snap, card_id, decode_card(unit.payload))
            if not stale:
                snap.response_date = _oldest(dates)
                return snap
            if len(stale) <= BOARD_PATCH_MAX or attempt == 1:
                for card_id in stale:
                    read = self._card_read(card_id)
                    dates.append(read.response_date)
                    self._apply(snap, card_id, decode_card(read.payload))
                snap.response_date = _oldest(dates)
                return snap
            board = self._read("board", LIVE, self._fetch_board)
        raise AssertionError("unreachable")

    def _apply(self, snap: Snapshot, card_id: str, card: Optional[Card]) -> None:
        """A fresher card replaces the board's; one archived, deleted or on another board leaves the view."""
        if card is None or card.closed or card.board_id != self.board_id:
            snap.cards.pop(card_id, None)
        else:
            snap.cards[card_id] = card

    def _card_read(self, card_id: str) -> Read:
        return self._read(f"card:{card_id}", LIVE, lambda: self.client.read_card(card_id))

    def card(self, card_id: str, fresh: Freshness) -> Optional[Card]:
        """One card (``None`` when deleted, a closed card when archived), from the fresher of its card unit and the
        board unit when they satisfy ``fresh`` and the card's mark, otherwise read (C1). A short link reads live."""
        if not _CARD_ID.match(card_id or ""):
            return decode_card(self.client.read_card(card_id).payload) if self.state_dir is None \
                else self._card_by_short(card_id)
        fresh = _fresh(fresh)
        called_at = time.time()
        not_before = fresh.not_before(called_at)
        key = f"card:{card_id}"
        if self.state_dir is not None and not fresh.live:
            marks = self._marks()
            unit = self._load(key)
            board = self._load("board")
            nested = self._nested_card(board.payload, card_id) if board is not None and \
                isinstance(board.payload, dict) else None
            board_newer = board is not None and (unit is None or board.sent_at > unit.sent_at)
            if nested is not None and board_newer and self._usable(board, key, not_before, marks) and \
                    self._usable(board, "board", not_before, marks):
                return decode_card(nested)
            contradicted = board_newer and nested is None and unit is not None and _open_here(unit, self.board_id)
            if not contradicted and self._usable(unit, key, not_before, marks):
                return decode_card(unit.payload)  # a newer board read without an open cached card needs C1
        return decode_card(self._card_read(card_id).payload)

    def _card_by_short(self, short: str) -> Optional[Card]:
        for _ in range(REFETCH_LIMIT + 1):
            read = self.client.read_card(short)
            card = decode_card(read.payload)
            if card is None or self._store(f"card:{card.id}", read):
                return card
        raise KankanError(Status.TRELLO_ERROR, f"the card {short} changed during every read; try again")

    def list_info(self, list_id: str, fresh: Freshness) -> Optional[TList]:
        """A list from the board view (``closed`` set when archived); None when the board read lacks it (deleted)."""
        return self.snapshot(fresh).lists.get(list_id)

    def settings_cards(self, list_id: str, fresh: Freshness) -> SettingsRead:
        """The open cards of ``list_id`` with their checklists, from the board view, with the view's response date."""
        snap = self.snapshot(fresh)
        info = snap.lists.get(list_id)
        if info is None or info.closed:
            return SettingsRead([], None, False)
        cards = sorted((c for c in snap.cards.values() if c.list_id == list_id), key=lambda c: c.pos)
        return SettingsRead(cards, snap.response_date, True)

    # ------------------------------------------------------------------ threads, members, immutable (technical §2)
    def thread(self, card_id: str, fresh: Freshness) -> List[Comment]:
        """Every comment of the card (M2, all pages), oldest first by date."""
        read = self._read(f"thread:{card_id}", _fresh(fresh), lambda: self.client.read_comments(card_id))
        return [decode_comment(raw) for raw in read.payload]

    def members(self, fresh: Freshness):
        """(member id, username, member type) of the board's members."""
        read = self._read("members", _fresh(fresh), lambda: self.client.read_members(self.board_id))
        return [(m["idMember"], (m.get("member") or {}).get("username", ""), m.get("memberType", ""))
                for m in read.payload]

    def admins(self, fresh: Freshness):
        return [(i, u) for i, u, kind in self.members(fresh) if kind == "admin"]

    def _token_key(self) -> str:
        return hashlib.sha256(self.client._auth.encode()).hexdigest()[:24]

    def _immutable(self) -> dict:
        read = self._load("immutable")
        return read.payload if read is not None and isinstance(read.payload, dict) else {}

    def _remember(self, section: str, key: str, value) -> None:
        if self.state_dir is None:
            return
        with file_lock(self.state_dir / STORE_LOCK, what="board cache store lock"):
            data = self._immutable()
            data.setdefault(section, {})[key] = value
            self._ensure_cache()
            atomic_write_json(_unit_path(self._cache, "immutable"), {"format": FORMAT, "board_id": self.board_id,
                                                                     "sent_at": time.time(), "response_date": None,
                                                                     "payload": data}, sort_keys=False)

    def me(self, fresh: Freshness) -> dict:
        """The token's member, kept per credentials; ``LIVE`` always asks Trello (revoked credentials fail)."""
        key = self._token_key()
        if not _fresh(fresh).live:
            kept = self._immutable().get("me", {}).get(key)
            if isinstance(kept, dict):
                return kept
        member = self.client.read_me().payload
        self._remember("me", key, member)
        return member

    def card_creator(self, card_id: str):
        """(member id, username) of the card's creator, kept forever once known."""
        kept = self._immutable().get("creators", {}).get(card_id)
        if isinstance(kept, list) and len(kept) == 2:
            return tuple(kept)
        creator = self.client.card_creator(card_id)
        if creator is not None:
            self._remember("creators", card_id, list(creator))
        return creator

    # ------------------------------------------------------------------ always live, never stored
    def closed_cards(self, *, limit: int = 1000):
        return self.client.closed_cards(self.board_id, limit=limit)

    def board_payload(self):
        return self.client.board_payload(self.board_id)

    def card_place(self, card_id: str):
        return self.client.card_place(card_id)

    def board_info(self, short_or_id: str) -> dict:
        return self.client.board(short_or_id)

    def member(self, username: str):
        return self.client.member(username)

    # ------------------------------------------------------------------ comment log (technical §7)
    def _refresh_log(self) -> Read:
        """Start or refresh the log from the stored one: newest first by date, ``since`` the cursor (exclusive),
        pages ``before`` the oldest date of a full page; entries older than a day pruned into the horizon. M7
        refused (400/404) marks the log unsupported."""
        sent_at = time.time()
        stored = self._load("log")
        log = stored.payload if stored is not None and isinstance(stored.payload, dict) else None
        if log is not None and log.get("unsupported") and sent_at - log.get("checked_at", 0) < LOG_KEEP:
            return Read(log, sent_at)  # M7 refused: probed again once a day
        try:
            if log is None or log.get("unsupported"):
                page = self.client.read_board_comments(self.board_id, limit=1).payload
                cursor = page[0]["date"] if page else ""
                log = {"cursor": cursor, "horizon": cursor or ORIGIN, "entries": []}
            else:
                cursor, fetched, before = log["cursor"], [], None
                while True:
                    page = self.client.read_board_comments(self.board_id, since=cursor or None, before=before,
                                                           limit=LOG_PAGE).payload
                    fetched.extend(a for a in page if not cursor or a["date"] > cursor)
                    if len(page) < LOG_PAGE:
                        break
                    before = min(a["date"] for a in page)
                known = {a["id"] for a in log["entries"]}
                entries = log["entries"] + [a for a in fetched if a["id"] not in known]
                log = {"cursor": max([cursor] + [a["date"] for a in fetched]), "horizon": log["horizon"],
                       "entries": entries}
        except KankanError as err:
            if (err.data or {}).get("status") in (400, 404):
                return Read({"unsupported": True, "checked_at": sent_at, "cursor": "", "horizon": "", "entries": []},
                            sent_at)
            raise
        cutoff = datetime.fromtimestamp(time.time() - LOG_KEEP, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        dropped = [a["date"] for a in log["entries"] if a["date"] < cutoff]
        if dropped:
            log["entries"] = [a for a in log["entries"] if a["date"] >= cutoff]
            newest = max(dropped)
            log["horizon"] = newest if log["horizon"] == ORIGIN else max(log["horizon"], newest)
        log["entries"].sort(key=lambda a: (a["date"], a["id"]))
        return Read(log, sent_at)

    def new_comments(self, card_id: str, after: str, fresh: Freshness):
        """``(comments, point)``: the card's comments dated after ``after``, oldest first, and the completeness
        point (a comment date) up to which the answer is complete, never before ``after``. Served by the log when it
        reaches back to ``after``; otherwise the log is brought to ``fresh`` and the card's comments are read."""
        after = after or ""
        log = self._read("log", _fresh(fresh), self._refresh_log).payload
        if log.get("unsupported"):
            comments = [decode_comment(raw) for raw in self.client.read_comments(card_id, after or None).payload]
            return comments, max([after] + [c.date for c in comments])
        horizon = log["horizon"]
        if horizon == ORIGIN or (after and after >= horizon):
            comments = [decode_comment(a) for a in log["entries"]
                        if a["data"]["card"]["id"] == card_id and a["date"] > after]
            return comments, max(log["cursor"], after)
        cursor = log["cursor"]
        comments = [decode_comment(raw) for raw in self.client.read_comments(card_id, after or None).payload
                    if raw["date"] > after]
        return comments, max([cursor, after] + [c.date for c in comments])


def _write_cache_file(path: Path, document: dict) -> None:
    """Write a cache file through a temporary file and a rename, without fsync: a cache file lost by a crash is
    only read from Trello again, and an fsync per read slowed every poll."""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _lock_kind(key: str) -> str:
    kind = key.partition(":")[0]
    return {"card": "cards", "thread": "threads"}.get(kind, kind)


def _oldest(dates):
    """The oldest response date of a view's parts; unknown when any part has none."""
    return None if not dates or any(d is None for d in dates) else min(dates)


def _open_here(read: Read, board_id: Optional[str]) -> bool:
    card = decode_card(read.payload)
    return card is not None and not card.closed and card.board_id == board_id
