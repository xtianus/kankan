import time

import pytest

from kankan.errors import KankanError
from kankan.tmux import session_name

pytestmark = pytest.mark.tmux


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_session_name_sanitized(tmux_server, tmp_path):
    name = session_name("ai-", "studio.ghezzi.net", "claude-1")
    assert name == "ai-studio_ghezzi_net-claude-1"
    tmux_server.new_session(name, str(tmp_path), ["sleep", "30"])
    assert tmux_server.has_session(name)
    assert name in tmux_server.sessions()
    tmux_server.rename_session(name, "ai-other")
    assert tmux_server.has_session("ai-other") and not tmux_server.has_session(name)


def test_paste_multiline_arrives_whole(tmux_server, tmp_path):
    tmux_server.new_session("paste", str(tmp_path), ["cat"])
    pane = tmux_server.pane_of_session("paste")
    tmux_server.paste(pane, "first line\nsecond line\nthird line", "Enter")
    assert wait_for(lambda: "third line" in tmux_server.capture(pane))
    screen = tmux_server.capture(pane)
    assert screen.index("first line") < screen.index("second line") < screen.index("third line")
    assert not [b for b in tmux_server.buffers() if b.startswith("kankan-")]


def test_interrupt_keys_refused(tmux_server, tmp_path):
    tmux_server.new_session("keys", str(tmp_path), ["cat"])
    pane = tmux_server.pane_of_session("keys")
    with pytest.raises(KankanError):
        tmux_server.send_keys(pane, ["Escape"])
    with pytest.raises(KankanError):
        tmux_server.send_keys(pane, ["C-c"])


def test_options_and_activity(tmux_server, tmp_path):
    tmux_server.new_session("opt", str(tmp_path), ["sleep", "30"])
    tmux_server.set_option("opt", "@kankan_repo", "/x/y")
    assert tmux_server.get_option("opt", "@kankan_repo") == "/x/y"
    assert tmux_server.client_activity("opt") is None


def test_sessions_with_option_in_one_call(tmux_server, tmp_path):
    """Names and a user option are read atomically, so a rename between two calls cannot hide a session."""
    tmux_server.new_session("one", str(tmp_path), ["sleep", "60"])
    tmux_server.new_session("two", str(tmp_path), ["sleep", "60"])
    tmux_server.set_option("one", "@kankan_repo", "/x/y")
    assert tmux_server.sessions_with_option("@kankan_repo") == {"one": "/x/y", "two": ""}
