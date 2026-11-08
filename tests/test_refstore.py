import multiprocessing
import subprocess

import pytest

from kankan.errors import KankanError, Status
from kankan.refstore import (Create, Delete, Main, RefStore, Update, Verify, card_ref, seat_ref,
                             validate_card_id, validate_seat, validate_short_link)
from tests.conftest import git

CARD = "65a1" + "0" * 18 + "e3"


def _worker(repo, barrier, queue):
    store = RefStore(repo, "main")
    oid = store.write_blob({"schema": 1, "phase": "impl", "owner": multiprocessing.current_process().name})
    barrier.wait()
    try:
        store.create(card_ref(CARD), oid)
        queue.put("ok")
    except KankanError as err:
        queue.put(err.status.value)


def test_parallel_create_exactly_one_wins(git_repo):
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(20)
    queue = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(git_repo, barrier, queue), name=f"w{i}") for i in range(20)]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join()
    assert results.count("ok") == 1
    assert results.count("already_claimed") == 19


def test_create_retry_recognizes_own_ref(git_repo):
    store = RefStore(git_repo, "main")
    oid = store.write_blob({"schema": 1, "phase": "impl"})
    store.create(card_ref(CARD), oid)
    store.create(card_ref(CARD), oid)
    assert store.read(card_ref(CARD)).oid == oid


def test_write_blob_adds_nonce(git_repo):
    store = RefStore(git_repo, "main")
    first = store.write_blob({"schema": 1})
    second = store.write_blob({"schema": 1})
    assert first != second


def test_conditional_update_rejects_replaced_ref(git_repo):
    store = RefStore(git_repo, "main")
    ref = card_ref(CARD)
    b1, b2, b3 = (store.write_blob({"schema": 1, "n": i}) for i in range(3))
    store.create(ref, b1)
    assert store.update(ref, b3, b1)
    assert not store.update(ref, b2, b1)
    assert store.read(ref).oid == b3
    assert not store.delete(ref, b1)
    assert store.delete(ref, b3)
    assert store.read(ref) is None


def test_transaction_all_or_nothing(git_repo):
    store = RefStore(git_repo, "main")
    seat, card = seat_ref("claude-1"), card_ref(CARD)
    s1, b1, b2, wrong = (store.write_blob({"schema": 1, "n": i}) for i in range(4))
    store.create(seat, s1)
    store.create(card, b1)
    assert not store.transaction([Verify(seat, wrong), Update(card, b2, b1)])
    assert store.read(card).oid == b1
    assert store.transaction([Verify(seat, s1), Update(card, b2, b1)])
    assert store.read(card).oid == b2


def test_transaction_create_delete(git_repo):
    store = RefStore(git_repo, "main")
    b1 = store.write_blob({"schema": 1})
    other = card_ref("a" * 24)
    assert store.transaction([Create(other, b1)])
    assert not store.transaction([Create(other, b1)])
    assert store.transaction([Delete(other, b1)])
    assert store.list("refs/agent-cards/") == {}


def test_malformed_blob_needs_reconciliation(git_repo):
    store = RefStore(git_repo, "main")
    head = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "update-ref", card_ref(CARD), head)
    with pytest.raises(KankanError) as exc:
        store.read(card_ref(CARD))
    assert exc.value.status == Status.NEEDS_RECONCILIATION
    bad = store.write_blob({"schema": 2})
    git(git_repo, "update-ref", card_ref(CARD), bad)
    with pytest.raises(KankanError):
        store.read(card_ref(CARD))


def test_validators_reject_injection():
    for func, bad in ((validate_seat, "claude-1;rm"), (validate_card_id, "x" * 24), (validate_short_link, "Ab12Cd3")):
        with pytest.raises(KankanError) as exc:
            func(bad)
        assert exc.value.status == Status.USAGE_ERROR
    assert validate_seat("codex-12") == "codex-12"
    assert validate_short_link("Ab12Cd34") == "Ab12Cd34"


def test_list_prefix(git_repo):
    store = RefStore(git_repo, "main")
    oid = store.write_blob({"schema": 1, "phase": "impl"})
    store.create(card_ref(CARD), oid)
    listed = store.list("refs/agent-cards/")
    assert list(listed) == [card_ref(CARD)]
    assert listed[card_ref(CARD)].meta["phase"] == "impl"


def test_blob_refs_survive_gc_and_fsck(git_repo):
    store = RefStore(git_repo, "main")
    ref = card_ref(CARD)
    b1, b2 = store.write_blob({"schema": 1, "n": 1}), store.write_blob({"schema": 1, "n": 2})
    store.create(ref, b1)
    head = git(git_repo, "rev-parse", "HEAD")
    tree = git(git_repo, "rev-parse", "HEAD^{tree}")
    new = subprocess.run(["git", "-C", str(git_repo), "commit-tree", tree, "-p", head, "-m", "m"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert store.transaction([Update(ref, b2, b1), Main(new, head)])
    git(git_repo, "gc", "-q", "--prune=now")
    result = subprocess.run(["git", "-C", str(git_repo), "fsck", "--no-dangling"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert store.read(ref).meta["n"] == 2
    assert git(git_repo, "rev-parse", "main") == new
