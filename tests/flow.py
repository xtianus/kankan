"""Workflow helpers shared by review/integration tests."""

from kankan import agentcmds, seats, workflow, worktrees
from kankan.cards import CardMeta, target_list_for, transition
from kankan.refstore import card_ref
from tests.conftest import git


def commit(wt, name, text=None, message=None):
    (wt / name).write_text(text if text is not None else name + "\n")
    git(wt, "add", name)
    git(wt, "commit", "-qm", message or f"add {name}")
    return git(wt, "rev-parse", "HEAD")


def implement(project, fake, lease, name="feature", files=None):
    card = fake.add_card(project.cfg.backlog, name, pos=0)
    task = workflow.tick(project, lease)
    assert task["card"] == card["id"], task
    wt = project.paths.seat_worktree(lease.seat)
    for path, text in (files or {f"{name}.txt": name + "\n"}).items():
        commit(wt, path, text)
    worktrees.detach(wt)
    value = project.refs.read(card_ref(card["id"]))
    new = CardMeta.from_dict(value.meta).evolve(phase="implemented", owner=None, session=None,
                                                 target_list=target_list_for(project.cfg, "implemented", None, lease.seat))
    transition(project, lease, project.trello.card(card["id"]), value, new, None)
    return card


def agent_ctx(project, lease, monkeypatch):
    monkeypatch.setenv("KANKAN_SEAT", lease.seat)
    monkeypatch.setenv("KANKAN_SESSION", lease.session)
    return agentcmds.context(project.paths.seat_worktree(lease.seat))


def review_setup(project, fake, monkeypatch, name="feature", files=None, claude=None, codex=None):
    """claude implements a card, codex claims its review; returns (card, codex lease, ctx)."""
    claude = claude or seats.acquire(project, "claude")
    codex = codex or seats.acquire(project, "codex")
    project.reload_config()
    card = implement(project, fake, claude, name, files)
    task = workflow.tick(project, codex)
    assert task["kind"] == "review" and task["card"] == card["id"], task
    return card, codex, agent_ctx(project, codex, monkeypatch)
