# kankan stop all and start — technical specification

**Status:** implemented locally ([plan](../aiplans/2026-11-20-stopall.md)).
**Date:** 2026-11-20
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3; this feature changes it as listed in [§7](#7-changes-to-the-initial-technical-specification).
**Companion:** [`stopall_spec_functional.md`](stopall_spec_functional.md) — the behaviour this file implements; read it first.

**Split rule.** The functional file describes what users observe; this file describes how kankan implements it. Section numbers are this feature's own.

## 1. Scope

Module `kankan/stopall.py`, called by `kankan stop` when no seat is given and by `kankan start`. No Trello call is added except the card comments of the immediate stop, made through the existing `diag.seat_recover` path (`kanban_requirements.md` unchanged).

## 2. Inventory

- **Seats:** every entry of `cfg.seats` (work and Q&A seats) with `seats.liveness`; `alive` is running, `unavailable` has leftovers, `dead` and `free` are stopped.
- **Sessions:** `Tmux.sessions_with_option("@kankan_repo")` filtered on the value equal to this clone's path, so sessions of other projects (and the user's) are never touched. This includes the `ai-<project>-<type>-starting-<hex>` sessions of launchers that have not renamed their session yet and the panes of supervisors showing an error.
- **Autostart:** `agents_cmd.unit_path(project).exists()`; the `ExecStart` line of the unit gives the arguments after `kankan up`. Without a unit, the arguments are built from the running seats (`<type>=<count of alive work seats>` and `qa=<type>` for an alive Q&A seat).

## 3. Stop record

`<gcd>/kankan/stopped.json`: `{"up": "<arguments of kankan up>", "autostart": bool, "seats": [seat…], "at": iso}`, written atomically after the confirmation and before anything is stopped, only when the arguments are not empty. A stop with nothing to stop leaves an existing record alone. `kankan start` deletes it after `up` (and `autostart enable`) succeeded.

## 4. Stop sequence

1. Inventory; nothing to do → return `nothing_to_stop` (with the restart commands of an existing record) without prompting.
2. Confirmation through an injectable `confirm(text) -> bool`; the CLI uses `input()` with the prompt on stderr, and refuses with `usage_error` when stdin is not a terminal and `--yes` is absent.
3. Stop record ([§3](#3-stop-record)).
4. `agents_cmd.autostart_disable` inside `try`: a `KankanError` is kept as the autostart outcome, never raised.
5. `stop_requested = True` (via `update_state`) for every alive seat; logged as `stop_all`.
6. Immediate (default): for each alive seat `diag.seat_recover(project, seat, kill=True, comment=messages.STOP_FORCED)`; a `KankanError` (survivors, ref changed) is recorded for that seat and the next ones continue. After waiting up to 5 s for the sessions to close (they close when their supervisor exits), `Tmux.kill_session` for every remaining session of the project.
7. Graceful: wait loop, every `poll` seconds (default 1, through `clock`): re-read liveness; for every seat still alive set `stop_requested` again, because `Launcher.start` clears it (initial T§8.3) and a launcher restarted by its supervisor would otherwise forget the stop; print a line per seat when its `(launcher_state, card)` changes. The loop ends when no seat is alive and no `-starting-` session of the project exists; a `-starting-` session is waited for at most `agents_cmd.UP_WAIT_SECONDS` (it becomes an alive seat, or shows an error and is reported). `KeyboardInterrupt` ends the loop and turns into `needs_action`.
8. Final check: seats `alive`/`unavailable`, sessions of the project, unit file present. Each leftover becomes a `remaining` entry with its remedy; any entry (or an autostart error, or an interrupted wait) raises `KankanError(NEEDS_ACTION, …, data)` carrying the full report.

Report data: `{"stopped": [seat…], "already_stopped": [seat…], "graceful": bool, "sessions_closed": [name…], "autostart": "disabled" | "already disabled" | "error: <message>", "restart": ["kankan up …", "kankan autostart enable …"], "remaining": [{"what", "remedy"}…], "interrupted": bool}`, plus `nothing_to_stop` or `cancelled` when they apply.

## 5. Start

`start_all`: read the record; missing → `KankanError(NEEDS_ACTION)` with `no_record_help` (the CLI checks the record before opening the project, so an unbound clone gets the help instead of `config_error`). Otherwise `agents_cmd.parse_wanted` on the recorded arguments, `agents_cmd.up`, then `agents_cmd.autostart_enable` when `autostart` is true, then delete the record. A failure of `up` or `autostart enable` keeps the record, so `kankan start` can be repeated. `no_record_help` lists `kankan init <board-url>` when `load_config` fails or has no board, then `kankan up` and `kankan autostart enable` with the counts of `cfg.seats` (work seats per type, `qa=<type>` for the Q&A seat), or placeholders.

## 6. Changes to existing code

- `diag.seat_recover(project, seat, kill, comment=None)`: an optional comment template replaces `ADMIN_SEAT_RECOVERED`; the idempotency key changes with it (`stop-force-…`), so the two comments never deduplicate each other.
- `cli`: `stop` takes an optional `seat` and the flags `--graceful` and `--yes` (refused with a seat); new command `start`.
- `Launcher.start` clears a stale `stop_requested` **before** publishing `starting`, no longer after it: a stop requested once the seat shows `starting` (for example `kankan stop <seat>` during startup) was erased. A launcher restarted by its supervisor still clears the request, hence the re-assertion of [§4](#4-stop-sequence) step 7.

## 7. Changes to the initial technical specification

- **§5.2 Local state:** `<gcd>/kankan/stopped.json`, the stop record.
- **§8.5 Cycle and signals:** a stop request can be re-asserted by `kankan stop --graceful` while it waits, after the launcher cleared it at start.
- **§16 CLI:** `stop` without a seat ([§4](#4-stop-sequence)) and `start` ([§5](#5-start)).
