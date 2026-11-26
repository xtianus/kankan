"""Fetch sharing between pollers and live reads that never wait (board reader technical §5). Each poller is a thread
with its own reader and client: flock locks belong to separate open files, so threads contend as processes do."""
import shutil
import threading
import time

import pytest

from kankan.boardreader import LIVE, BoardReader, max_age
from kankan.trello import TrelloClient
from tests.fakes.fake_trello import KEY, TOKEN


@pytest.fixture
def env(fake_trello, tmp_path, monkeypatch):
    monkeypatch.delenv("KANKAN_TEST_NO_CACHE", raising=False)
    fake = fake_trello
    board = fake.add_board()
    fake.add_card(fake.add_list(board["id"], "Backlog")["id"], "c")

    def reader():
        client = TrelloClient(KEY, TOKEN, base_url=fake.url, timeout=5)
        built = BoardReader(tmp_path / "kankan", client, board["id"])
        client.on_write = built.on_write
        return built
    return fake, reader, tmp_path


def board_reads(fake):
    return sum(1 for m, p in fake.requests if m == "GET" and p.startswith("/1/boards/") and p.count("/") == 3)


def run_together(*calls):
    results, threads = [None] * len(calls), []
    for index, call in enumerate(calls):
        def target(index=index, call=call):
            started = time.monotonic()
            call()
            results[index] = time.monotonic() - started
        threads.append(threading.Thread(target=target))
    for thread in threads:
        thread.start()
        time.sleep(0.05)
    for thread in threads:
        thread.join(20)
    return results


def test_two_pollers_one_board_request(env):
    fake, reader, _ = env
    fake.response_delay = 0.5
    fake.inject("GET", r"^/1/boards/[^/]+$", "delay")
    first, second = reader(), reader()
    run_together(lambda: first.snapshot(max_age(60)), lambda: second.snapshot(max_age(60)))
    assert board_reads(fake) == 1


def test_live_read_does_not_wait_for_slow_poll(env):
    fake, reader, _ = env
    fake.response_delay = 1.5
    fake.inject("GET", r"^/1/boards/[^/]+$", "delay")
    poller, live = reader(), reader()
    times = run_together(lambda: poller.snapshot(max_age(60)), lambda: live.snapshot(LIVE))
    assert times[1] < 1.0 and board_reads(fake) == 2


def test_waiter_gives_up_after_budget_and_fetches(env, monkeypatch):
    from kankan import boardreader
    fake, reader, _ = env
    monkeypatch.setattr(boardreader, "FETCH_WAIT_EXTRA", 0)
    fake.response_delay = 2.0
    fake.inject("GET", r"^/1/boards/[^/]+$", "delay")
    slow, waiter = reader(), reader()
    waiter.client.budget = 0.3
    times = run_together(lambda: slow.snapshot(max_age(60)), lambda: waiter.snapshot(max_age(60)))
    assert times[1] < 1.8 and board_reads(fake) == 2


def test_cache_deleted_while_processes_poll(env):
    fake, reader, tmp_path = env
    first, second = reader(), reader()
    first.snapshot(max_age(60))
    shutil.rmtree(tmp_path / "kankan" / "board-cache")
    assert second.snapshot(max_age(60)).cards and first.snapshot(max_age(60)).cards
    assert board_reads(fake) == 2
