"""Card attachments as task inputs (spec §13.3, acceptance test 41)."""

import errno
import fcntl
import os
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from kankan import agentcmds, attachments, auditlog, config, seats, workflow
from kankan.errors import KankanError, Status
from kankan.refstore import card_ref
from kankan.trello import TrelloClient
from tests.conftest import git
from tests.flow import agent_ctx, commit
from tests.helpers import bind_project

CONTENT = b"hello attachment\n"
NOT_MEMBER = "added by someone who is not a board member (or by kankan)"
NOT_YET = "not downloaded yet: run `kankan task` again"


def make_project(git_repo, fake, monkeypatch, **overrides):
    monkeypatch.setattr(seats, "KILL_GRACE", 0.5)
    git(git_repo, "branch", "kankan-agents")
    return bind_project(git_repo, fake, monkeypatch, integration_branch="kankan-agents", **overrides)


@pytest.fixture
def env(git_repo, fake_trello, monkeypatch):
    return make_project(git_repo, fake_trello, monkeypatch), fake_trello


def claim(project, fake, monkeypatch, card=None):
    """The claude seat claims ``card`` (a new Backlog card by default); returns (card, lease, ctx)."""
    card = card or fake.add_card(project.cfg.backlog, "feature", pos=0)
    lease = seats.acquire(project, "claude")
    project.reload_config()
    task = workflow.tick(project, lease)
    assert task["card"] == card["id"], task
    return card, lease, agent_ctx(project, lease, monkeypatch)


def entries(ctx):
    return agentcmds.task(ctx)["attachments"]


def att_dir(project, card, att):
    return project.paths.kankan_dir / "attachments" / card["shortLink"] / att["id"]


def download_requests(fake):
    return [path for _, path, _ in fake.auth_log if "/download/" in path]


def part_files(root):
    return [name for _, _, names in os.walk(root) for name in names if name.endswith(".part")]


def events(project):
    return [record["event"] for record in auditlog.read(project.paths)]


# ---------------------------------------------------------------- pure helpers

@pytest.mark.parametrize("name, expected", [
    ("../../evil.txt", "____evil.txt"),
    (".env", "_env"),
    ("", "file"),
    ("a\nb.png", "a_b.png"),
    ("dir\\x.txt", "dir_x.txt"),
    ("ok name.png", "ok name.png"),
    ("..", "_"),
    ("a\x7fb", "a_b"),
])
def test_sanitize(name, expected):
    assert attachments.sanitize(name) == expected


CARD_ID = "5f" + "0" * 21 + "1"
ATT_ID = "5f" + "0" * 21 + "2"
PATH = f"/1/cards/{CARD_ID}/attachments/{ATT_ID}/download/a%20b.png"


def test_download_path():
    base = "https://api.trello.com"
    assert attachments.download_path(base, "https://trello.com" + PATH, CARD_ID, ATT_ID) == PATH
    assert attachments.download_path(base, base + PATH, CARD_ID, ATT_ID) == PATH
    assert attachments.download_path("http://127.0.0.1:8123", "http://127.0.0.1:8123" + PATH, CARD_ID, ATT_ID) == PATH
    other = "5f" + "0" * 21 + "3"
    for url in ("https://trello.com" + PATH.replace(CARD_ID, other),
                "https://trello.com" + PATH.replace(ATT_ID, other),
                "https://trello.com" + PATH + "/extra",
                "https://trello.com" + PATH.rsplit("/", 1)[0] + "/",
                "https://trello.com" + PATH.rsplit("/", 1)[0] + "/..",
                "https://trello.com" + PATH + "?x=1",
                "https://user@trello.com" + PATH,
                "https://trello.com:8443" + PATH,
                "http://trello.com" + PATH,
                "https://evil.example" + PATH,
                "http://127.0.0.1:8124" + PATH,
                "",
                "not a url"):
        assert attachments.download_path(base, url, CARD_ID, ATT_ID) is None, url


def test_card_dir_validates_short(env):
    project, _ = env
    assert attachments.card_dir(project.paths, "abcdEFG1") == project.paths.kankan_dir / "attachments" / "abcdEFG1"
    assert attachments.root(project.paths) == project.paths.kankan_dir / "attachments"
    for bad in ("", "../x", "abcdefg", "abcdefghi", "abc/efgh"):
        with pytest.raises(KankanError) as err:
            attachments.card_dir(project.paths, bad)
        assert err.value.status == Status.INTERNAL_ERROR


# ---------------------------------------------------------------- acceptance test 41

def test_task_lists_files_and_links_not_card_links(env, monkeypatch):
    """[41.1]"""
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    dep = fake.add_card(project.cfg.done, "dependency")
    fake.add_attachment(card["id"], dep["url"])
    upload = fake.upload(card["id"], "shot.png", CONTENT, "image/png")
    link = fake.add_attachment(card["id"], "https://example.com/spec.html", name="spec")
    result = entries(ctx)
    assert [e["id"] for e in result] == [upload["id"], link["id"]]
    file_entry, link_entry = result
    assert set(file_entry) == {"id", "name", "kind", "url", "mime_type", "bytes", "path", "status"}
    assert file_entry["kind"] == "file" and file_entry["status"] == "ok" and file_entry["name"] == "shot.png"
    assert file_entry["url"] == upload["url"] and file_entry["mime_type"] == "image/png"
    assert file_entry["bytes"] == len(CONTENT) and Path(file_entry["path"]).read_bytes() == CONTENT
    assert set(link_entry) == {"id", "name", "kind", "url", "status"}
    assert link_entry == {"id": link["id"], "name": "spec", "kind": "link", "url": "https://example.com/spec.html",
                          "status": "ok"}


def test_file_downloaded_once_and_reused_by_reviewer(env, monkeypatch):
    """[41.2]"""
    project, fake = env
    card = fake.add_card(project.cfg.backlog, "feature", pos=0)
    upload = fake.upload(card["id"], "spec.txt", CONTENT, "text/plain")
    claude = seats.acquire(project, "claude")
    codex = seats.acquire(project, "codex")
    project.reload_config()
    assert workflow.tick(project, claude)["card"] == card["id"]
    ctx = agent_ctx(project, claude, monkeypatch)
    first = entries(ctx)[0]
    commit(project.paths.seat_worktree(claude.seat), "feature.txt")
    agentcmds.done(ctx)
    task = workflow.tick(project, codex)
    assert task["kind"] == "review" and task["card"] == card["id"], task
    second = entries(agent_ctx(project, codex, monkeypatch))[0]
    assert first["status"] == second["status"] == "ok"
    assert first["path"] == second["path"]
    assert fake.downloads[upload["id"]] == 1
    assert Path(first["path"]).parent == att_dir(project, card, upload)
    assert Path(first["path"]).read_bytes() == CONTENT


def test_too_large_reported_and_streamed(git_repo, fake_trello, monkeypatch):
    """[41.3]"""
    project = make_project(git_repo, fake_trello, monkeypatch, attachment_max_bytes=10)
    fake = fake_trello
    card, _, ctx = claim(project, fake, monkeypatch)
    big = fake.upload(card["id"], "big.bin", b"x" * 20)
    liar = fake.upload(card["id"], "liar.bin", b"y" * 20, bytes_reported=5)
    fake.download_omit_length(liar["id"])
    result = entries(ctx)
    assert [e["status"] for e in result] == ["too_large", "too_large"]
    assert all(e["message"] and "path" not in e for e in result)
    assert not any(big["id"] in path for path in download_requests(fake))
    assert not att_dir(project, card, liar).exists()
    assert part_files(attachments.root(project.paths)) == []


def test_not_member_attachments(env, monkeypatch):
    """[41.4]"""
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    stranger = fake.add_member("stranger")
    fake.upload(card["id"], "a.txt", CONTENT, member_id=stranger["id"])
    fake.upload(card["id"], "b.txt", CONTENT, member_id=fake.me["id"])
    fake.add_attachment(card["id"], "https://example.com/x", member_id=stranger["id"])
    result = entries(ctx)
    assert [e["status"] for e in result] == ["not_member"] * 3
    assert all("url" not in e and "path" not in e and e["message"] == NOT_MEMBER for e in result)
    assert download_requests(fake) == []


def test_dotdot_name_stays_inside(env, monkeypatch):
    """[41.5]"""
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "../../evil.txt", CONTENT)
    entry = entries(ctx)[0]
    assert entry["status"] == "ok", entry
    assert entry["path"].endswith(f"{upload['id']}/____evil.txt")
    card_dir = attachments.card_dir(project.paths, card["shortLink"]).resolve()
    assert Path(entry["path"]).resolve().is_relative_to(card_dir)


def test_unexpected_url_gets_no_request(env, monkeypatch, external_host):
    """[41.6]"""
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    other = fake.add_card(project.cfg.backlog, "other")
    elsewhere = fake.upload(card["id"], "a.txt", CONTENT)
    elsewhere["url"] = f"{external_host.url}/1/cards/{card['id']}/attachments/{elsewhere['id']}/download/a.txt"
    wrong_card = fake.upload(card["id"], "b.txt", CONTENT)
    wrong_card["url"] = f"https://trello.com/1/cards/{other['id']}/attachments/{wrong_card['id']}/download/b.txt"
    result = entries(ctx)
    assert [e["status"] for e in result] == ["unexpected_url", "unexpected_url"]
    assert [e["url"] for e in result] == [elsewhere["url"], wrong_card["url"]]
    assert all(e["message"] and "path" not in e for e in result)
    assert external_host.requests == []
    assert download_requests(fake) == []


def test_redirect_to_other_host_gets_no_credential(env, monkeypatch, external_host):
    """[41.7]"""
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.bin", external_host.content)
    fake.redirect_download(upload["id"], external_host.url + "/f")
    entry = entries(ctx)[0]
    assert entry["status"] == "ok", entry
    assert Path(entry["path"]).read_bytes() == external_host.content
    assert len(external_host.requests) == 1
    assert not any(name.lower() == "authorization" for name in external_host.requests[0][1])


def test_redirect_hops(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    three = fake.upload(card["id"], "three.bin", CONTENT)
    four = fake.upload(card["id"], "four.bin", CONTENT)
    ftp = fake.upload(card["id"], "ftp.bin", CONTENT)
    target = fake.upload(card["id"], "target.bin", CONTENT)
    target_path = f"/1/cards/{card['id']}/attachments/{target['id']}/download/target.bin"
    fake.redirect_download(three["id"], fake.url + "/hop1")
    fake.redirect_download(four["id"], fake.url + "/hop0")
    fake.redirect_route("GET", r"^/hop0$", fake.url + "/hop1")
    fake.redirect_route("GET", r"^/hop1$", fake.url + "/hop2")
    fake.redirect_route("GET", r"^/hop2$", fake.url + target_path)
    fake.redirect_download(ftp["id"], "ftp://x/f")
    result = {e["id"]: e for e in entries(ctx)}
    assert result[three["id"]]["status"] == "ok", result[three["id"]]
    assert Path(result[three["id"]]["path"]).read_bytes() == CONTENT
    assert (result[four["id"]]["status"], result[four["id"]]["message"]) == ("error", "too many redirects")
    assert (result[ftp["id"]]["status"], result[ftp["id"]]["message"]) == ("error", "redirect refused")


def test_failed_download_is_error_then_retried(env, monkeypatch, virtual_time):
    """[41.8]"""
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.upload(card["id"], "a.txt", CONTENT)
    fake.inject("GET", "/download/", "500", times=20)
    entry = entries(ctx)[0]
    assert entry["status"] == "error" and entry["message"] and "path" not in entry
    assert part_files(attachments.root(project.paths)) == []
    fake.faults.clear()
    entry = entries(ctx)[0]
    assert entry["status"] == "ok" and Path(entry["path"]).read_bytes() == CONTENT


def test_removed_attachment_loses_copy(env, monkeypatch):
    """[41.9]"""
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.txt", CONTENT)
    assert entries(ctx)[0]["status"] == "ok"
    assert att_dir(project, card, upload).is_dir()
    fake.remove_attachment(card["id"], upload["id"])
    assert entries(ctx) == []
    assert not att_dir(project, card, upload).exists()


def test_uploader_left_board_loses_copy(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    member = fake.add_member("colleague")
    fake.join_board(project.cfg.board_id, member["id"])
    upload = fake.upload(card["id"], "a.txt", CONTENT, member_id=member["id"])
    assert entries(ctx)[0]["status"] == "ok"
    assert att_dir(project, card, upload).is_dir()
    fake.leave_board(project.cfg.board_id, member["id"])
    entry = entries(ctx)[0]
    assert entry["status"] == "not_member" and "url" not in entry
    assert not att_dir(project, card, upload).exists()


def test_task_network_error_when_listing_fails(env, monkeypatch, virtual_time):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.inject("GET", "/attachments$", "500", times=20)
    with pytest.raises(KankanError) as err:
        agentcmds.task(ctx)
    assert err.value.status == Status.NETWORK_ERROR
    fake.faults.clear()
    fake.upload(card["id"], "a.txt", CONTENT)
    fake.inject("GET", "/memberships$", "500", times=20)
    with pytest.raises(KankanError) as err:
        agentcmds.task(ctx)
    assert err.value.status == Status.NETWORK_ERROR


def test_task_takes_no_project_lock(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.upload(card["id"], "a.txt", CONTENT)
    real = config.file_lock

    def guarded(path, *args, **kwargs):
        if Path(path).name == "project.lock":
            raise AssertionError("task took the project lock")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(config, "file_lock", guarded)
    assert entries(ctx)[0]["status"] == "ok"


# ---------------------------------------------------------------- path safety

def test_symlinked_cache_file_not_served(env, monkeypatch, tmp_path):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.upload(card["id"], "a.txt", CONTENT)
    path = Path(entries(ctx)[0]["path"])
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    path.unlink()
    path.symlink_to(outside)
    entry = entries(ctx)[0]
    assert (entry["status"], entry["message"]) == ("error", "unsafe attachment path")
    assert "path" not in entry
    assert outside.read_text() == "secret"


def test_symlinked_root_refused(env, monkeypatch, tmp_path):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.upload(card["id"], "a.txt", CONTENT)
    fake.upload(card["id"], "b.txt", CONTENT)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = attachments.root(project.paths)
    root.symlink_to(outside)
    result = entries(ctx)
    assert [(e["status"], e["message"]) for e in result] == [("error", "unsafe attachment path")] * 2
    assert list(outside.iterdir()) == []
    assert root.is_symlink()
    assert download_requests(fake) == []


def test_invalid_attachment_id(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.txt", CONTENT)
    upload["id"] = "../x"
    entry = entries(ctx)[0]
    assert (entry["id"], entry["status"], entry["message"]) == ("../x", "error", "invalid attachment id")
    assert download_requests(fake) == []


def test_discard_does_not_follow_symlink(env, tmp_path):
    project, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    card_dir = attachments.card_dir(project.paths, "abcdEFG1")
    (card_dir / ("5f" + "0" * 22)).mkdir(parents=True)
    (card_dir / ("5f" + "0" * 22) / "inner").symlink_to(outside)
    (card_dir / "link").symlink_to(outside)
    assert attachments.directories(project.paths) == ["abcdEFG1"]
    assert attachments.discard(project.paths, "abcdEFG1") is True
    assert not card_dir.exists() and (outside / "keep.txt").read_text() == "keep"
    assert attachments.directories(project.paths) == []
    assert attachments.discard(project.paths, "abcdEFG1") is True  # nothing left to delete
    assert attachments.discard(project.paths, "") is False
    assert attachments.discard(project.paths, "../../x") is False
    assert attachments.discard(project.paths, None) is False


def test_discard_symlinked_card_dir_unlinks_it(env, tmp_path):
    project, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    card_dir = attachments.card_dir(project.paths, "abcdEFG1")
    card_dir.parent.mkdir(parents=True)
    card_dir.symlink_to(outside)
    assert attachments.discard(project.paths, "abcdEFG1") is True
    assert not card_dir.is_symlink() and (outside / "keep.txt").exists()


# ---------------------------------------------------------------- races

def run_thread(target):
    """Start ``target`` in a thread; returns (thread, outcome) where outcome gets "result" or "error"."""
    outcome = {}

    def body():
        try:
            outcome["result"] = target()
        except BaseException as err:  # noqa: B902 - reported by the test
            outcome["error"] = err
    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, outcome


def lock_waiter(monkeypatch):
    """An event set when a flock attempt finds a lock busy (a second call waiting on the card lock)."""
    waiting = threading.Event()
    real = fcntl.flock

    def flock(fd, operation):
        try:
            return real(fd, operation)
        except BlockingIOError:
            waiting.set()
            raise
    monkeypatch.setattr(fcntl, "flock", flock)
    return waiting


def test_ref_deleted_during_download(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.upload(card["id"], "a.txt", CONTENT)
    real = TrelloClient.download
    outcome = {}

    def download(self, *args):
        size = real(self, *args)
        value = ctx.project.refs.read(card_ref(card["id"]))
        assert ctx.project.refs.delete(card_ref(card["id"]), value.oid)
        # `done` runs in another process: a helper thread opens its own descriptor and finds the lock busy
        thread, outcome["discard"] = run_thread(lambda: attachments.discard(ctx.project.paths, card["shortLink"]))
        thread.join(10)
        return size
    monkeypatch.setattr(TrelloClient, "download", download)
    with pytest.raises(KankanError) as err:
        agentcmds.task(ctx)
    assert err.value.status == Status.INELIGIBLE
    assert err.value.message == "the card of the current task is no longer held by this seat"
    assert outcome["discard"] == {"result": False}
    assert "attachments_discard_busy" in events(project)
    assert not attachments.card_dir(project.paths, card["shortLink"]).exists()


def test_concurrent_tasks_serialized(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.txt", CONTENT)
    trello_card = ctx.project.trello.card(card["id"])
    waiting = lock_waiter(monkeypatch)
    real = TrelloClient.download

    def download(self, *args):
        size = real(self, *args)
        assert waiting.wait(10), "the other call never waited on the lock"
        return size
    monkeypatch.setattr(TrelloClient, "download", download)
    calls = [run_thread(lambda: attachments.for_task(ctx.project, trello_card, still_held=lambda: True))
             for _ in range(2)]
    for thread, _ in calls:
        thread.join(30)
    results = [outcome.get("result") or outcome.get("error") for _, outcome in calls]
    assert all(isinstance(result, list) for result in results), results
    assert [r[0]["status"] for r in results] == ["ok", "ok"]
    assert results[0][0]["path"] == results[1][0]["path"]
    assert fake.downloads[upload["id"]] == 1
    assert Path(results[0][0]["path"]).read_bytes() == CONTENT


def test_waiting_call_uses_cache_despite_faults(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.txt", CONTENT)
    trello_card = ctx.project.trello.card(card["id"])
    waiting = lock_waiter(monkeypatch)
    real = TrelloClient.download
    second = {}

    def download(self, *args):
        size = real(self, *args)
        fake.inject("GET", "/download/", "500", times=20)
        second["call"] = run_thread(lambda: attachments.for_task(ctx.project, trello_card, still_held=lambda: True))
        assert waiting.wait(10), "the second call never waited on the lock"
        return size
    monkeypatch.setattr(TrelloClient, "download", download)
    first = attachments.for_task(ctx.project, trello_card, still_held=lambda: True)
    thread, outcome = second["call"]
    thread.join(30)
    assert "error" not in outcome, outcome
    assert first[0]["status"] == outcome["result"][0]["status"] == "ok"
    assert first[0]["path"] == outcome["result"][0]["path"]
    assert len(download_requests(fake)) == 1 and fake.downloads[upload["id"]] == 1
    assert fake.faults[0][3] == 20  # the waiting call sent no download request
    assert Path(first[0]["path"]).read_bytes() == CONTENT


# ---------------------------------------------------------------- errors, limits, cache

def test_error_message_has_no_body(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.upload(card["id"], "a.txt", CONTENT)
    fake.inject("GET", "/download/", "404")
    entry = entries(ctx)[0]
    assert (entry["status"], entry["message"]) == ("error", "HTTP 404")
    assert fake.token[:12] not in str(entry) and fake.key[:12] not in str(entry)


def test_null_size_copy_above_lowered_limit_redownloaded(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.bin", b"z" * 50, bytes_reported=None)
    fake.download_omit_length(upload["id"])  # no Content-Length: the streaming cap stops the download
    assert entries(ctx)[0]["status"] == "ok"
    ctx.project.cfg.attachment_max_bytes = 10
    entry = entries(ctx)[0]
    assert entry["status"] == "too_large" and "path" not in entry
    assert not att_dir(project, card, upload).exists()
    assert len(download_requests(fake)) == 2  # the oversized copy was not reused: the cap stopped a new download
    assert part_files(attachments.root(project.paths)) == []


def test_budget_exhausted_defers_downloads(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.upload(card["id"], "a.txt", CONTENT)
    fake.add_attachment(card["id"], "https://example.com/spec")
    monkeypatch.setattr(attachments, "DOWNLOAD_BUDGET_SECONDS", 0)
    file_entry, link_entry = entries(ctx)
    assert (file_entry["status"], file_entry["message"]) == ("error", NOT_YET)
    assert link_entry["status"] == "ok"
    assert download_requests(fake) == []


def test_leftover_part_removed(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.txt", CONTENT)
    path = Path(entries(ctx)[0]["path"])
    leftover = att_dir(project, card, upload) / ".crashed.part"
    leftover.write_bytes(b"half")
    assert entries(ctx)[0]["path"] == str(path)
    assert not leftover.exists() and path.read_bytes() == CONTENT
    assert fake.downloads[upload["id"]] == 1


def _disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


class _FullFile:
    """A file whose writes fail as on a full disk."""

    def __init__(self, out):
        self.out = out

    def write(self, data):
        _disk_full()

    def __getattr__(self, name):
        return getattr(self.out, name)


@pytest.mark.parametrize("failing", ["replace", "mkstemp", "makedirs", "write"])
def test_write_errors_are_entry_errors(env, monkeypatch, failing):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    fake.upload(card["id"], "a.txt", CONTENT)
    root = str(attachments.root(project.paths))

    def selective(real, path_of):
        def wrapper(*args, **kwargs):
            if str(path_of(args, kwargs)).startswith(root):
                _disk_full()
            return real(*args, **kwargs)
        return wrapper
    if failing == "replace":
        monkeypatch.setattr(os, "replace", selective(os.replace, lambda a, k: a[1]))
    elif failing == "mkstemp":
        monkeypatch.setattr(tempfile, "mkstemp", selective(tempfile.mkstemp, lambda a, k: k.get("dir", "")))
    elif failing == "makedirs":
        monkeypatch.setattr(os, "makedirs", selective(os.makedirs, lambda a, k: a[0]))
    else:
        real = TrelloClient.download
        monkeypatch.setattr(TrelloClient, "download", lambda self, path, out, *a: real(self, path, _FullFile(out), *a))
    entry = entries(ctx)[0]
    assert entry["status"] == "error" and "No space left" in entry["message"] and "path" not in entry
    assert part_files(root) == []


def test_prune_error_logged(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.txt", CONTENT)
    assert entries(ctx)[0]["status"] == "ok"
    fake.remove_attachment(card["id"], upload["id"])
    real = shutil.rmtree
    root = str(attachments.root(project.paths))

    def rmtree(path, *args, **kwargs):
        if str(path).startswith(root):
            _disk_full()
        return real(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    assert entries(ctx) == []
    assert "attachments_prune_failed" in events(project)


def test_null_bytes_and_mime(env, monkeypatch):
    project, fake = env
    card, _, ctx = claim(project, fake, monkeypatch)
    upload = fake.upload(card["id"], "a.bin", CONTENT, mime_type=None, bytes_reported=None)
    for _ in range(2):
        entry = entries(ctx)[0]
        assert entry["status"] == "ok" and entry["mime_type"] is None and entry["bytes"] is None
        assert Path(entry["path"]).read_bytes() == CONTENT
    assert fake.downloads[upload["id"]] == 1


def test_input_members_excludes_kankan(env):
    from kankan import notify
    project, fake = env
    assert notify.input_members(project) == {fake.owner["id"]}
