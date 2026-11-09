"""In-memory fake of the subset of the Trello REST API that kankan uses.

Runs an HTTP server on 127.0.0.1 in a background thread. Tests drive it through
the REST API (via TrelloClient) and through helper methods that play the human
user (``add_card``, ``user_move``, ``user_comment``...). Faults can be injected
per request with ``inject``.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import secrets
import string
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEY = "fakekey0123456789abcdef0123456789"
TOKEN = "faketoken" + "0" * 55


def _short() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


class FakeTrello:
    def __init__(self, key: str = KEY, token: str = TOKEN):
        self.key, self.token = key, token
        self.lock = threading.RLock()
        self._ids = itertools.count(1)
        self.request_count = 0
        self.requests = []  # (method, path)
        self.faults = []  # [method, regex, fault, remaining]
        self.timeout_delay = 0.6
        self.me = {"id": self.new_id(), "username": "kankanbot", "fullName": "Kankan Bot"}  # the token owner
        self.owner = {"id": self.new_id(), "username": "boardowner", "fullName": "Board Owner"}  # the human
        self.members = {self.me["id"]: self.me, self.owner["id"]: self.owner}
        self.state = {"boards": {}, "lists": {}, "cards": {}, "labels": {}, "comments": {}}
        self.prev_state = copy.deepcopy(self.state)
        self.server = None
        self.thread = None

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self):
        handler = self._make_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    # ---------------------------------------------------------------- helpers
    def new_id(self) -> str:
        return f"{0x5f000000 + next(self._ids):024x}"

    def inject(self, method: str, path_regex: str, fault: str, times: int = 1) -> None:
        with self.lock:
            self.faults.append([method.upper(), re.compile(path_regex), fault, times])

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def add_board(self, name: str = "board", desc: str = "") -> dict:
        with self.lock:
            board = {"id": self.new_id(), "name": name, "desc": desc, "shortLink": _short(), "closed": False,
                     "memberships": [{"idMember": self.owner["id"], "memberType": "admin"},
                                     {"idMember": self.me["id"], "memberType": "normal"}]}
            board["url"] = f"https://trello.com/b/{board['shortLink']}/{name}"
            self.state["boards"][board["id"]] = board
            return board

    def add_list(self, board_id: str, name: str, closed: bool = False) -> dict:
        with self.lock:
            pos = 1024 * (1 + sum(1 for l in self.state["lists"].values() if l["idBoard"] == board_id))
            lst = {"id": self.new_id(), "name": name, "closed": closed, "pos": pos, "idBoard": board_id}
            self.state["lists"][lst["id"]] = lst
            return lst

    def add_label(self, board_id: str, name: str, color: str = "red") -> dict:
        with self.lock:
            label = {"id": self.new_id(), "name": name, "color": color, "idBoard": board_id}
            self.state["labels"][label["id"]] = label
            return label

    def add_card(self, list_id: str, name: str = "card", desc: str = "", pos: float = None, creator: str = None) -> dict:
        with self.lock:
            lst = self.state["lists"][list_id]
            short = _short()
            if pos is None:
                pos = 1 + max([c["pos"] for c in self.state["cards"].values() if c["idList"] == list_id] or [0]) + 1000
            card = {"id": self.new_id(), "name": name, "desc": desc, "idBoard": lst["idBoard"], "idList": list_id,
                    "pos": pos, "idLabels": [], "shortLink": short,
                    "url": f"https://trello.com/c/{short}/1-{re.sub('[^a-z0-9]+', '-', name.lower())}",
                    "closed": False, "dateLastActivity": self._now(), "attachments": [],
                    "idMemberCreator": creator or self.owner["id"]}
            self.state["cards"][card["id"]] = card
            return card

    def add_attachment(self, card_id: str, url: str) -> None:
        with self.lock:
            self.state["cards"][card_id]["attachments"].append({"id": self.new_id(), "url": url})

    def user_move(self, card_id: str, list_id: str) -> None:
        with self.lock:
            self.state["cards"][card_id]["idList"] = list_id

    def user_label(self, card_id: str, label_id: str, add: bool = True) -> None:
        with self.lock:
            labels = self.state["cards"][card_id]["idLabels"]
            if add and label_id not in labels:
                labels.append(label_id)
            elif not add and label_id in labels:
                labels.remove(label_id)

    def user_archive(self, card_id: str) -> None:
        with self.lock:
            self.state["cards"][card_id]["closed"] = True

    def user_delete(self, card_id: str) -> None:
        with self.lock:
            self.state["cards"].pop(card_id, None)

    def add_member(self, username: str) -> dict:
        member = {"id": self.new_id(), "username": username, "fullName": username}
        self.members[member["id"]] = member
        return member

    def user_comment(self, card_id: str, text: str, member_id: str = None) -> dict:
        with self.lock:
            return self._comment(card_id, text, member_id or self.owner["id"])

    def _comment(self, card_id: str, text: str, member_id: str) -> dict:
        member = self.members[member_id]
        comment = {"id": self.new_id(), "idMemberCreator": member_id, "type": "commentCard", "date": self._now(),
                   "data": {"text": text, "card": {"id": card_id}}, "memberCreator": {"id": member_id, "username": member["username"]}}
        self.state["comments"][comment["id"]] = comment
        return comment

    def card_comments(self, card_id: str) -> list:
        with self.lock:
            return [c["data"]["text"] for c in sorted(self.state["comments"].values(), key=lambda c: c["id"])
                    if c["data"]["card"]["id"] == card_id]

    def card_list(self, card_id: str) -> str:
        return self.state["cards"][card_id]["idList"]

    def list_by_name(self, board_id: str, name: str) -> list:
        return [l for l in self.state["lists"].values() if l["idBoard"] == board_id and l["name"] == name and not l["closed"]]

    def label_by_name(self, board_id: str, name: str) -> list:
        return [l for l in self.state["labels"].values() if l["idBoard"] == board_id and l["name"] == name]

    # ---------------------------------------------------------------- HTTP
    def _make_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self, method):
                parsed = urlparse(self.path)
                params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = self.rfile.read(length)
                    try:
                        params.update(json.loads(body))
                    except ValueError:
                        params.update({k: v[-1] for k, v in parse_qs(body.decode()).items()})
                auth = self.headers.get("Authorization", "")
                with fake.lock:
                    fake.request_count += 1
                    fake.requests.append((method, parsed.path))
                    fault = None
                    for entry in fake.faults:
                        if entry[3] > 0 and entry[0] == method and entry[1].search(parsed.path):
                            entry[3] -= 1
                            fault = entry[2]
                            break
                if fault == "timeout":
                    time.sleep(fake.timeout_delay)
                    self.close_connection = True
                    return
                if fault in ("500", "429"):
                    headers = {"Retry-After": "1"} if fault == "429" else {}
                    return self._send(int(fault), {"message": "injected"}, headers)
                if f'oauth_consumer_key="{fake.key}"' not in auth or f'oauth_token="{fake.token}"' not in auth:
                    return self._send(401, "invalid token")
                with fake.lock:
                    if fault == "stale_read" and method == "GET":
                        saved = fake.state
                        fake.state = fake.prev_state
                        try:
                            status, payload = fake.route(method, parsed.path, params)
                        finally:
                            fake.state = saved
                    else:
                        if method != "GET":
                            fake.prev_state = copy.deepcopy(fake.state)
                        status, payload = fake.route(method, parsed.path, params)
                if fault == "late_success":
                    time.sleep(fake.timeout_delay)
                    self.close_connection = True
                    return
                self._send(status, payload)

            def _send(self, status, payload, headers=None):
                data = json.dumps(payload).encode() if not isinstance(payload, str) else payload.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

        return Handler

    # ---------------------------------------------------------------- routing
    def _find(self, kind: str, id_or_short: str):
        obj = self.state[kind].get(id_or_short)
        if obj is None:
            for candidate in self.state[kind].values():
                if candidate.get("shortLink") == id_or_short:
                    return candidate
        return obj

    @staticmethod
    def _card_out(card: dict, attachments: bool) -> dict:
        out = {k: v for k, v in card.items() if k != "attachments"}
        if attachments:
            out["attachments"] = [dict(a) for a in card["attachments"]]
        return out

    def route(self, method: str, path: str, p: dict):
        parts = path.strip("/").split("/")
        if parts[0] != "1":
            return 404, "not found"
        parts = parts[1:]
        st = self.state
        if parts == ["members", "me"] and method == "GET":
            return 200, self.me
        if parts[0] == "members" and len(parts) == 2 and method == "GET":
            for member in self.members.values():
                if member["username"] == parts[1] or member["id"] == parts[1]:
                    return 200, member
            return 404, "member not found"
        if parts[0] == "boards":
            board = self._find("boards", parts[1])
            if board is None:
                return 404, "board not found"
            if len(parts) == 2 and method == "GET":
                out = {"id": board["id"], "name": board["name"], "desc": board["desc"],
                       "shortLink": board["shortLink"], "url": board["url"]}
                if p.get("lists"):
                    out["lists"] = [{k: l[k] for k in ("id", "name", "closed", "pos")}
                                    for l in st["lists"].values() if l["idBoard"] == board["id"]
                                    and (p["lists"] == "all" or not l["closed"])]
                if p.get("cards"):
                    out["cards"] = [self._card_out(c, p.get("card_attachments") == "true")
                                    for c in st["cards"].values() if c["idBoard"] == board["id"] and not c["closed"]]
                if p.get("labels"):
                    limit = int(p.get("labels_limit", 50))
                    out["labels"] = [{k: l[k] for k in ("id", "name", "color")}
                                     for l in st["labels"].values() if l["idBoard"] == board["id"]][:limit]
                return 200, out
            if len(parts) == 2 and method == "PUT":
                if "desc" in p:
                    board["desc"] = p["desc"]
                return 200, board
            if len(parts) == 3 and parts[2] == "memberships" and method == "GET":
                out = []
                for m in board.get("memberships", []):
                    member = self.members[m["idMember"]]
                    out.append({"idMember": m["idMember"], "memberType": m["memberType"],
                                "member": {"id": member["id"], "username": member["username"]}})
                return 200, out
            if len(parts) == 3 and parts[2] == "labels" and method == "GET":
                labels = sorted((l for l in st["labels"].values() if l["idBoard"] == board["id"]), key=lambda l: l["id"])
                if p.get("before"):
                    labels = [l for l in labels if l["id"] < p["before"]]
                labels = labels[-int(p.get("limit", 50)):]
                return 200, [{k: l[k] for k in ("id", "name", "color")} for l in reversed(labels)]
            if len(parts) == 3 and parts[2] == "lists" and method == "GET":
                return 200, [l for l in st["lists"].values() if l["idBoard"] == board["id"]]
        if parts[0] == "lists":
            if len(parts) == 1 and method == "POST":
                lst = self.add_list(p["idBoard"], p["name"])
                if p.get("pos") == "top":
                    lst["pos"] = 0.5
                return 200, lst
            lst = st["lists"].get(parts[1])
            if lst is None:
                return 404, "list not found"
            if len(parts) == 2 and method == "GET":
                return 200, lst
            if len(parts) == 3 and parts[2] == "closed" and method == "PUT":
                lst["closed"] = str(p.get("value")).lower() == "true"
                return 200, lst
        if parts[0] == "labels":
            if len(parts) == 1 and method == "POST":
                return 200, self.add_label(p["idBoard"], p["name"], p.get("color"))
            if method == "DELETE":
                if st["labels"].pop(parts[1], None) is None:
                    return 404, "label not found"
                for card in st["cards"].values():
                    if parts[1] in card["idLabels"]:
                        card["idLabels"].remove(parts[1])
                return 200, {}
        if parts[0] == "cards":
            if len(parts) == 1 and method == "POST":
                if p.get("pos") == "top":
                    others = [c["pos"] for c in st["cards"].values() if c["idList"] == p["idList"]]
                    return 200, self.add_card(p["idList"], p.get("name", ""), p.get("desc", ""), pos=min(others or [1024]) / 2,
                                              creator=self.me["id"])
                return 200, self.add_card(p["idList"], p.get("name", ""), p.get("desc", ""), creator=self.me["id"])
            card = self._find("cards", parts[1])
            if card is None:
                return 404, "The requested resource was not found."
            if len(parts) == 2 and method == "GET":
                return 200, self._card_out(card, p.get("attachments") == "true")
            if len(parts) == 2 and method == "PUT":
                if "idList" in p:
                    card["idList"] = p["idList"]
                    if p.get("pos") == "top":
                        others = [c["pos"] for c in st["cards"].values() if c["idList"] == p["idList"] and c is not card]
                        card["pos"] = min(others or [1024]) / 2
                if "closed" in p:
                    card["closed"] = str(p["closed"]).lower() == "true"
                card["dateLastActivity"] = self._now()
                return 200, self._card_out(card, False)
            if len(parts) == 2 and method == "DELETE":
                del st["cards"][card["id"]]
                return 200, {}
            if parts[2] == "idLabels":
                if method == "POST":
                    if p["value"] in card["idLabels"]:
                        return 400, "that label is already on the card"
                    if p["value"] not in st["labels"]:
                        return 400, "invalid value for value"
                    card["idLabels"].append(p["value"])
                    return 200, card["idLabels"]
                if method == "DELETE":
                    if parts[3] not in card["idLabels"]:
                        return 400, "that label is not on the card"
                    card["idLabels"].remove(parts[3])
                    return 200, card["idLabels"]
            if parts[2:] == ["actions", "comments"] and method == "POST":
                return 200, self._comment(card["id"], p["text"], self.me["id"])
            if parts[2:] == ["actions"] and method == "GET" and "createCard" in p.get("filter", ""):
                creator = self.members[card["idMemberCreator"]]
                return 200, [{"id": card["id"], "type": "createCard", "idMemberCreator": creator["id"],
                              "memberCreator": {"id": creator["id"], "username": creator["username"]}}]
            if parts[2:] == ["actions"] and method == "GET":
                comments = sorted((c for c in st["comments"].values() if c["data"]["card"]["id"] == card["id"]),
                                  key=lambda c: c["id"], reverse=True)
                if p.get("since"):
                    comments = [c for c in comments if c["id"] > p["since"]]
                if p.get("before"):
                    comments = [c for c in comments if c["id"] < p["before"]]
                return 200, comments[:int(p.get("limit", 50))]
            if parts[2:] == ["attachments"] and method == "POST":
                self.add_attachment(card["id"], p["url"])
                return 200, card["attachments"][-1]
        return 404, "not found"
