"""Remove leftovers of interrupted live runs: ``python -m tests.live.cleanup``.

Deletes ``kt-``/``ke-`` cards and ``kt-`` labels, archives ``kt-`` lists and removes any kankan registry block.
The workflow lists and labels of the live agents run stay.
"""

import sys

from kankan.init import parse_board_url
from kankan.trello import TrelloClient
from tests.live.conftest import MARKER, live_config
from tests.live.e2e_support import cleanup_board


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
    for action in cleanup_board(client, board["id"]):
        print(action)
    print("cleanup done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
