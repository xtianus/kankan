# Project notes

## Specifications

- `aispecs/kankan_spec.md` is the specification of kankan, the CLI that coordinates Claude Code and Codex agents through a Trello board.
- `aispecs/kanban_requirements.md` lists the Kanban API capabilities kankan needs, mapped to the Trello endpoints it uses. It is the reference for anyone writing an adapter for a Trello alternative.
- Implementation plans live in `aiplans/`. The user manual is `docs/manual.md`; observed agent screens and behaviours are in `docs/adapter-notes.md`.

## Conventions

- Python code uses 4-space indentation (PEP 8), overriding the global tabs rule.
- Everything is in English: code, comments, specs, plans, user manual and user-facing messages. User-facing texts live in `kankan/messages.py`.
- Runtime code is standard library only, Python ≥ 3.10 (no `datetime.UTC`, `StrEnum`, `tomllib`...).

## Rules

- Before every commit that adds a call to a Trello API endpoint, changes how an endpoint is used, or stops using one, revise `aispecs/kanban_requirements.md` so that its capability tables and endpoint mappings match the code. The commit must include that revision.

## Architecture

- Module map: `refstore` (only module touching `refs/agent-*`), `trello` (only module talking to Trello), `config`/`properties`/`auditlog`, `seats`/`seatstate`/`procutil`, `cards` (transitions, convergence), `workflow` (choice, claims, `tick`), `integrate` (sync, verify, integrate, review done, block, resolve), `agentcmds` (agent commands), `launcher`/`supervisor`/`forward`/`tmux`/`adapters`, `agents_cmd` (agent, up, autostart), `diag` (status, claims, reconcile, admin), `init`, `cli`.
- `workflow.tick(project, lease)` is one launcher iteration without the agent (convergence, choice, preparation); the hidden `kankan tick --seat <s>` runs it for debugging.
- Seat state (`<gcd>/kankan/seats/<seat>.state.json`) is changed only through `seatstate.update_state`.
- The launcher shares the pane's terminal with the agent: it never prints while the agent runs; events go to `<gcd>/kankan/log.jsonl`.
- Seat recovery terminates the processes of the recorded pane session only when that session was created by a kankan supervisor (`KANKAN_SUPERVISOR_PID` equals the session id) and the session leader's start time still matches; otherwise it kills nothing.

## Testing

- Tests use a fake Trello server (`tests/fakes/fake_trello.py`, with fault injection), a scripted fake agent (`tests/fakes/fake_agent.py`, installed as `claude`/`codex` on a temporary PATH) and private tmux servers (`KANKAN_TMUX_SOCKET`).
- Run the suite in its own session (`setsid -w .venv/bin/python -m pytest -q`): recovery tests kill process sessions. `-m "not slow"` skips the tmux process-chain tests.
- Live Trello tests (`-m trello_live`) need `~/.config/kankan/test.properties` or `KANKAN_TEST_BOARD_URL` + `KANKAN_TEST_CREDENTIALS`, and a board whose description contains `kankan-test-board`.
