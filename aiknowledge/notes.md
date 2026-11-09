# Project notes

## Specifications

- [`aispecs/kankan_spec.md`](../aispecs/kankan_spec.md) is the specification of kankan, the CLI that coordinates Claude Code and Codex agents through a Trello board.
- [`aispecs/kanban_requirements.md`](../aispecs/kanban_requirements.md) lists the Kanban API capabilities kankan needs, mapped to the Trello endpoints it uses. It is the reference for anyone writing an adapter for a Trello alternative.
- Implementation plans live in `aiplans/`. The user manual is [`docs/user-manual.md`](../docs/user-manual.md), the developer manual (tests, live Trello test board) is [`docs/developer-manual.md`](../docs/developer-manual.md); observed agent screens and behaviours are in [`docs/adapter-notes.md`](../docs/adapter-notes.md).

## Conventions

- Python code uses 4-space indentation (PEP 8), overriding the global tabs rule.
- Everything is in English: code, comments, specs, plans, user manual and user-facing messages. User-facing texts live in `kankan/messages.py`.
- Runtime code is standard library only, Python ≥ 3.10 (no `datetime.UTC`, `StrEnum`, `tomllib`...).

## Rules

- Before every commit that adds a call to a Trello API endpoint, changes how an endpoint is used, or stops using one, revise [`aispecs/kanban_requirements.md`](../aispecs/kanban_requirements.md) so that its capability tables and endpoint mappings match the code. The commit must include that revision.

## Architecture

- Module map: `refstore` (only module touching `refs/agent-*`), `trello` (only module talking to Trello), `config`/`properties`/`auditlog`, `seats`/`seatstate`/`procutil`, `cards` (transitions, convergence), `workflow` (choice, claims, `tick`), `integrate` (sync, verify, integrate, review done, block, resolve), `agentcmds` (agent commands), `launcher`/`supervisor`/`forward`/`tmux`/`adapters`, `agents_cmd` (agent, up, autostart), `attachments` (task inputs, download cache; called by `task` outside the lock), `diag` (status, claims, reconcile, admin), `doctor` (checks, fixes, preflight), `init`, `cli`.
- The agents integrate into `cfg.integration_branch` (default `kankan-agents`; old configs with `main_branch` are read as an alias); `cfg.user_branch` is the user's. Only `integrate` moves/pushes the integration branch (`pre-push` hook + `KANKAN_PUSH=1`); user work enters through merge-request cards (`integrate.merge_request`, SHA pinned at request time). The JSON key `main`, `push_main` and `main_moved` keep their names for compatibility.
- Codex 0.167 runs shell commands through a shared `codex app-server --managed-daemon`: they get the daemon's environment (login PATH, no `KANKAN_*`). Agent commands therefore fall back to `<worktree git dir>/kankan-agent.json` (written by the launcher), and `kankan` must be on the login PATH.
- `build.code_id()` hashes the kankan package; the launcher stores it in the seat file and agent commands refuse to run with different code (editable installs make this likely while developing kankan).
- Trello facts: attachment downloads answer 200 without a redirect; an upload's `fileName` is stable across renames; `mimeType` can be null.
- `TrelloClient` never follows redirects automatically (`_OPENER` with a no-redirect handler) and sends the credential with `add_unredirected_header`: a redirected JSON call fails; `download` follows at most 3 redirects itself, with the credential only on the `trello_base_url` origin and never downgrading https to http (`_redirect_auth`). Download URLs are validated and requested on `trello_base_url`.
- `attachments` (spec §13.3) is the policy of card attachments as task inputs; `agentcmds.task` calls `attachments.for_task`. Uploads by `notify.input_members` (board members minus kankan's account) are downloaded to `<gcd>/kankan/attachments/<shortLink>/<att-id>/<sanitized fileName>`. Every change inside a card's directory happens under the per-card attachment lock (`<gcd>/kankan/attachment-locks/<shortLink>.lock`, ordered after the project lock); `task` never takes the project lock. `cards.delete_integrated_ref` is the only way an integrated ref is deleted in `Done`. `attachments.discard` never waits (timeout 0) and never follows symlinks; a `task` holding the lock deletes the directory itself when its `still_held` check returns `Hold.GONE` (ref deleted), and never for `Hold.OTHER` (another owner or phase, or an unreadable ref: the copies may be another seat's). A ref deleted after the `task`'s final check but before it releases the lock leaves the directory behind (`discard` found the lock busy); `reconcile --fix` removes it. Names are sanitized to at most 255 UTF-8 bytes; the attachment directories are created with mode 0700; once a download hop leaves the API origin, no later hop carries the credential.
- kankan acts through its own Trello account (`cfg.kankan_member_id`, the token's owner). `notify.mention` mentions a card's creator, or the board's admins for cards kankan created; `notify.answers` forwards comments by board members other than kankan's account. A person's own token is unsupported (Trello never notifies anyone about their own comments, and their answers would be ignored): `init` warns, `doctor` gives a manual step.
- `doctor` checks and fixes the installation and project; its offline subset is the `up`/`agent` preflight. The launcher and `agent` put `<gcd>/kankan/bin` (only a `kankan` wrapper running `sys.executable -m kankan`) first on the agents' `PATH` (`agents_cmd.agent_path`): never the interpreter's own directory, which would shadow the project's python/pip.
- `workflow.tick(project, lease)` is one launcher iteration without the agent (convergence, choice, preparation); the hidden `kankan tick --seat <s>` runs it for debugging.
- Seat state (`<gcd>/kankan/seats/<seat>.state.json`) is changed only through `seatstate.update_state`.
- The launcher shares the pane's terminal with the agent: it never prints while the agent runs; events go to `<gcd>/kankan/log.jsonl`.
- Card refs carry `from_list` only while a Trello move is unconfirmed: after a verified move `cards.confirm_move` rewrites the ref without it, so convergence moves a card only from its recorded source and never replays a move over a later human move.
- Transitions of held cards (`impl`/`review`) by a seat require the card to be open and in its expected list; only the user-move parking (`cause` `user_*`) is exempt. `integrate` and review `done` apply the same rule and re-check seat ownership and task identity under the project lock.
- The launcher checks its seat lease every poll interval and exits when `seat recover` revoked it.
- `seat recover --kill` terminates only the recorded processes (pid + start time); a whole session is terminated only when it is the recorded supervisor-led pane session.
- Seat recovery terminates the processes of the recorded pane session only when that session was created by a kankan supervisor (`KANKAN_SUPERVISOR_PID` equals the session id) and the session leader's start time still matches; otherwise it kills nothing.

## Testing

- Tests use a fake Trello server (`tests/fakes/fake_trello.py`, with fault injection), a scripted fake agent (`tests/fakes/fake_agent.py`, installed as `claude`/`codex` on a temporary PATH) and private tmux servers (`KANKAN_TMUX_SOCKET`).
- Run the suite in parallel and in its own session: `setsid -w .venv/bin/python -m pytest -q -n auto` (pytest-xdist; each test has its own temp dir, HOME and tmux socket). Tests skip `kankan init`'s settle wait (conftest).
- Run the suite in its own session (`setsid -w .venv/bin/python -m pytest -q`): recovery tests kill process sessions. `-m "not slow"` skips the tmux process-chain tests.
- Tests set `KANKAN_SKIP_PREFLIGHT=1` (autouse); `tests/test_doctor.py` unsets it where it matters. `tests/helpers.bind_project` binds with `integration_branch="main"`; new tests pass `integration_branch="kankan-agents"`.
- `python -m tests.live.agents_e2e` is a manual live run with real Claude Code and Codex agents on the test board ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents)); it is never collected by pytest. Its helpers (`tests/live/e2e_support.py`) are tested in `tests/test_e2e_support.py`.
- Never call `monkeypatch.undo()` in a test: it also undoes the autouse isolated HOME.
- The fake Trello server serves attachment uploads (`upload`, `remove_attachment`, `redirect_download`, the `truncate` fault, downloads counted in `downloads` (racy when the client aborts a too-large download: assert on `auth_log`/`requests` instead), a `404` fault echoing the credential, `auth_log` records whether each request carried the credential); the `external_host` fixture (`tests/fakes/external_host.py`) is an HTTP server on `localhost`, another origin than the fake's `127.0.0.1`, for redirect tests.
- Live Trello tests (`-m trello_live`) need `~/.config/kankan/test.properties` or `KANKAN_TEST_BOARD_URL` + `KANKAN_TEST_CREDENTIALS`, and a board whose description contains `kankan-test-board`.
