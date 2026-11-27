"""Q&A end to end (questions F§8): the Q&A seat and the work seats together, with the real supervisor → launcher →
fake agent chain in a private tmux server. A question, a follow-up with an attachment and a handoff that work seats
implement, review and integrate; the next card of the queue; a card dragged back; and the independence of the Q&A
seat and the work seats with Trello responses delayed for one of them (F§8 test 9, T§9)."""

import fcntl
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from kankan import auditlog, diag, messages, seats
from kankan.config import load_config
from kankan.refstore import card_ref
from tests.conftest import git
from tests.helpers import add_qa_list, add_questions_seed, bind_project
from tests.launcher_helpers import agent_inputs, start_agent, start_qa_agent, wait_for

pytestmark = [pytest.mark.tmux, pytest.mark.slow]

FAST = dict(poll_seconds=1, write_quiet_seconds=0, idle_ready_minutes=0.05, qa_poll_seconds=1, trello_timeout=10,
            integration_branch="kankan-agents")
IMPLEMENT = "echo feature > feature.txt && git add feature.txt && git commit -qm 'add feature' && kankan done --json"
REVIEW = ("kankan sync --json && H=$(git rev-parse HEAD) && kankan verify --tested $H --json && "
          "kankan integrate --tested $H --json && kankan done --json")
DELAY = 1.5  # seconds added to each delayed Trello response
# The longest project-lock hold allowed, by the seat whose card's Trello responses are delayed; the margin covers
# Git and the undelayed board reads.
# - Q&A (T§3): at most a card read, a move and its verification read. Posting the three chunks of an answer under
#   the lock (a card read, a comment read and a post per chunk: 9 delayed requests) would exceed it.
# - work seat: the implementation claim as the initial specification makes it, unchanged by this feature (F§5):
#   the card read, the move, its verification read, the claim comment and the HOLD recheck.
LOCK_BOUND = {"qa": 3 * DELAY + 1.5, "work": 5 * DELAY + 3}


@pytest.fixture
def stack(git_repo, fake_trello, monkeypatch, agent_path, tmux_server, tmp_path):
    monkeypatch.setattr(seats, "KILL_GRACE", 0.5)
    git(git_repo, "branch", "kankan-agents", "main")
    project = bind_project(git_repo, fake_trello, monkeypatch, **FAST)
    add_questions_seed()
    env = SimpleNamespace(project=project, fake=fake_trello, tmux=tmux_server, tmp=tmp_path)
    env.qa_list = add_qa_list(project, fake_trello)
    env.out = tmp_path / "out"
    env.out.mkdir()
    return env


def worker(env):
    """A work agent's step: implement or review, whichever task it gets; ``task --json`` saved for the checks."""
    save = f"F={env.out}/task-$KANKAN_SEAT-$(date +%s%N).json; kankan task --json > $F"
    return {"shell": f"{save}; if grep -q '\"kind\": \"impl\"' $F; then {IMPLEMENT}; else {REVIEW}; fi"}


def answer(text, save=None):
    task = f"kankan task --json > {save} && " if save else ""
    return {"shell": f"{task}kankan answer --turn {{turn}} --file {{file}}", "text": text}


def comments(env, card, needle):
    return [text for text in env.fake.card_comments(card["id"]) if needle in text]


def ref_meta(env, card):
    value = env.project.refs.read(card_ref(card["id"]))
    return value.meta if value else None


def saved_tasks(env, kind):
    found = []
    for path in sorted(env.out.glob("task-*.json")):
        data = json.loads(path.read_text()).get("data") or {}
        if data.get("kind") == kind:
            found.append(data)
    return found


def assigned(env, card, kind):
    return [e["seat"] for e in auditlog.read(env.project.paths)
            if e["event"] == "task_assigned" and e.get("card") == card["id"] and e.get("kind") == kind]


def refs(env):
    """Every ref of the repository but the seat refs (taken by the seats as they start)."""
    lines = git(env.project.paths.repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    return {line for line in lines if not line.startswith("refs/agent-seats/")}


def new_flags(task):
    return {c["text"]: c["new"] for c in task["comments"]}


def test_question_follow_up_and_handoff_implemented_reviewed_and_integrated(stack):
    env = stack
    cfg = env.project.cfg
    first = env.fake.add_card(env.qa_list, "how", desc="how does it work?")
    before = refs(env)
    qa2, qa5 = env.out / "qa2.json", env.out / "qa5.json"
    _, qa_log = start_qa_agent(env.tmux, env.project, scenario={"steps": [
        answer("It works like this."),
        answer("About the log: it is fine.", save=qa2),
        {"do": [["kankan", "handoff", "--turn", "{turn}", "--file", "{file}"]],
         "text": "Create feature.txt with the line `feature`."},
        answer("Second answer."),
        answer("Yes, it is integrated.", save=qa5)]}, tmp_path=env.tmp)
    for agent_type in ("claude", "codex"):
        start_agent(env.tmux, env.project, agent_type, scenario={"steps": [worker(env) for _ in range(3)]},
                    tmp_path=env.tmp)

    # F§8 1: an answer mentioning the creator; the card stays in Q&A without labels; only its card ref is new
    assert wait_for(lambda: comments(env, first, "It works like this."), timeout=60)
    assert "@boardowner" in comments(env, first, "It works like this.")[0]
    assert env.fake.card_list(first["id"]) == env.qa_list and env.fake.state["cards"][first["id"]]["idLabels"] == []
    card_ref_name = f"refs/agent-cards/{first['id']}"
    assert {line.split()[0] for line in refs(env) ^ before} == {card_ref_name}
    # F§8 2, 3: a follow-up with a comment and an attachment, delivered once, marked new, downloaded
    env.fake.user_comment(first["id"], "and the log?")
    env.fake.upload(first["id"], "log.txt", b"all good\n", "text/plain")
    assert wait_for(lambda: comments(env, first, "About the log"), timeout=30)
    task = json.loads(qa2.read_text())["data"]
    assert new_flags(task)["and the log?"] is True
    assert [c["new"] for c in task["comments"] if c["role"] == "agent"] == [False]
    log_entry = next(a for a in task["attachments"] if a["name"] == "log.txt")
    assert log_entry["new"] is True and open(log_entry["path"], "rb").read() == b"all good\n"
    assert len(comments(env, first, messages.FORWARDED)) == 1

    # F§8 5: the handoff goes to the bottom of Backlog, work seats implement, review and integrate it
    env.fake.user_comment(first["id"], "now implement it")
    assert wait_for(lambda: env.fake.card_list(first["id"]) == cfg.done and ref_meta(env, first) is None,
                    timeout=180)
    summary = comments(env, first, "Create feature.txt")
    assert len(summary) == 1 and "@boardowner" in summary[0]
    impl = [t for t in saved_tasks(env, "impl") if t["card_id"] == first["id"]]
    assert impl and impl[0]["handoff"] == "Create feature.txt with the line `feature`."
    assert any(c["text"] == "now implement it" for c in impl[0]["comments"])
    implementers, reviewers = assigned(env, first, "impl"), assigned(env, first, "review")
    assert implementers and reviewers and all(not seats.is_qa(s) for s in implementers + reviewers)
    assert implementers[0].split("-")[0] != reviewers[0].split("-")[0]
    log = git(env.project.paths.repo, "log", "--first-parent", "--format=%s%n%b", "kankan-agents")
    assert f"Kankan-Card: {first['shortLink']}" in log
    assert git(env.project.paths.repo, "show", "kankan-agents:feature.txt") == "feature"

    # F§8 6: the next card, created once the handed-off conversation ended, goes to claude-qa (its card ended: no
    # member opens, qapool F§4.2) and starts in a cleared conversation
    second = env.fake.add_card(env.qa_list, "second", desc="a second question")
    assert wait_for(lambda: comments(env, second, "Second answer."), timeout=60)
    inputs = agent_inputs(qa_log)
    prompts = [i for i, line in enumerate(inputs) if line not in ("/clear", "/new")]
    assert "/clear" in inputs[prompts[2] + 1:prompts[3]]

    # F§8 6: moving the active card to Done ends the conversation; reconcile does not report it
    env.fake.user_move(second["id"], cfg.done)
    assert wait_for(lambda: ref_meta(env, second) is None, timeout=30)

    # F§8 5, 3.7: the integrated card dragged back into Q&A starts a new conversation over its whole thread
    env.fake.user_comment(first["id"], "is it integrated?")
    env.fake.user_move(first["id"], env.qa_list)
    assert wait_for(lambda: comments(env, first, "Yes, it is integrated."), timeout=60)
    again = new_flags(json.loads(qa5.read_text())["data"])
    assert again["is it integrated?"] is True and again["now implement it"] is False
    assert again["and the log?"] is False
    env.fake.user_move(first["id"], cfg.done)
    assert wait_for(lambda: ref_meta(env, first) is None, timeout=30)
    assert env.fake.card_list(second["id"]) == cfg.done and env.fake.card_list(first["id"]) == cfg.done
    # a command holding the conversation lock at the end keeps the directory until it releases the lock
    assert wait_for(lambda: diag.reconcile(env.project) == [], timeout=30), diag.reconcile(env.project)


class LockWatch:
    """Measures, from outside, the longest continuous hold of the project lock by any process."""

    def __init__(self, path):
        self.path, self.longest, self.stop = path, 0.0, threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        busy_since = None
        try:
            while not self.stop.is_set():
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    busy_since = None
                except BlockingIOError:
                    now = time.monotonic()
                    busy_since = busy_since or now
                    self.longest = max(self.longest, now - busy_since)
                time.sleep(0.002)
        finally:
            os.close(fd)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(5)


@pytest.mark.parametrize("slow", ["qa", "work"])
def test_long_answer_and_implementation_claim_never_wait_for_each_other(stack, slow):
    """F§8 9, F§5: with Trello responses delayed for one seat's card, a three-chunk answer and an implementation
    claim each hold the project lock only for bounded steps, and neither waits for the other's agent."""
    env = stack
    cfg = env.project.cfg
    env.fake.response_delay = DELAY
    question = env.fake.add_card(env.qa_list, "long", desc="explain everything")
    text = "\n\n".join(f"Paragraph {i}: " + "x" * 1000 for i in range(36))  # about 36 000 characters: 3 chunks
    go, go2 = env.tmp / "go", env.tmp / "go2"
    _, qa_log = start_qa_agent(env.tmux, env.project, scenario={"steps": [
        {"shell": f"while [ ! -e {go} ]; do sleep 0.2; done; kankan answer --turn {{turn}} --file {{file}}",
         "text": text, "sleep": 0.5}]}, tmp_path=env.tmp)
    start_agent(env.tmux, env.project, "claude", scenario={"steps": [
        {"shell": f"while [ ! -e {go2} ]; do sleep 0.2; done; {IMPLEMENT}"}]}, tmp_path=env.tmp)
    with LockWatch(env.project.paths.kankan_dir / "project.lock") as watch:
        # the Q&A agent is busy with its answer (it waits for `go`) ...
        assert wait_for(lambda: any("turn" in line for line in agent_inputs(qa_log)), timeout=60)
        card = env.fake.add_card(cfg.backlog, "feature")
        delayed = question["id"] if slow == "qa" else card["id"]
        for method in ("GET", "POST", "PUT"):
            env.fake.inject(method, rf"/1/cards/{delayed}", "delay", times=10 ** 6)
        # ... and the implementation claim does not wait for it
        in_progress = lambda: load_config(env.project.paths).seats["claude-1"].in_progress
        assert wait_for(lambda: env.fake.card_list(card["id"]) == in_progress(), timeout=60)
        assert comments(env, question, "Paragraph 0") == []
        go.write_text("")
        # the work agent is busy (it waits for `go2`) and the three chunks are posted meanwhile
        assert wait_for(lambda: len(comments(env, question, "Paragraph")) == 3, timeout=90)
        posted = comments(env, question, "Paragraph")
        assert [p.split("\n", 1)[0] for p in posted] == ["1/3", "2/3", "3/3"]
        assert env.fake.card_list(card["id"]) == in_progress()
        assert wait_for(lambda: ref_meta(env, question).get("covered"), timeout=30)
        go2.write_text("")
        implemented = lambda: load_config(env.project.paths).seats["claude-1"].implemented
        assert wait_for(lambda: env.fake.card_list(card["id"]) == implemented(), timeout=60)
    assert watch.longest < LOCK_BOUND[slow], f"the project lock was held for {watch.longest:.1f} s"
