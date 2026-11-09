"""Trello REST client: retries, read-after-write, error classification, rate limiting.

This is the only module that talks to Trello (see ``aispecs/kanban_requirements.md``).
Credentials travel in the ``Authorization`` header, never in URLs, so they cannot
leak into error messages or logs.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from kankan import clock
from kankan.errors import KankanError, Status

MARKER_PREFIX = "kankan:"


def marker(op_id: str) -> str:
    return f"`{MARKER_PREFIX}{op_id}`"


@dataclass
class Card:
    id: str
    short: str
    board_id: str
    name: str
    desc: str
    url: str
    list_id: str
    pos: float
    label_ids: List[str]
    attachments: List[str]
    closed: bool
    last_activity: str


@dataclass
class TList:
    id: str
    name: str
    closed: bool
    pos: float


@dataclass
class Label:
    id: str
    name: str
    color: Optional[str]


@dataclass
class Snapshot:
    board_id: str
    desc: str
    lists: Dict[str, TList] = field(default_factory=dict)
    cards: Dict[str, Card] = field(default_factory=dict)
    labels: Dict[str, Label] = field(default_factory=dict)


@dataclass
class Comment:
    id: str
    member_id: str
    username: str
    date: str
    text: str


class _Transient(Exception):
    def __init__(self, reason: str, retry_after: Optional[float] = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class _RateBucket:
    """Per-process sliding window: at most ``limit`` requests per ``window`` seconds."""

    def __init__(self, limit: int = 90, window: float = 10.0):
        self.limit, self.window = limit, window
        self.times: deque = deque()
        self.lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self.lock:
                now = clock.monotonic()
                while self.times and now - self.times[0] >= self.window:
                    self.times.popleft()
                if len(self.times) < self.limit:
                    self.times.append(now)
                    return
                wait = self.window - (now - self.times[0])
            clock.sleep(max(wait, 0.01))


_BUCKET = _RateBucket()


def _card(data: dict) -> Card:
    return Card(id=data["id"], short=data.get("shortLink", ""), board_id=data.get("idBoard", ""),
                name=data.get("name", ""), desc=data.get("desc", ""), url=data.get("url", ""),
                list_id=data.get("idList", ""), pos=data.get("pos", 0), label_ids=list(data.get("idLabels", [])),
                attachments=[a.get("url", "") for a in data.get("attachments", [])],
                closed=bool(data.get("closed", False)), last_activity=data.get("dateLastActivity", ""))


def _tlist(data: dict) -> TList:
    return TList(id=data["id"], name=data.get("name", ""), closed=bool(data.get("closed", False)), pos=data.get("pos", 0))


def _label(data: dict) -> Label:
    return Label(id=data["id"], name=data.get("name", ""), color=data.get("color"))


def _comment(data: dict) -> Comment:
    member = data.get("memberCreator") or {}
    return Comment(id=data["id"], member_id=data.get("idMemberCreator", ""), username=member.get("username", ""),
                   date=data.get("date", ""), text=(data.get("data") or {}).get("text", ""))


class TrelloClient:
    def __init__(self, key: str, token: str, base_url: str = "https://api.trello.com", timeout: float = 5.0,
                 max_retries: int = 4, budget: float = 15.0):
        self._auth = f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"'
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.budget = budget

    # ------------------------------------------------------------------ transport
    def _once(self, method: str, path: str, params: Optional[dict], body: Optional[dict]):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", self._auth)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        _BUCKET.take()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "replace")[:200]
            if err.code == 429:
                retry = err.headers.get("Retry-After")
                raise _Transient("rate limited (429)", float(retry) if retry and retry.isdigit() else None)
            if err.code >= 500:
                raise _Transient(f"server error {err.code}")
            return err.code, text
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise _Transient(f"connection failed: {type(reason).__name__}")
        except ValueError:
            raise KankanError(Status.TRELLO_ERROR, f"{method} {path}: response is not JSON")

    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None,
                 retry: bool = True, ok_codes=(200,)):
        """Send a request, retrying transient failures (if ``retry``) within the time budget.

        Returns ``(status, payload)`` for status codes in ``ok_codes`` or 404/400; maps the
        rest to KankanError.
        """
        deadline = clock.monotonic() + self.budget
        attempt = 0
        while True:
            try:
                status, payload = self._once(method, path, params, body)
            except _Transient as err:
                attempt += 1
                delay = err.retry_after if err.retry_after is not None else min(0.5 * 2 ** (attempt - 1), 4.0)
                if not retry or attempt > self.max_retries or clock.monotonic() + delay >= deadline:
                    raise KankanError(Status.NETWORK_ERROR, f"Trello {method} {path.split('?')[0]}: {err.reason}",
                                      {"method": method, "path": path})
                clock.sleep(delay)
                continue
            if status in ok_codes:
                return status, payload
            if status in (401, 403):
                raise KankanError(Status.CONFIG_ERROR, f"Trello refused the credentials ({status}) on {method} {path}")
            return status, payload

    def _ok(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None, retry: bool = True):
        status, payload = self._request(method, path, params, body, retry)
        if status != 200:
            raise KankanError(Status.TRELLO_ERROR, f"Trello {method} {path}: HTTP {status}: {str(payload)[:120]}",
                              {"status": status})
        return payload

    def _write_once(self, do: Callable[[], object], find: Callable[[], object]):
        """Non-idempotent write: never retry blindly; after a transient failure reread first."""
        deadline = clock.monotonic() + self.budget
        attempt = 0
        while True:
            try:
                return do()
            except KankanError as err:
                if err.status != Status.NETWORK_ERROR:
                    raise
                attempt += 1
                existing = find()
                if existing is not None:
                    return existing
                if attempt > self.max_retries or clock.monotonic() >= deadline:
                    raise
                clock.sleep(min(0.5 * 2 ** (attempt - 1), 4.0))

    # ------------------------------------------------------------------ reads
    def me(self) -> dict:
        return self._ok("GET", "/1/members/me", {"fields": "id,username,fullName"})

    def member(self, username: str) -> Optional[dict]:
        status, payload = self._request("GET", f"/1/members/{urllib.parse.quote(username)}", {"fields": "id,username"})
        if status in (400, 404):
            return None
        if status != 200:
            raise KankanError(Status.TRELLO_ERROR, f"Trello GET member: HTTP {status}")
        return payload

    def card_creator(self, card_id: str) -> Optional[Tuple[str, str]]:
        """(member id, username) of whoever created the card, or None if Trello no longer has that action."""
        actions = self._ok("GET", f"/1/cards/{card_id}/actions",
                           {"filter": "createCard,copyCard,convertToCardFromCheckItem", "fields": "type,idMemberCreator",
                            "memberCreator_fields": "username", "limit": "1000"})
        if not actions:
            return None
        oldest = min(actions, key=lambda a: a["id"])
        member = oldest.get("memberCreator") or {}
        return (oldest.get("idMemberCreator") or member.get("id"), member.get("username", ""))

    def board_members(self, board_id: str) -> List[Tuple[str, str, str]]:
        """(member id, username, member type) of the board's members (admin, normal, observer)."""
        memberships = self._ok("GET", f"/1/boards/{board_id}/memberships",
                               {"member": "true", "member_fields": "username"})
        return [(m["idMember"], (m.get("member") or {}).get("username", ""), m.get("memberType", ""))
                for m in memberships]

    def board_admins(self, board_id: str) -> List[Tuple[str, str]]:
        """(member id, username) of the board's admins."""
        return [(i, u) for i, u, kind in self.board_members(board_id) if kind == "admin"]

    def board(self, short_or_id: str) -> dict:
        return self._ok("GET", f"/1/boards/{urllib.parse.quote(short_or_id)}", {"fields": "id,name,desc,shortLink,url"})

    def snapshot(self, board_id: str) -> Snapshot:
        """One nested board read: description, all lists, open cards with attachments, labels."""
        params = {"fields": "name,desc", "lists": "all", "list_fields": "name,closed,pos", "cards": "open",
                  "card_fields": "name,desc,idBoard,idList,pos,idLabels,url,shortLink,closed,dateLastActivity",
                  "card_attachments": "true", "card_attachment_fields": "url", "labels": "all",
                  "labels_limit": "1000", "label_fields": "name,color"}
        data = self._ok("GET", f"/1/boards/{board_id}", params)
        snap = Snapshot(board_id=data.get("id", board_id), desc=data.get("desc", ""))
        snap.lists = {l["id"]: _tlist(l) for l in data.get("lists", [])}
        snap.cards = {c["id"]: _card(c) for c in data.get("cards", [])}
        labels = data.get("labels", [])
        if len(labels) >= 1000:
            labels = self._all_labels(board_id)
        snap.labels = {l["id"]: _label(l) for l in labels}
        return snap

    def _all_labels(self, board_id: str) -> list:
        result, before = [], None
        while True:
            params = {"limit": "1000", "fields": "name,color"}
            if before:
                params["before"] = before
            page = self._ok("GET", f"/1/boards/{board_id}/labels", params)
            result.extend(page)
            if len(page) < 1000:
                return result
            before = min(l["id"] for l in page)

    def card(self, id_or_short: str) -> Optional[Card]:
        status, payload = self._request("GET", f"/1/cards/{urllib.parse.quote(id_or_short)}", {
            "attachments": "true", "attachment_fields": "url",
            "fields": "name,desc,idBoard,idList,pos,idLabels,url,shortLink,closed,dateLastActivity"})
        if status in (400, 404):
            return None
        if status != 200:
            raise KankanError(Status.TRELLO_ERROR, f"Trello GET card: HTTP {status}")
        return _card(payload)

    def list_info(self, list_id: str) -> Optional[TList]:
        status, payload = self._request("GET", f"/1/lists/{list_id}", {"fields": "name,closed,pos"})
        if status in (400, 404):
            return None
        if status != 200:
            raise KankanError(Status.TRELLO_ERROR, f"Trello GET list: HTTP {status}")
        return _tlist(payload)

    def comments(self, card_id: str, since: Optional[str] = None) -> List[Comment]:
        """Comments posted after ``since`` (an action id), oldest first, following pagination."""
        result: List[Comment] = []
        before = None
        while True:
            params = {"filter": "commentCard", "limit": "50",
                      "fields": "idMemberCreator,data,date,type", "memberCreator_fields": "username"}
            if since:
                params["since"] = since
            if before:
                params["before"] = before
            page = self._ok("GET", f"/1/cards/{card_id}/actions", params)
            result.extend(_comment(c) for c in page)
            if len(page) < 50:
                break
            before = min(c["id"] for c in page)
        result.sort(key=lambda c: c.id)
        return result

    # ------------------------------------------------------------------ writes
    def create_list(self, board_id: str, name: str, pos: str = "bottom") -> TList:
        def find():
            board = self._ok("GET", f"/1/boards/{board_id}", {"lists": "open", "list_fields": "name,closed,pos", "fields": "id"})
            matches = [l for l in board.get("lists", []) if l["name"] == name and not l.get("closed")]
            return _tlist(matches[-1]) if matches else None
        return self._write_once(lambda: _tlist(self._ok("POST", "/1/lists", body={"name": name, "idBoard": board_id, "pos": pos},
                                                         retry=False)), find)

    def archive_list(self, list_id: str) -> None:
        self._ok("PUT", f"/1/lists/{list_id}/closed", body={"value": True})

    def move_card(self, card_id: str, list_id: str) -> Card:
        """Move to the top of ``list_id`` and verify with a reread."""
        self._ok("PUT", f"/1/cards/{card_id}", body={"idList": list_id, "pos": "top"})
        card = self.card(card_id)
        if card is None or card.list_id != list_id:
            raise KankanError(Status.TRELLO_ERROR, f"card {card_id} not in the expected list after the move",
                              {"card": card_id, "expected": list_id, "found": card.list_id if card else None})
        return card

    def add_label(self, card_id: str, label_id: str) -> None:
        status, payload = self._request("POST", f"/1/cards/{card_id}/idLabels", body={"value": label_id})
        if status == 400 and "already" in str(payload):
            return
        if status != 200:
            raise KankanError(Status.TRELLO_ERROR, f"Trello add label: HTTP {status}: {str(payload)[:120]}")

    def remove_label(self, card_id: str, label_id: str) -> None:
        status, payload = self._request("DELETE", f"/1/cards/{card_id}/idLabels/{label_id}")
        if status in (200, 400, 404):
            return
        raise KankanError(Status.TRELLO_ERROR, f"Trello remove label: HTTP {status}")

    def create_label(self, board_id: str, name: str, color: Optional[str]) -> Label:
        def find():
            labels = self._ok("GET", f"/1/boards/{board_id}/labels", {"limit": "1000", "fields": "name,color"})
            matches = [l for l in labels if l["name"] == name]
            return _label(matches[0]) if matches else None
        return self._write_once(lambda: _label(self._ok("POST", "/1/labels", body={"idBoard": board_id, "name": name, "color": color},
                                                         retry=False)), find)

    def delete_label(self, label_id: str) -> None:
        status, _ = self._request("DELETE", f"/1/labels/{label_id}")
        if status not in (200, 404):
            raise KankanError(Status.TRELLO_ERROR, f"Trello delete label: HTTP {status}")

    def set_board_desc(self, board_id: str, desc: str) -> None:
        self._ok("PUT", f"/1/boards/{board_id}", body={"desc": desc})

    def add_comment(self, card_id: str, text: str, op_id: str) -> Comment:
        """Comment ending with the op-id marker; after a timeout an existing marker means success."""
        full = f"{text}\n\n{marker(op_id)}"

        def find():
            for comment in reversed(self.comments(card_id)):
                if comment.text.rstrip().endswith(marker(op_id)):
                    return comment
            return None
        return self._write_once(lambda: _comment(self._ok("POST", f"/1/cards/{card_id}/actions/comments",
                                                          body={"text": full}, retry=False)), find)

    def comment_once(self, card_id: str, text: str, op_id: str) -> Comment:
        """Post the comment unless one with this op-id marker already exists."""
        return self.find_comment(card_id, op_id) or self.add_comment(card_id, text, op_id)

    def find_comment(self, card_id: str, op_id: str) -> Optional[Comment]:
        for comment in reversed(self.comments(card_id)):
            if comment.text.rstrip().endswith(marker(op_id)):
                return comment
        return None

    def create_card(self, list_id: str, name: str, desc: str = "", pos: str = "bottom") -> Card:
        body = {"idList": list_id, "name": name, "pos": pos}
        if desc:
            body["desc"] = desc
        return _card(self._ok("POST", "/1/cards", body=body, retry=False))

    def delete_card(self, card_id: str) -> None:
        self._ok("DELETE", f"/1/cards/{card_id}")
