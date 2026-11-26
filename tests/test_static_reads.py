"""Only ``boardreader`` calls the read methods of ``TrelloClient`` (board reader technical §2)."""
import re
from pathlib import Path

import kankan

READS = ("me", "member", "card_creator", "board_members", "board_admins", "board", "board_payload", "snapshot",
         "closed_cards", "card_place", "card", "settings_cards", "attachments", "list_info", "comments",
         "find_comment", "read_board", "read_card", "read_comments", "read_board_comments", "read_members", "read_me")
CALL = re.compile(r"\b(?:trello|client)\.(" + "|".join(READS) + r")\b(?!_)")  # calls and bound methods
def offenders():
    root = Path(kankan.__file__).parent
    found = {}
    for path in sorted(root.glob("*.py")):
        if path.stem in ("boardreader", "trello"):
            continue
        calls = sorted({m.group(1) for m in CALL.finditer(path.read_text())})
        if calls:
            found[path.stem] = calls
    return found


def test_static_only_boardreader_reads():
    assert offenders() == {}
