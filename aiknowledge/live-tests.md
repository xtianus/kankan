# Live tests (real Trello, real agents)

The procedures for people (creating the board, the Trello app, key and token, taking part in the agents run) are in [developer manual §3](../docs/developer-manual.md#3-live-trello-test-board).

## Live Trello tests (`-m trello_live`)

- Live Trello tests (`-m trello_live`) need `~/.config/kankan/test.properties` or `KANKAN_TEST_BOARD_URL` + `KANKAN_TEST_CREDENTIALS`, and a board whose description contains `kankan-test-board`.
- The test board belongs to the user's account and workspace and kankan's bot account is invited as a normal member, as in real use; the Trello app, its API key and the token all belong to the bot account.
- `tests/live/conftest.py` checks the marker before any write and stops the run with `pytest.exit(..., returncode=3)` when it is missing.
- Tests run with an isolated `HOME`; `tests/conftest.py` sets `KANKAN_REAL_HOME` so the default `~/.config/kankan/test.properties` is read from the real home directory.
- Each run names its lists, cards and labels with the prefix `kt-<timestamp>-<random>` and at the end deletes the cards and labels and archives the lists. `python -m tests.live.cleanup` deletes `kt-`/`ke-` cards and `kt-` labels, archives `kt-` lists and removes any kankan registry block; it refuses a board without the marker.

## Live agents run

- `python -m tests.live.agents_e2e` is a manual live run with real Claude Code and Codex agents on the test board ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents)); it is never collected by pytest. Its helpers (`tests/live/e2e_support.py`) are tested in `tests/test_e2e_support.py`.
- Codex runs its commands with the login `PATH`, so the runner links `~/.local/bin/kankan` to the checkout's `.venv/bin/kankan` for the run and removes the link at the end (`--keep` leaves it while the agents run; `--clean-only` removes it later); it stops when `~/.local/bin/kankan` is another installation. While the link exists, every agent on every project runs the checkout as it is.
- Its cards are named `ke-<timestamp>-<random> <title>`; its run directory is `<temp>/kankan-e2e-<run-id>/`. `--clean` (or `--clean-only`) first kills and removes what earlier runs left: `ai-kankan-e2e-*` tmux sessions, `ke-`/`kt-` cards, the registry block and `kankan-e2e-ke-*` run directories. On failure, timeout or Ctrl-C it saves pane captures, `log.jsonl` and `kankan status` in `<run dir>/diagnostics/`.

## Publishing

- Before publishing the repository, check that no test fixture holds real board URLs or tokens: `git grep -nE 'trello.com/b/|[0-9a-f]{64}'`.
