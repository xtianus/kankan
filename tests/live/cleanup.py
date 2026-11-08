"""Archive leftovers of interrupted live runs: ``python -m tests.live.cleanup``."""

import sys

from kankan.init import parse_board_url
from kankan.trello import TrelloClient
from tests.live.conftest import MARKER, live_config


def main() -> int:
    config = live_config()
    if config is None:
        print("no test board configured")
        return 1
    url, key, token = config
    client = TrelloClient(key, token)
    board = client.board(parse_board_url(url))
    if MARKER not in (board.get("desc") or ""):
        print(f"board description lacks {MARKER!r}: refusing")
        return 3
    snap = client.snapshot(board["id"])
    for card in snap.cards.values():
        if card.name.startswith("kt-"):
            client.delete_card(card.id)
    for label in snap.labels.values():
        if label.name.startswith("kt-"):
            client.delete_label(label.id)
    for lst in snap.lists.values():
        if lst.name.startswith("kt-") and not lst.closed:
            client.archive_list(lst.id)
    print("cleanup done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
