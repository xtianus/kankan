"""The prompt cache expiry note at the end of answers (qapool T§7, F§5; F test 7)."""

import os
import time

import pytest

from kankan import messages, qa_answer, qapool
from kankan.config import atomic_write_json
from kankan.seats import write_proc
from kankan.seatstate import update_state
from kankan.trello import COMMENT_MAX, comment_text
from tests.test_qa_answer import conv, marked  # noqa: F401 - the fixture

OPS = [f"qa-0123456789abcdef-0123456789abcdef-{i}" for i in range(1, 40)]


@pytest.fixture(autouse=True)
def rome(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Rome")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def _reading(project, seat, expires_at):
    write_proc(project, seat, agent=os.getpid())
    from kankan import procutil
    atomic_write_json(project.paths.seats_dir / f"{seat}.cache.json",
                      {"expires_at": expires_at, "ttl": "1h",
                       "agent": {"pid": os.getpid(), "start": procutil.start_time(os.getpid())}, "read_at": time.time()})


def _hhmm(when):
    return time.strftime("%H:%M", time.localtime(when))


def test_note_exact_wording(kproject):
    when = time.time() + 1800
    _reading(kproject, "claude-qa", when)
    assert qapool.note_text(kproject, "claude-qa") == \
        messages.QA_CACHE_EXACT.format(time=_hhmm(when), zone="Europe/Rome")
    assert qapool.note_text(kproject, "claude-qa") == f"Prompt cache expires at {_hhmm(when)} (Europe/Rome)."


def test_note_estimated_for_codex(kproject):
    now = time.time()
    _reading(kproject, "codex-qa", now + 99999)  # Codex reports nothing: a reading never counts
    assert qapool.note_text(kproject, "codex-qa", now=now) == \
        f"Prompt cache may expire at {_hhmm(now + 30 * 60)} (Europe/Rome)."


def test_note_estimated_when_reading_missing_or_past(kproject):
    now = time.time()
    estimated = f"Prompt cache may expire at {_hhmm(now + 60 * 60)} (Europe/Rome)."
    assert qapool.note_text(kproject, "claude-qa", now=now) == estimated  # no reading
    _reading(kproject, "claude-qa", now - 5)
    assert qapool.note_text(kproject, "claude-qa", now=now) == estimated  # expires_at not in the future
    _reading(kproject, "claude-qa", None)
    assert qapool.note_text(kproject, "claude-qa", now=now) == estimated


def test_note_zone_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("TZ", ":America/New_York")
    assert qapool.zone_name() == "America/New_York"
    monkeypatch.delenv("TZ")
    timezone_file = tmp_path / "timezone"
    timezone_file.write_text("Asia/Tokyo\n")
    localtime = tmp_path / "localtime"
    localtime.symlink_to("/usr/share/zoneinfo/Europe/Paris")
    monkeypatch.setattr(qapool, "_ZONE_FILES", (str(timezone_file), str(localtime)))
    assert qapool.zone_name() == "Asia/Tokyo"
    timezone_file.unlink()
    assert qapool.zone_name() == "Europe/Paris"
    localtime.unlink()
    monkeypatch.setattr(time, "strftime", lambda fmt, *args: "CET" if fmt == "%Z" else "")
    assert qapool.zone_name() == "CET"


def test_note_counts_in_chunk_budget():
    note = "Prompt cache expires at 14:32 (Europe/Rome)."
    text = "A" * (COMMENT_MAX - 200)
    one = qa_answer.chunks(text, mention="@bob", op_ids=OPS, note=note)
    assert len(one) == 1 and one[0].endswith("@bob\n\n" + note)
    long = ("word " * 199 + "word\n\n") * 40
    pieces = qa_answer.chunks(long, mention="@bob", op_ids=OPS, note=note)
    assert all(len(comment_text(p, op)) <= COMMENT_MAX for p, op in zip(pieces, OPS))
    assert pieces[-1].endswith("\n\n@bob\n\n" + note) and all(note not in p for p in pieces[:-1])
    near = "A" * (COMMENT_MAX - 80)  # fits without the note, not with it
    assert len(qa_answer.chunks(near, mention="@bob", op_ids=OPS)) == 1
    assert len(qa_answer.chunks(near, mention="@bob", op_ids=OPS, note=note)) == 2


def test_completed_pending_answer_keeps_planned_note(conv, monkeypatch):  # noqa: F811
    from kankan import faults
    planned = "Prompt cache may expire at 01:23 (Europe/Rome)."
    monkeypatch.setattr(qapool, "note_text", lambda project, seat, now=None: planned)
    monkeypatch.setenv("KANKAN_FAULT", "qa_after_chunk")
    with pytest.raises(faults.SimulatedCrash):
        qa_answer.answer(conv.ctx, ("word " * 199 + "word\n\n") * 40, conv.token)
    monkeypatch.delenv("KANKAN_FAULT")
    monkeypatch.setattr(qapool, "note_text", lambda project, seat, now=None: "another note")
    assert qa_answer.complete_pending(conv.project, conv.lease, conv.task) == "answer"
    comments = marked(conv)
    assert len(comments) == 3 and planned in comments[-1] and "another note" not in "".join(comments)
    assert all(planned not in text for text in comments[:-1])


def test_handoff_has_no_note(conv):  # noqa: F811
    qa_answer.answer(conv.ctx, "It works.", conv.token)
    assert "Prompt cache" in marked(conv)[-1]
    from kankan import qa
    token = qa.start_turn(conv.project, conv.lease, conv.task, qa.Inputs([], []))
    from tests.test_qa_answer import fresh_ctx
    qa_answer.handoff(fresh_ctx(conv), "Do the change.", token)
    summary = marked(conv, "qh")
    assert summary and "Prompt cache" not in summary[-1]
