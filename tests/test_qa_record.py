"""The Q&A record (qa.json) and the conversation directories (questions T§2, T§5)."""

import pytest

from kankan import qa
from kankan.config import project_lock, read_json
from kankan.errors import KankanError

CONV = "0123456789abcdef"


def test_record_add_remove_recorded(kproject):
    paths = kproject.paths
    assert qa.recorded(paths) == set()
    with project_lock(paths):
        qa.record_add(paths, "a" * 24)
        qa.record_add(paths, "b" * 24)
        qa.record_add(paths, "a" * 24)
    assert qa.recorded(paths) == {"a" * 24, "b" * 24}
    assert sorted(read_json(paths.kankan_dir / "qa.json", None)["cards"]) == ["a" * 24, "b" * 24]
    with project_lock(paths):
        qa.record_remove(paths, "a" * 24)
        qa.record_remove(paths, "c" * 24)
    assert qa.recorded(paths) == {"b" * 24}


def test_conversation_dir_and_validation(kproject):
    paths = kproject.paths
    assert qa.conversation_dir(paths, CONV) == paths.kankan_dir / "qa" / CONV
    for bad in ("", "../x", "a/b", "ABC-DEF", "x" * 80):
        with pytest.raises(KankanError):
            qa.conversation_dir(paths, bad)


def test_conversation_lock_is_exclusive_with_timeout(kproject):
    import threading
    paths = kproject.paths
    held, release, outcome = threading.Event(), threading.Event(), {}

    def holder():
        with qa.conversation_lock(paths, CONV, 1):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(5)
    try:
        with qa.conversation_lock(paths, CONV, 0.2):
            outcome["taken"] = True
    except KankanError:
        outcome["taken"] = False
    release.set()
    thread.join()
    assert outcome["taken"] is False
    assert (qa.conversation_dir(paths, CONV) / "op.lock").exists()
    with qa.conversation_lock(paths, CONV, 1):
        pass


def test_discard_conversation(kproject):
    paths = kproject.paths
    directory = qa.conversation_dir(paths, CONV)
    directory.mkdir(parents=True)
    (directory / "turn.json").write_text("{}")
    assert qa.conversations(paths) == [CONV]
    assert qa.discard_conversation(paths, CONV) is True
    assert not directory.exists()
    assert qa.conversations(paths) == []
    assert qa.discard_conversation(paths, CONV) is True  # already gone
    assert qa.discard_conversation(paths, "../evil") is False


def test_discard_conversation_busy_lock_keeps_directory(kproject):
    import threading
    paths = kproject.paths
    held, release = threading.Event(), threading.Event()

    def holder():
        with qa.conversation_lock(paths, CONV, 1):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(5)
    try:
        assert qa.discard_conversation(paths, CONV) is False
        assert qa.conversation_dir(paths, CONV).exists()
    finally:
        release.set()
        thread.join()


def test_q_and_a_code_keeps_its_user_facing_texts_in_messages():
    """Global constraint: user-facing texts live in messages.py. No ``KankanError`` of the Q&A modules, nor of
    the functions the feature added elsewhere, is raised with an inline text."""
    import ast
    import inspect
    from kankan import agents_cmd, diag, qa_answer, qa_launcher, seats
    sources = [inspect.getsource(module) for module in (qa, qa_answer, qa_launcher)]
    sources += [inspect.getsource(function) for function in (
        agents_cmd.parse_wanted, agents_cmd._wait_started, seats.acquire_qa, diag._qa_seat_remove)]
    inline = []
    for source in sources:
        for node in ast.walk(ast.parse(inspect.cleandoc(source) if source.startswith(" ") else source)):
            name = getattr(node, "func", None)
            name = getattr(name, "id", None) or getattr(name, "attr", None)
            if isinstance(node, ast.Call) and name in ("KankanError",) and len(node.args) > 1 \
                    and isinstance(node.args[1], (ast.Constant, ast.JoinedStr, ast.BinOp)):
                inline.append(ast.unparse(node.args[1]))
    assert inline == []
