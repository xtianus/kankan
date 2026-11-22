"""seat T§5: what a seat being removed may do — task choice, the Q&A claim, acquisition, required lists, counts."""

import pytest

from kankan import agents_cmd, diag, qa, seats, stopall, workflow
from kankan.adapters import ModelEffort
from kankan.config import check_lists, save_config
from kankan.errors import KankanError, Status
from kankan.project import Project
from kankan.refstore import card_ref
from tests.helpers import add_qa_list, hold

PAIR = ModelEffort("sonnet", "high")


def _set(project, seat, **fields):
    """Write seat fields through a separately opened project, as another process would."""
    other = Project.open(str(project.paths.repo))
    cfg = other.reload_config()
    for key, value in fields.items():
        setattr(cfg.seats[seat], key, value)
    save_config(other.paths, cfg)


def _register(project, *names):
    for name in names:
        lease = seats.acquire(project, seats.seat_type(name), seat=name)
        seats.release(project, lease)
    project.reload_config()


def test_removing_seat_takes_only_own_work(kproject, fake_trello):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    held = fake.add_card(project.cfg.backlog, "held", pos=0)
    assert workflow.tick(project, lease)["card"] == held["id"]
    fake.add_card(project.cfg.backlog, "next", pos=0)
    _set(project, "claude-1", removing="graceful")
    project.reload_config()
    choice = workflow.choose(project, lease, project.trello.snapshot(project.cfg.board_id), workflow.card_refs(project))
    assert choice.card.id == held["id"] and choice.kind == "resume_impl"
    project.refs.delete(card_ref(held["id"]), project.refs.read(card_ref(held["id"])).oid)
    assert workflow.choose(project, lease, project.trello.snapshot(project.cfg.board_id),
                           workflow.card_refs(project)) is None


def test_tick_recheck_sees_flag_written_by_other_process(kproject, fake_trello, monkeypatch):
    project, fake = kproject, fake_trello
    lease = seats.acquire(project, "claude")
    card = fake.add_card(project.cfg.backlog, "c", pos=0)
    real = workflow.choose

    def choose(*args, **kwargs):
        result = real(*args, **kwargs)  # chosen on the configuration read before the flag
        _set(project, "claude-1", removing="graceful")
        return result
    monkeypatch.setattr(workflow, "choose", choose)
    assert workflow.tick(project, lease) is None
    assert project.refs.read(card_ref(card["id"])) is None
    assert fake.card_list(card["id"]) == project.cfg.backlog


def test_qa_claim_refused_while_removing(kproject, fake_trello):
    project, fake = kproject, fake_trello
    qa_list = add_qa_list(project, fake)
    lease = seats.acquire_qa(project, "claude", None)
    card = fake.add_card(qa_list, "question")
    _set(project, "claude-qa", removing="graceful")
    assert qa.claim(project, lease, project.trello.card(card["id"]), PAIR) is None
    assert project.refs.read(card_ref(card["id"])) is None


def test_acquire_skips_removing_seat_unless_named(kproject):
    project = kproject
    _register(project, "claude-1", "claude-2")
    _set(project, "claude-1", removing="graceful")
    lease = seats.acquire(project, "claude")
    assert lease.seat == "claude-2"
    named = seats.acquire(project, "claude", seat="claude-1")
    assert named.seat == "claude-1"


def test_acquire_create_false_does_not_recreate(kproject):
    project = kproject
    with pytest.raises(KankanError) as err:
        seats.acquire(project, "claude", seat="claude-3", create=False)
    assert err.value.status == Status.USAGE_ERROR
    assert "claude-3" not in project.reload_config().seats


def test_agent_create_false_marks_the_session(kproject, monkeypatch):
    seen = {}

    class Tmux:
        def has_session(self, name):
            return False

        def sessions_with_option(self, option):
            return {}

        def new_session(self, name, cwd, argv, env=None):
            seen["env"] = env

        def set_option(self, *args):
            pass
    monkeypatch.setattr("kankan.doctor.preflight", lambda *a, **k: None)
    _register(kproject, "claude-1")
    agents_cmd.agent(kproject, "claude", seat="claude-1", tmux=Tmux(), create=False)
    assert seen["env"].get(seats.EXISTING_ONLY_ENV) == "1"


def test_partial_archive_does_not_stop_other_seat(kproject, fake_trello):
    project = kproject
    _register(project, "claude-1", "codex-1")
    project.trello.archive_list(project.cfg.seats["claude-1"].implemented)
    with pytest.raises(KankanError):
        check_lists(project.reload_config(), project.trello.snapshot(project.cfg.board_id).lists)
    _set(project, "claude-1", removing="immediate")
    check_lists(project.reload_config(), project.trello.snapshot(project.cfg.board_id).lists)


def test_counts_skip_removing(kproject, holders):
    project = kproject
    holders.append(hold(project, "claude-1", mode="ignore"))
    holders.append(hold(project, "claude-2", mode="ignore"))
    project.reload_config()
    assert agents_cmd.live_counts(project) == {"claude": 2}
    _set(project, "claude-1", removing="graceful")
    project.reload_config()
    assert agents_cmd.live_counts(project) == {"claude": 1}
    assert agents_cmd.registered_args(project.cfg) == "claude=1"
    assert stopall._args_from_seats(project, ["claude-1", "claude-2"]) == "claude=1"


def test_status_shows_removing_and_error(kproject, fake_trello):
    project = kproject
    _register(project, "claude-1")
    _set(project, "claude-1", removing="graceful", remove_error="card X is still in claude-1 · Review")
    data = diag.status(project)
    row = next(r for r in data["seats"] if r["seat"] == "claude-1")
    assert row["removing"] == "graceful" and row["remove_error"].startswith("card X")
    text = diag.status_text(data)
    assert "removing (graceful)" in text and "card X is still" in text
