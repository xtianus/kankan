"""End-to-end smoke on the fake stack: init, up, implement, review, integrate, Done."""

import json

import pytest

from kankan import agents_cmd, diag
from kankan.init import init
from kankan.project import Project
from kankan.refstore import card_ref
from tests.conftest import git
from tests.fakes.fake_trello import KEY, TOKEN
from tests.launcher_helpers import wait_for

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

IMPLEMENT = "echo 'feature' > feature.txt && git add feature.txt && git commit -qm 'add feature' && kankan done --json"
REVIEW = ("kankan sync --json && H=$(git rev-parse HEAD) && kankan verify --tested $H --json && "
          "kankan integrate --tested $H --json && kankan done --json")


def test_card_goes_from_backlog_to_done(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    fake = fake_trello
    monkeypatch.setenv("KANKAN_TRELLO_URL", fake.url)
    monkeypatch.setenv("KANKAN_TRELLO_KEY", KEY)
    monkeypatch.setenv("KANKAN_TRELLO_TOKEN", TOKEN)
    monkeypatch.setenv("KANKAN_TMUX_SOCKET", tmux_server.socket)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"claude": {"steps": [{"shell": IMPLEMENT}]},
                                    "codex": {"steps": [{"shell": REVIEW}]}}))
    monkeypatch.setenv("FAKE_AGENT_SCENARIO", str(scenario))
    board = fake.add_board("e2e")
    answers = iter(["test -f feature.txt"])
    cfg, _ = init(git_repo, board["url"], prompt=lambda q: next(answers, ""), say=lambda s: None)
    from kankan.config import Paths, load_config, save_config
    paths = Paths.discover(git_repo)
    cfg = load_config(paths)
    cfg.poll_seconds, cfg.write_quiet_seconds, cfg.settle_seconds = 1, 0, 0
    save_config(paths, cfg)
    git(git_repo, "checkout", "-q", "--detach")
    project = Project.open(git_repo)
    card = fake.add_card(cfg.backlog, "Add the feature")
    agents_cmd.up(project, {"claude": 1, "codex": 1})
    assert wait_for(lambda: fake.card_list(card["id"]) == cfg.done and project.refs.read(card_ref(card["id"])) is None,
                    timeout=120)
    log = git(git_repo, "log", "--first-parent", "--format=%s%n%b", "main")
    assert "Add the feature" in log and f"Kankan-Card: {card['shortLink']}" in log
    assert git(git_repo, "show", "main:feature.txt") == "feature"
    assert diag.reconcile(project) == []
