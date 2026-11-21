"""``kankan destroy``: board inventory (destroy spec T§3, Board bullet, and T§6)."""

import pytest

from kankan import destroy, labels, messages
from kankan.config import REGISTRY_START, SeatConfig, load_config, save_config, write_registry_block
from kankan.trello import TrelloClient
from kankan.errors import KankanError, Status
from tests.helpers import add_qa_list

SEAT_STEPS = ("In Progress", "Implemented", "Review")


def setup_board(project, fake):
    """A work seat ``claude-1`` with its lists, a Q&A seat (no lists), a ``Q&A`` list, a ``kankan`` settings list
    and a foreign ``Ideas`` list; returns the list IDs by name."""
    board_id = project.cfg.board_id
    ids = {"Q&A": add_qa_list(project, fake)}
    ids["kankan"] = fake.add_list(board_id, "kankan")["id"]
    for step in SEAT_STEPS:
        ids[f"claude-1 · {step}"] = fake.add_list(board_id, f"claude-1 · {step}")["id"]
    ids["Ideas"] = fake.add_list(board_id, "Ideas")["id"]
    cfg = load_config(project.paths)
    ids.update(Backlog=cfg.backlog, Done=cfg.done, Blocked=cfg.blocked)
    cfg.settings_list = ids["kankan"]
    cfg.seats["claude-1"] = SeatConfig(type="claude", in_progress=ids["claude-1 · In Progress"],
                                       implemented=ids["claude-1 · Implemented"], review=ids["claude-1 · Review"])
    cfg.seats["codex-9"] = SeatConfig(type="codex", role="qa")
    save_config(project.paths, cfg)
    return ids


def plan_of(project):
    return destroy.board_plan(destroy.Ctx.open(project.paths.repo))


def set_desc(project, fake, desc):
    fake.state["boards"][project.cfg.board_id]["desc"] = desc


SECRET_NAME = "Zanzibar Quokka Board 7f3e"


def secret_name(project, fake):
    fake.state["boards"][project.cfg.board_id]["name"] = SECRET_NAME


def edit_route(fake, monkeypatch, edit):
    """Let ``edit(method, path, params, payload)`` change each fake response payload in place."""
    original = fake.route

    def route(method, path, params):
        status, payload = original(method, path, params)
        edit(method, path, params, payload)
        return status, payload
    monkeypatch.setattr(fake, "route", route)


def is_board_read(path, params):
    """``TrelloClient.board`` (fields only), as opposed to the nested read (``lists`` asked)."""
    return path.count("/") == 3 and not params.get("lists")


def is_nested_read(path, params):
    return path.count("/") == 3 and bool(params.get("lists"))


def assert_needs_action(project, message):
    with pytest.raises(KankanError) as err:
        plan_of(project)
    assert err.value.status == Status.NEEDS_ACTION
    assert err.value.message == message
    return err.value


def only_gets(fake):
    assert {method for method, _ in fake.requests} == {"GET"}


# --- resume_time --------------------------------------------------------------------------------------------

def test_resume_time_strict():
    assert labels.resume_time("Resume 2026-11-20 10:00").strftime("%Y-%m-%d %H:%M") == "2026-11-20 10:00"
    assert labels.resume_time("Resume 2026-11-20 10:00").tzinfo is not None
    for name in ("Resume hiring", "Resume 2026-1-2 3:04", "XResume 2026-11-20 10:00", "resume 2026-11-20 10:00",
                 "Resume 2026-11-20 10:00 ", "2026-11-20 10:00"):
        assert labels.resume_time(name) is None, name


# --- Inventory ----------------------------------------------------------------------------------------------

def test_board_plan_cards_open_and_archived(kproject, fake_trello):
    ids = setup_board(kproject, fake_trello)
    secret_name(kproject, fake_trello)
    backlog = [fake_trello.add_card(ids["Backlog"], f"b{i}")["id"] for i in range(3)]
    archived = fake_trello.add_card(ids["Done"], "old")["id"]
    fake_trello.user_archive(archived)
    review = [fake_trello.add_card(ids["claude-1 · Review"], f"r{i}")["id"] for i in range(2)]
    settings = fake_trello.add_card(ids["kankan"], "Settings")["id"]
    idea = fake_trello.add_card(ids["Ideas"], "idea")["id"]
    plan, reason = plan_of(kproject)
    assert reason is None
    assert {key: sorted(value) for key, value in plan.cards.items() if value} == {
        ids["Backlog"]: sorted(backlog), ids["Done"]: [archived], ids["claude-1 · Review"]: sorted(review),
        ids["kankan"]: [settings]}
    assert all(idea not in value for value in plan.cards.values())
    assert sorted(plan.seat_lists) == sorted(ids[f"claude-1 · {step}"] for step in SEAT_STEPS)
    assert plan.board_id == kproject.cfg.board_id and plan.short == kproject.cfg.board_short
    assert plan.name == SECRET_NAME and plan.has_block is True
    assert plan.list_names[ids["Backlog"]] == "Backlog" and plan.list_names[ids["claude-1 · Review"]] == "claude-1 · Review"
    assert sorted(plan.kept_lists) == sorted((ids[name], name) for name in ("Backlog", "Done", "Blocked", "Q&A", "kankan"))
    assert SECRET_NAME not in repr(plan)  # the board name is never shown
    assert any(path.endswith("/cards/closed") for _, path in fake_trello.requests)
    only_gets(fake_trello)


def test_board_plan_closed_seat_list_not_archived_again(kproject, fake_trello):
    ids = setup_board(kproject, fake_trello)
    fake_trello.state["lists"][ids["claude-1 · Implemented"]]["closed"] = True
    plan, _ = plan_of(kproject)
    assert sorted(plan.seat_lists) == sorted(ids[f"claude-1 · {step}"] for step in ("In Progress", "Review"))


def test_board_plan_resume_labels(kproject, fake_trello):
    ids = setup_board(kproject, fake_trello)
    board_id = kproject.cfg.board_id
    unused = fake_trello.add_label(board_id, "Resume 2026-11-20 10:00")["id"]
    on_target = fake_trello.add_label(board_id, "Resume 2026-11-22 08:00")["id"]
    others = [fake_trello.add_label(board_id, name)["id"]
              for name in ("Resume hiring", "Resume 2026-1-2 3:04", "XResume 2026-11-20 10:00")]
    kept = fake_trello.add_label(board_id, "Resume 2026-11-21 09:00")["id"]
    card = fake_trello.add_card(ids["Backlog"], "waiting")["id"]
    fake_trello.user_label(card, on_target)
    outside = fake_trello.add_card(ids["Ideas"], "idea")["id"]
    fake_trello.user_label(outside, kept)
    fake_trello.user_label(outside, others[0])
    fake_trello.user_archive(outside)
    plan, _ = plan_of(kproject)
    assert sorted(plan.labels) == sorted([unused, on_target])
    assert plan.kept_labels == ["Resume 2026-11-21 09:00"]
    only_gets(fake_trello)


def test_board_plan_duplicate_names(kproject, fake_trello):
    ids = setup_board(kproject, fake_trello)
    twin = fake_trello.add_list(kproject.cfg.board_id, "Backlog")["id"]
    ours = fake_trello.add_card(ids["Backlog"], "ours")["id"]
    fake_trello.add_card(twin, "theirs")
    plan, _ = plan_of(kproject)
    assert plan.cards[ids["Backlog"]] == [ours]
    assert twin not in plan.cards and twin not in [ident for ident, _ in plan.kept_lists]


def test_board_plan_renamed_list(kproject, fake_trello):
    ids = setup_board(kproject, fake_trello)
    fake_trello.state["lists"][ids["Backlog"]]["name"] = "Todo"
    plan, _ = plan_of(kproject)
    assert (ids["Backlog"], "Todo") in plan.kept_lists
    assert plan.list_names[ids["Backlog"]] == "Todo"


# --- Ownership ----------------------------------------------------------------------------------------------

def test_board_plan_other_clone(kproject, fake_trello):
    setup_board(kproject, fake_trello)
    set_desc(kproject, fake_trello, write_registry_block("test board", {"registry-id": "someone-else"}))
    assert plan_of(kproject) == (None, "another clone")


def test_board_plan_absent_block_ours(kproject, fake_trello):
    ids = setup_board(kproject, fake_trello)
    set_desc(kproject, fake_trello, "test board")
    card = fake_trello.add_card(ids["Backlog"], "left")["id"]
    plan, reason = plan_of(kproject)
    assert reason is None and plan.has_block is False and plan.cards[ids["Backlog"]] == [card]


def test_board_plan_malformed_block(kproject, fake_trello):
    setup_board(kproject, fake_trello)
    secret_name(kproject, fake_trello)
    set_desc(kproject, fake_trello, f"{SECRET_NAME}\n\n{REGISTRY_START}\nregistry-id: x\n")
    err = assert_needs_action(kproject, messages.DESTROY_REGISTRY_MALFORMED)
    assert SECRET_NAME not in str(err) and SECRET_NAME not in repr(err.data)


# --- Errors -------------------------------------------------------------------------------------------------

def test_board_plan_no_configuration(git_repo, fake_trello):
    assert destroy.board_plan(destroy.Ctx.open(git_repo)) == (None, "no configuration")


def test_board_plan_no_credentials(kproject, fake_trello, monkeypatch):
    monkeypatch.delenv("KANKAN_TRELLO_KEY")
    monkeypatch.delenv("KANKAN_TRELLO_TOKEN")
    assert plan_of(kproject) == (None, "unreadable: credentials")
    assert fake_trello.requests == []


@pytest.mark.parametrize("fault", ["401", "403", "404"])
def test_board_plan_unreadable(kproject, fake_trello, fault):
    setup_board(kproject, fake_trello)
    fake_trello.inject("GET", rf"^/1/boards/{kproject.cfg.board_id}$", fault)
    assert plan_of(kproject) == (None, f"unreadable: {fault}")


def test_board_plan_missing_board(kproject, fake_trello):
    setup_board(kproject, fake_trello)
    del fake_trello.state["boards"][kproject.cfg.board_id]
    assert plan_of(kproject) == (None, "unreadable: 404")


def assert_transient(project):
    with pytest.raises(KankanError) as err:
        plan_of(project)
    assert err.value.status == Status.NEEDS_ACTION
    assert err.value.message == messages.DESTROY_TRELLO_TRANSIENT
    assert err.value.data is None


@pytest.mark.parametrize("fault", ["503", "429", "timeout"])
def test_transient_error_is_not_local_only(kproject, fake_trello, virtual_time, fault):
    setup_board(kproject, fake_trello)
    fake_trello.timeout_delay = 0.6
    fake_trello.inject("GET", rf"^/1/boards/{kproject.cfg.board_id}$", fault, times=10)
    assert_transient(kproject)
    assert not any(path.endswith("/cards/closed") for _, path in fake_trello.requests)
    fake_trello.faults.clear()
    fake_trello.inject("GET", r"/cards/closed$", fault, times=10)
    assert_transient(kproject)
    assert any(path.endswith("/cards/closed") for _, path in fake_trello.requests)
    only_gets(fake_trello)


def test_full_page_of_archived_cards_refuses(kproject, fake_trello, monkeypatch):
    setup_board(kproject, fake_trello)
    lst = fake_trello.add_list(kproject.cfg.board_id, "Archive")
    for i in range(3):
        fake_trello.add_card(lst["id"], f"a{i}")["closed"] = True
    original = TrelloClient.closed_cards
    monkeypatch.setattr(TrelloClient, "closed_cards", lambda self, board_id: original(self, board_id, limit=3))
    with pytest.raises(KankanError) as err:
        plan_of(kproject)
    assert err.value.status == Status.NEEDS_ACTION
    assert err.value.message == messages.TOO_MANY_ARCHIVED_CARDS.format(limit=3)
    only_gets(fake_trello)


@pytest.mark.parametrize("fault", ["404", "500"])
def test_failed_archived_read_is_not_local_only(kproject, fake_trello, virtual_time, fault):
    """A 404 after the board was read is not an unreadable board: nothing may fall back to the local state only."""
    setup_board(kproject, fake_trello)
    fake_trello.inject("GET", r"/cards/closed$", fault, times=10)
    assert_transient(kproject)


# --- Strict reads -------------------------------------------------------------------------------------------

def test_board_plan_empty_desc_is_absent_block(kproject, fake_trello):
    setup_board(kproject, fake_trello)
    set_desc(kproject, fake_trello, "")
    plan, reason = plan_of(kproject)
    assert reason is None and plan.has_block is False


@pytest.mark.parametrize("change", [{"desc": None}, {"desc": 3}, {"name": None}, {"name": ""}, {"name": 7},
                                    {"name": "   "}, {"name": "\t\n"}])
def test_board_read_incomplete(kproject, fake_trello, monkeypatch, change):
    """A board read without a string description or a usable name is no evidence of ownership."""
    setup_board(kproject, fake_trello)
    secret_name(kproject, fake_trello)

    def edit(method, path, params, payload):
        if is_board_read(path, params):
            for key, value in change.items():
                if value is None:
                    payload.pop(key, None)
                else:
                    payload[key] = value
    edit_route(fake_trello, monkeypatch, edit)
    err = assert_needs_action(kproject, messages.DESTROY_BOARD_INCOMPLETE)
    assert SECRET_NAME not in str(err)


@pytest.mark.parametrize("desc", [
    write_registry_block("test board", {"registry-id": "someone-else"}), "test board",
    f"test board\n\n{REGISTRY_START}\n"])
def test_nested_read_disagrees_on_ownership(kproject, fake_trello, monkeypatch, desc):
    setup_board(kproject, fake_trello)

    def edit(method, path, params, payload):
        if is_nested_read(path, params):
            payload["desc"] = desc
    edit_route(fake_trello, monkeypatch, edit)
    assert_needs_action(kproject, messages.DESTROY_BOARD_CHANGED)


def drop_cards(params, payload):
    payload.pop("cards")


def drop_id_labels(params, payload):
    payload["cards"][0].pop("idLabels")


def drop_id_list(params, payload):
    payload["cards"][0]["idList"] = None


def drop_desc(params, payload):
    payload.pop("desc")


def drop_labels(params, payload):
    payload.pop("labels")


def drop_lists(params, payload):
    payload.pop("lists")


def drop_list_closed(params, payload):
    payload["lists"][0].pop("closed")


def drop_label_name(params, payload):
    payload["labels"][0].pop("name")


@pytest.mark.parametrize("drop", [drop_cards, drop_id_labels, drop_id_list, drop_desc, drop_labels, drop_lists,
                                  drop_list_closed, drop_label_name])
def test_nested_read_incomplete(kproject, fake_trello, monkeypatch, drop):
    """An open card in ``Ideas`` keeps its Resume label only if the read shows it: a missing field stops."""
    ids = setup_board(kproject, fake_trello)
    label = fake_trello.add_label(kproject.cfg.board_id, "Resume 2026-11-21 09:00")["id"]
    card = fake_trello.add_card(ids["Ideas"], "idea")["id"]
    fake_trello.user_label(card, label)

    def edit(method, path, params, payload):
        if is_nested_read(path, params):
            drop(params, payload)
    edit_route(fake_trello, monkeypatch, edit)
    assert_needs_action(kproject, messages.DESTROY_BOARD_INCOMPLETE)


def test_card_moved_and_archived_between_reads(kproject, fake_trello, monkeypatch):
    """Open in ``Backlog`` for the nested read, archived in ``Ideas`` for the archived read: no side wins."""
    ids = setup_board(kproject, fake_trello)
    label = fake_trello.add_label(kproject.cfg.board_id, "Resume 2026-11-21 09:00")["id"]
    card = fake_trello.add_card(ids["Backlog"], "moving")["id"]
    original = fake_trello.route

    def route(method, path, params):
        if path.endswith("/cards/closed"):
            row = fake_trello.state["cards"][card]
            row.update(idList=ids["Ideas"], closed=True, idLabels=[label])
        return original(method, path, params)
    monkeypatch.setattr(fake_trello, "route", route)
    assert_needs_action(kproject, messages.DESTROY_BOARD_CHANGED)


def test_card_labels_differ_between_reads(kproject, fake_trello, monkeypatch):
    ids = setup_board(kproject, fake_trello)
    label = fake_trello.add_label(kproject.cfg.board_id, "Resume 2026-11-21 09:00")["id"]
    card = fake_trello.add_card(ids["Ideas"], "idea")["id"]
    fake_trello.user_archive(card)

    def edit(method, path, params, payload):
        if is_nested_read(path, params):  # the same card, open and without the label
            payload["cards"].append({"id": card, "idList": ids["Ideas"], "idLabels": []})
        if path.endswith("/cards/closed"):
            payload[0]["idLabels"] = [label]
    edit_route(fake_trello, monkeypatch, edit)
    assert_needs_action(kproject, messages.DESTROY_BOARD_CHANGED)


@pytest.mark.parametrize("err, expected", [
    (KankanError(Status.CONFIG_ERROR, "Trello refused the credentials (401) on GET /1/boards/x"), "401"),
    (KankanError(Status.CONFIG_ERROR, "Trello refused the credentials (403) on GET /1/boards/x"), "403"),
    (KankanError(Status.CONFIG_ERROR, "Trello refused the credentials (400) on GET /1/boards/x"), None),
    (KankanError(Status.CONFIG_ERROR, "something else"), None),
    (KankanError(Status.CONFIG_ERROR, "x", {"status": 401}), "401"),
    (KankanError(Status.CONFIG_ERROR, "x (401)", {"status": 500}), None),
    (KankanError(Status.TRELLO_ERROR, "x", {"status": 404}), "404"),
    (KankanError(Status.TRELLO_ERROR, "x (401)", {"status": 400}), None),
    (KankanError(Status.NETWORK_ERROR, "x"), None),
])
def test_unreadable_status(err, expected):
    assert destroy._unreadable_status(err) == expected


def duplicate_label_rows(fake, monkeypatch, label_id, names):
    """The nested read lists ``label_id`` once per name, in order."""
    def edit(method, path, params, payload):
        if is_nested_read(path, params):
            payload["labels"] = [row for row in payload["labels"] if row["id"] != label_id]
            payload["labels"] += [{"id": label_id, "name": name, "color": None} for name in names]
    edit_route(fake, monkeypatch, edit)


def test_label_rows_same_id_different_names(kproject, fake_trello, monkeypatch):
    setup_board(kproject, fake_trello)
    label = fake_trello.add_label(kproject.cfg.board_id, "Roadmap")["id"]
    duplicate_label_rows(fake_trello, monkeypatch, label, ["Resume 2026-11-20 10:00", "Roadmap"])
    assert_needs_action(kproject, messages.DESTROY_BOARD_INCOMPLETE)


def test_label_rows_identical_duplicates_collapse(kproject, fake_trello, monkeypatch):
    setup_board(kproject, fake_trello)
    label = fake_trello.add_label(kproject.cfg.board_id, "Resume 2026-11-20 10:00")["id"]
    duplicate_label_rows(fake_trello, monkeypatch, label, ["Resume 2026-11-20 10:00"] * 2)
    plan, _ = plan_of(kproject)
    assert plan.labels == [label]


def test_list_rows_same_id_different_state(kproject, fake_trello, monkeypatch):
    ids = setup_board(kproject, fake_trello)
    review = ids["claude-1 · Review"]

    def edit(method, path, params, payload):
        if is_nested_read(path, params):
            payload["lists"].append({"id": review, "name": "claude-1 · Review", "closed": True, "pos": 1})
    edit_route(fake_trello, monkeypatch, edit)
    assert_needs_action(kproject, messages.DESTROY_BOARD_INCOMPLETE)
