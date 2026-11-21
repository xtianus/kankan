"""Live check of ``kankan destroy`` on a disposable board (destroy spec T§8), never on the shared test board.

The test creates one board named ``ke-destroy-<run>``, binds a temporary clone to it with the Python entry points,
seeds it, runs ``destroy``, checks what is gone and what remains, runs ``init`` again and checks the adoption by
name, and deletes exactly that board (by the ID Trello returned) in ``finally``.
"""

import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import pytest

from kankan import clock, destroy, seats
from kankan import init as init_mod
from kankan import trello as trello_mod
from kankan.config import REGISTRY_START, save_config
from kankan.errors import KankanError, Status
from kankan.init import LABELS, init
from kankan.labels import resume_label_name
from kankan.project import Project
from kankan.trello import TrelloClient
from tests.helpers import NoTmux

SEAT = "claude-1"
DESC = "Disposable board of the kankan destroy live check."
KEPT = ("backlog", "done", "blocked", "qa_list", "settings_list")


def _answers(question: str) -> str:
    return ""  # the defaults: no test command, the default integration branch, adopt every list found by name


def _ask(question: str):
    raise AssertionError(f"destroy asked a question despite --confirm: {question}")


def _status(client: TrelloClient, path: str) -> int:
    return client._request("GET", path, {"fields": "id"}).status


def _boards_named(client: TrelloClient, name: str) -> list:
    """The member's boards whose name is exactly ``name`` (the run's unique name)."""
    rows = client._ok("GET", "/1/members/me/boards", {"fields": "name,shortUrl"})
    return [row for row in rows if row.get("name") == name]


def _create_board(client: TrelloClient, name: str) -> dict:
    """One ``POST /1/boards``, never replayed: after an ambiguous failure (no answer, a timeout, a 5xx) the
    board is looked up by its unique name instead of being created again."""
    try:
        return client._ok("POST", "/1/boards", {"name": name, "desc": DESC, "defaultLists": "false"},
                          retry=False)
    except KankanError as err:
        if err.status != Status.NETWORK_ERROR:
            raise
        found = _boards_named(client, name)
        if len(found) > 1:
            ids = ", ".join(row["id"] for row in found)
            raise AssertionError(f"creating {name} failed and {len(found)} boards carry that name ({ids}): "
                                 "ownership is ambiguous; all listed boards were left untouched") from None
        if not found:
            raise
        return found[0]


def _delete_board(client: TrelloClient, name: str, board: Optional[dict]) -> None:
    """Delete the board this run owns: the one creation returned or recovered, never another board."""
    if board is None:
        return
    status = client._request("DELETE", f"/1/boards/{board['id']}").status
    assert status == 200, f"the disposable board {name} ({board['id']}) was not deleted: HTTP {status}"


def _archive(client: TrelloClient, card_id: str) -> None:
    client._ok("PUT", f"/1/cards/{card_id}", body={"closed": True})


@pytest.mark.trello_live
def test_destroy_then_init_live(live_client, git_repo, tmp_path, monkeypatch, capsys):
    client = live_client
    # the autouse isolated_home fixture tunes these for the fake server; real Trello needs the real ones
    monkeypatch.setattr(init_mod, "clock", clock)
    monkeypatch.setattr(trello_mod._BUCKET, "limit", 90)
    monkeypatch.delenv("KANKAN_TRELLO_URL", raising=False)
    monkeypatch.setattr(seats, "pane_sid", lambda: None)
    name = f"ke-destroy-{int(time.time())}-{secrets.token_hex(3)}"
    facts = {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "board": name}
    created = None
    try:
        created = _create_board(client, name)
        board_id = created["id"]
        assert created["name"] == name
        cfg, _ = init(git_repo, created["shortUrl"], prompt=_answers, say=lambda text: None)
        project = Project.open(git_repo)
        project.cfg.worktree_root = str(tmp_path / "worktrees")
        save_config(project.paths, project.cfg)
        seats.ensure_created(Project.open(git_repo), SEAT, "claude")
        cfg = Project.open(git_repo).cfg
        seat = cfg.seats[SEAT]
        seat_lists = [seat.in_progress, seat.implemented, seat.review]
        kept_lists = {attr: getattr(cfg, attr) for attr in KEPT}
        label_ids = {attr: getattr(cfg, attr) for attr, _, _ in LABELS}
        assert all(kept_lists.values()) and all(seat_lists) and all(label_ids.values())

        # seed: a Backlog card with an upload, a link, a comment, a checklist and a Resume label
        card = client.create_card(cfg.backlog, f"{name} backlog")
        client.upload_attachment(card.id, "destroy probe.txt", b"destroy probe\n", "text/plain")
        client.attach_link(card.id, "https://example.com/destroy", "destroy link")
        client.add_comment(card.id, "destroy live comment", f"{name}-c")
        checklist = client.create_checklist(card.id, "steps")
        client.create_check_item(checklist.id, "one", False, 1024)
        resume = client.create_label(board_id, resume_label_name(datetime.now(timezone.utc)), "sky")
        client.add_label(card.id, resume.id)
        in_seat = client.create_card(seat.in_progress, f"{name} in progress")
        # three archived cards in Done (the paging probe), one open card in Done
        archived = []
        for index in range(3):
            done_card = client.create_card(cfg.done, f"{name} archived {index}")
            _archive(client, done_card.id)
            archived.append(done_card.id)
        open_done = client.create_card(cfg.done, f"{name} done")
        # a list kankan does not know, with a card
        ideas = client.create_list(board_id, "Ideas")
        idea = client.create_card(ideas.id, f"{name} idea")

        # archived-cards probe (T§6): the raw pages, then closed_cards: one request, a full page (limit=1 on three cards) refuses
        closed = f"/1/boards/{board_id}/cards/closed"
        facts["archived_created_order"] = archived
        facts["raw_unpaged_order"] = [row["id"] for row in client._ok("GET", closed, {"fields": "id"})]
        pages, before = [], None
        for _ in range(4):
            params = {"limit": "1", "fields": "id", **({"before": before} if before else {})}
            rows = client._ok("GET", closed, params)
            pages.append([row["id"] for row in rows])
            if not rows:
                break
            before = rows[-1]["id"]
        facts["raw_pages_limit_1_before_last"] = pages
        facts["raw_limit_2_before_newest"] = [row["id"] for row in client._ok(
            "GET", closed, {"limit": "2", "fields": "id", "before": pages[0][0]})] if pages[0] else []
        try:
            client.closed_cards(board_id, limit=1)
            facts["closed_cards_limit_1"] = "returned"
        except KankanError as err:
            facts["closed_cards_limit_1"] = err.status.value if hasattr(err.status, "value") else str(err.status)
            full_page_status = err.status
        else:
            full_page_status = None
        whole = {c.id for c in client.closed_cards(board_id, limit=1000)}
        facts["closed_cards_limit_1000"] = sorted(whole)
        assert full_page_status == Status.NEEDS_ACTION, facts
        assert whole == set(archived), facts

        # every card destroy should delete: the open cards of kankan's lists (seeded and settings cards)
        snap = client.snapshot(board_id)
        targets = set(kept_lists.values()) | set(seat_lists)
        doomed = {c.id for c in snap.cards.values() if c.list_id in targets} | set(archived)
        assert {card.id, in_seat.id, open_done.id} <= doomed and idea.id not in doomed

        # destroy reads the archived cards in one request of 1000
        ctx = destroy.Ctx.open(git_repo)
        report = destroy.destroy(ctx, tmux=NoTmux(), ask=_ask, say=lambda text: None,
                                 confirm=[f"destroy {created['name']}"])
        assert not report.get("cancelled") and not report.get("nothing_to_destroy"), report

        # what is gone
        statuses = {card_id: _status(client, f"/1/cards/{card_id}") for card_id in sorted(doomed)}
        facts["deleted_card_status"] = sorted(set(statuses.values()))
        assert set(statuses.values()) == {404}, statuses
        facts["checklist_status"] = _status(client, f"/1/checklists/{checklist.id}")
        facts["resume_label_status"] = _status(client, f"/1/labels/{resume.id}")
        assert facts["checklist_status"] == 404
        assert facts["resume_label_status"] == 404
        assert all(client.list_info(list_id).closed for list_id in seat_lists)
        # the registry block is gone and the user's description is kept exactly
        assert client.board(board_id).get("desc") == DESC
        assert destroy.Ctx.open(git_repo).cfg is None
        # what remains
        kept_idea = client.card(idea.id)
        assert kept_idea is not None and kept_idea.list_id == ideas.id and not kept_idea.closed
        assert not client.list_info(ideas.id).closed
        assert not any(client.list_info(list_id).closed for list_id in kept_lists.values())
        assert all(_status(client, f"/1/labels/{label_id}") == 200 for label_id in label_ids.values())

        # init again adopts the same lists and labels by name
        again, _ = init(git_repo, created["shortUrl"], prompt=_answers, say=lambda text: None)
        assert {attr: getattr(again, attr) for attr in KEPT} == kept_lists
        assert {attr: getattr(again, attr) for attr, _, _ in LABELS} == label_ids
        assert REGISTRY_START in client.board(board_id)["desc"]
    finally:
        with capsys.disabled():
            print(f"\ndestroy live facts: {facts}")
        _delete_board(client, name, created)


class _StubClient:
    """Records the board calls of the creation and cleanup helpers; ``post`` is the POST outcome."""

    def __init__(self, post, boards):
        self.post, self.boards, self.calls = post, boards, []

    def _ok(self, method, path, params=None, body=None, retry=True):
        self.calls.append((method, path, retry))
        if method == "POST":
            if isinstance(self.post, Exception):
                raise self.post
            return self.post
        return self.boards

    def _request(self, method, path, params=None, body=None, retry=True):
        self.calls.append((method, path, retry))
        return trello_mod.Response(200, {})


LOST = KankanError(Status.NETWORK_ERROR, "Trello POST /1/boards: connection failed: TimeoutError")
NAME = "ke-destroy-1-abc"


def test_cleanup_deletes_only_the_returned_board():
    client = _StubClient({"id": "b1", "name": NAME}, [{"id": "b1", "name": NAME}, {"id": "b2", "name": NAME}])
    board = _create_board(client, NAME)
    _delete_board(client, NAME, board)
    assert client.calls == [("POST", "/1/boards", False), ("DELETE", "/1/boards/b1", True)]


def test_ambiguous_creation_recovers_the_single_match_without_a_second_post():
    client = _StubClient(LOST, [{"id": "other", "name": "kankan"}, {"id": "b1", "name": NAME}])
    board = _create_board(client, NAME)
    _delete_board(client, NAME, board)
    assert [c for c in client.calls if c[0] == "POST"] == [("POST", "/1/boards", False)]
    assert client.calls[-1] == ("DELETE", "/1/boards/b1", True)


def test_ambiguous_creation_with_several_matches_deletes_none():
    client = _StubClient(LOST, [{"id": "b1", "name": NAME}, {"id": "b2", "name": NAME}])
    with pytest.raises(AssertionError) as failure:
        _create_board(client, NAME)
    message = str(failure.value)
    assert "(b1, b2)" in message and "ownership is ambiguous; all listed boards were left untouched" in message
    assert "delete" not in message.lower()
    _delete_board(client, NAME, None)
    assert [c[0] for c in client.calls] == ["POST", "GET"]


def test_ambiguous_creation_without_a_match_reraises_and_deletes_none():
    client = _StubClient(LOST, [{"id": "other", "name": "kankan"}])
    with pytest.raises(KankanError):
        _create_board(client, NAME)
    _delete_board(client, NAME, None)
    assert [c[0] for c in client.calls] == ["POST", "GET"]
