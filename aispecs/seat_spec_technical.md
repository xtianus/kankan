# kankan seat commands — technical specification

**Status:** specified, not implemented.
**Date:** 2026-11-22; revised the same day after an independent review (integrated cards, pending moves, removal mode and restart, completion order, Q&A pending answers, labels).
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3, [`questions_spec_technical.md`](questions_spec_technical.md) and [`stopall_spec_technical.md`](stopall_spec_technical.md); this feature changes them as listed in [§10](#10-changes-to-other-technical-specifications).
**Companion:** [`seat_spec_functional.md`](seat_spec_functional.md) — the behaviour this file implements; read it first.

**Split rule.** The functional file describes what users observe; this file describes how kankan implements it. Section numbers are this feature's own.

## 1. Scope

- `kankan/cli.py`: the `seat` command gains `start`, `stop`, `--attach`, `--graceful` and `--yes`; `stop` loses its `seat` argument.
- New module `kankan/seatremove.py`: inventory, destinations, immediate removal, graceful request, completion, autostart adjustment.
- `kankan/cards.py`: the `Implemented` destination falls back when the implementer's seat is gone or being removed ([§3](#3-implemented-destination)).
- `kankan/config.py`: `SeatConfig.removing` and `SeatConfig.remove_error`.
- `kankan/workflow.py`, `kankan/launcher.py`, `kankan/qa_launcher.py`, `kankan/seats.py`, `kankan/agents_cmd.py`, `kankan/stopall.py`, `kankan/diag.py`, `kankan/integrate.py`, `kankan/agentcmds.py`: a seat being removed ([§5](#5-graceful-removal)) and the shared destination ([§3](#3-implemented-destination)).
- `kankan/qa_launcher.py`, `kankan/qa.py`: the Q&A first turn with nothing new ([§8](#8-qa-first-turn-with-nothing-new)).

Trello calls: only those kankan already makes (card read, move with `pos=top`, comment, label removal, list archive), so `kanban_requirements.md` is unchanged; the plan checks it against the calls actually used.

## 2. CLI

- `kankan seat {start,stop,recover,remove} <seat> [--attach] [--kill] [--graceful] [--yes]`: `--attach` only with `start`, `--kill` only with `recover`, `--graceful`/`--yes` only with `remove`; any other combination is `usage_error`.
- `seat start`: `seats.validate_seat`; not in `cfg.seats` → `usage_error` (`messages.SEAT_START_UNKNOWN`, naming `kankan agent <type>` and `kankan up`); `removing == "immediate"` → `usage_error` (`messages.SEAT_START_REMOVING`); `liveness == "alive"` → `{"seat", "started": false}`, "already running". Otherwise `agents_cmd.agent(project, seats.seat_type(seat), seat=seat, attach=args.attach, qa=seats.is_qa(seat))`, which runs the preflight, recovers a dead seat and refuses an open session or survivors as today; without `--attach`, `agents_cmd._wait_started` until the seat is `alive` (`UP_NOT_STARTED` otherwise). Returns `{"seat", "started": true, "session"}`. A `graceful` seat is started like any other: `choose` restricts it ([§5](#seat-behaviour-while-removing)).
- `seat stop`: the body of the former `_cmd_stop` seat branch (unknown seat → `usage_error`; when `alive`, `stopall.refuse_other_code` with `messages.STOP_SEAT_OTHER_CODE`; then `stop_requested`). The message names `kankan seat recover <seat> --kill` and `kankan stop`.
- `stop`: no positional argument; `--graceful`/`--yes` as before. `kankan stop <seat>` is rejected by argparse.
- Every text naming `kankan stop <seat>` (`messages.py`, `launcher.py`, `doctor.py`, the user manual, the skill if it names it) names `kankan seat stop <seat>`.
- The confirmation of `seat remove` uses the same `confirm` helper as `_stop_all` (prompt on stderr, `usage_error` without a terminal and without `--yes`).

## 3. `Implemented` destination

`cards.implemented_list(cfg, implementer) -> Optional[str]`, used by `target_list_for` for phase `implemented` in place of `cfg.seats[implementer].implemented`:

1. the implementer's `Implemented` when the seat is registered and not `removing`;
2. otherwise the `Implemented` of the first work seat (name order) of the implementer's type (`seats.seat_type(implementer)`) that is not `removing`;
3. otherwise the `Implemented` of the first work seat of any type that is not `removing`;
4. otherwise `None`.

`cards.implemented_destination(cfg, meta) -> (phase, target_list, cause)` returns `("implemented", list, None)`, or `("parked", Backlog, "seat_removed")` when `implemented_list` is `None`. Every transition to `implemented` uses it, computed from the configuration re-read under the project lock of the transition: `done` (`agentcmds.py:348`), the review `yield` (`agentcmds.py:374`), the rollback of a review claim that met `HOLD` (`workflow.py:229`), `integrate` returning a card (`integrate.py:647`), `resolve --done`, and the transitions of [§4.1](#41-card-transitions). The commands report the destination actually used.

`implementer` and `implementer_type` in the ref keep naming the removed seat: review eligibility reads `implementer_type` ([initial §9.2](initial_spec_functional.md#92-eligibility)), so a card in another seat's `Implemented` list is still reviewed only by the other type. This fixes two existing failures once the implementer's seat is gone: `cfg.seats[implementer]` raises `KeyError` (`cards.py:94`), and `resolve --done` refuses with `needs_reconciliation` (`integrate.py:644`) after it already removed the blocked worktree; the refusal is dropped. During a graceful removal the seat's own `done` goes straight to another seat's `Implemented` ([functional §3.3](seat_spec_functional.md#33-graceful-removal---graceful) step 2).

`target_list_for` maps cause `seat_removed` of phase `parked` to `Backlog`.

## 4. Immediate removal

`seatremove.remove(project, seat, *, confirm, say)`; every step is repeatable, so a new run completes an interrupted one.

1. **Inventory** (Trello reads outside the lock): `seats.liveness`; the board snapshot; all card refs. The cards concerned ([§4.1](#41-card-transitions)) are the open cards in the seat's three lists, the cards whose ref has `owner == seat`, and the cards whose ref has a `target_list` among the seat's lists (a move not completed yet, which convergence would otherwise replay into an archived list). Dirty files and an unfinished merge of the seat worktree. A malformed ref ([initial §15.3](initial_spec_functional.md#153-kankan-reconcile---fix)) stops the removal with `NEEDS_ACTION` naming it: its card could be any of the rows.
2. **Plan**: each card with its destination. For the Q&A seat: the active conversation from `qa._owned`.
3. **Confirmation** when the plan has cards (work seat) or an active conversation (Q&A seat), with the text of [functional §3.2](seat_spec_functional.md#32-immediate-removal-default) step 2 listing the plan. Declined → `{"cancelled": True}`, nothing changed.
4. **Mark**, under the project lock: `removing = "immediate"` saved in the seat's entry, so from now on the seat claims nothing ([§5](#5-graceful-removal)) and an interruption is completed by the next `seat remove` or `kankan up`. Autostart and stop record ([§7](#7-autostart-and-stop-record)).
5. **Stop**, under the project lock: `seats.recover_dead(project, seat, kill_alive=True)`; survivors → `KankanError(NEEDS_ACTION)` naming `kankan seat recover <seat> --kill`; then delete the seat ref conditionally on its value (as `diag.seat_recover`, without its comment).
6. **Unfinished merge** (work seat): `git merge --abort` when one is in progress, so the card branch is left at its last commit. Uncommitted changes are not saved: the worktree is removed with `--force` at completion. Nothing is committed by kankan.
7. **Completion** ([§6](#6-completion)), which re-reads the cards after the stop: a card the seat claimed between the confirmation and the mark is moved by the same rules. The confirmation does not ask again for it.

### 4.1 Card transitions

Each card is classified from its ref read again under the project lock of its own transition, so a card another reviewer claimed meanwhile is classified by its new ref. The transition uses `transition(project, AdminActor("seat remove"), card, value, new, comment)` ([initial T§10.3](initial_spec_technical.md#103-phases-and-transitions)). Rows are tried in order; the first that matches applies:

| # | Card | New ref | Destination |
|---|---|---|---|
| 1 | ref `integrated` (integrate done, move to `Done` pending) | unchanged | `Done`, then the closing of `integrate` (ref deleted once `Done` is verified, the late-`HOLD` comment kept), as convergence does |
| 2 | ref `impl`, owner the seat | `parked`, owner none, cause `seat_removed` | `Backlog` top |
| 3 | ref `review`, owner the seat | `implemented_destination` | its list |
| 4 | ref `qa`, `impl` or `review` held by another seat | unchanged | the ref's `target_list` (the holder's list), as convergence does |
| 5 | ref `parked` with cause `block` or `handoff`, or any ref whose `target_list` is outside the seat's lists | unchanged | the ref's `target_list` (`Blocked`, `Backlog`…): the pending move is completed; a `blocked-<short>` worktree and a handoff's summary and conversation are untouched |
| 6 | ref `implemented` (any implementer), or `parked` with an implementer whose card is in a seat's `Implemented` | `implemented_destination` | its list |
| 7 | any other open card in the seat's lists (no ref, or another `parked` ref) | `parked` when a ref exists, cause `seat_removed` | `Backlog` top; a card without ref is moved and commented as `diag._fix_stranded` does |
| 8 | the seat's ref on a card that is closed or deleted | `parked`, owner none, no target | none (the user's archive or deletion prevails, [initial §14.6](initial_spec_functional.md#146-cards-moved-by-the-user-safety-net)) |

Every moved card gets a comment through `comment_once` with op-id `seat-remove-<seat>-<card id>`, posted after the transition (not through `transition`'s own comment, whose failure is only logged); a comment still missing is posted again by the next run, so a crash between a move and its comment does not lose it. `HOLD` is kept. The labels `Question`, `Error` and `Resume` whose cause the removed seat recorded for the card ([initial §14.1](initial_spec_functional.md#141-labels)) are cleared, since the seat's state holding those causes is deleted at completion; other labels are untouched. A failed Trello move leaves the ref in its new state for convergence and is caught by the check of [§6](#6-completion) step 3.

For the Q&A seat, after step 5: `qa.end_admin(project, seat)` ends the conversation (ref deleted, conversation and attachment directories discarded). No card moves.

## 5. Graceful removal

`seatremove.request_graceful(project, seat, *, say, poll)`:

1. Unknown seat → `usage_error`. When `alive`, `stopall.refuse_other_code(project, [seat], messages.SEAT_REMOVE_OTHER_CODE)`.
2. Under the project lock: `removing = "graceful"` saved in the seat's entry; logged `seat_remove_requested`. Autostart and stop record ([§7](#7-autostart-and-stop-record)), now, so a reboot during the drain does not start a replacement.
3. Not alive: when the seat still has work ([below](#seat-behaviour-while-removing)), start it with `agents_cmd.agent(project, type, seat=seat)` (preflight included; a preflight failure is reported with `NEEDS_ACTION`, the flag stays); otherwise run the completion ([§6](#6-completion)) at once and return.
4. Wait loop as `stopall._wait` (poll through `clock`, a line per change of `(launcher_state, card)`). It ends with the completion's result when the seat is no longer registered, with `NEEDS_ACTION` and the recorded error when `remove_error` is set ([§6](#6-completion)), and with `NEEDS_ACTION` "the agent stopped before finishing; `kankan up` starts it again" when the seat is no longer alive while it still has work. `KeyboardInterrupt` → `NEEDS_ACTION` with the report so far; the flag stays.

### Seat behaviour while removing

**Work left:** a work seat has work while it holds a ref in phase `impl` or `review`. The Q&A seat has work while a turn is running or its conversation has a pending answer or handoff (`pending.json`).

- `workflow.choose` and the locked recheck in `workflow.tick` (`workflow.py:289`, next to `stop_requested`): only `_own_choices` (own work, including the `hold_yield`/`moved_yield` cleanups); no review or `Backlog` choice. `seats.acquire` refuses a `removing` seat unless `--seat` names it.
- Work seat launcher: when it has no work left — in `idle` after `tick` returned `None`, and in `limited` without a task — log `seat_drained` and exit as for a stop. A held card that cannot be chosen now (no model for the role, a settings block) keeps it waiting. `idle_exit_minutes` does not apply to a `removing` seat that has work.
- Q&A launcher: `removing` acts as a stop request only once it has no work left: after `complete_pending` at startup and after the current turn. So a pending answer is completed, never cut short, also after a launcher restart (which clears `stop_requested` but not `removing`).
- Launcher shutdown: after `seats.release` (seat ref deleted, flock released), when the entry is still `removing` and the seat has no work left, call the completion. A failure is saved as `remove_error` (message and remedy) and logged; the next `kankan up` or `kankan seat remove` retries.
- A `CONFIG_ERROR` exit (`launcher.py:607`) leaves the seat with work and stopped: the waiter reports it, `kankan up` restarts it.
- `seats._candidates` excludes `removing` seats; `agents_cmd.live_counts`, `agents_cmd.registered_args` and `stopall._args_from_seats` skip them.
- `agents_cmd.up` **first** handles the `removing` seats that are not alive: `graceful` with work left → started with `--seat`; otherwise (`immediate`, or nothing left) → completion. Then it starts the requested agents. `kankan up` without arguments does this even when no other seat is registered (it prints the start help only when nothing at all is registered or removing); `kankan start` without a stop record does it before printing its help.
- `diag.status` adds `"removing": "graceful"|"immediate"` and `remove_error` to the seat's row.
- **Repeated request** ([functional §3.7](seat_spec_functional.md#37-repeated-request)), decided from the entry read under the project lock before anything else:
  - `removing == "graceful"` and `--graceful`: `KankanError(NEEDS_ACTION, messages.SEAT_REMOVE_GRACEFUL_RUNNING, {"seat", "liveness", "launcher_state", "card"})`; nothing written.
  - `removing == "graceful"` without `--graceful`: the inventory and plan of [§4](#4-immediate-removal) steps 1–2, then one `confirm` with `messages.SEAT_REMOVE_ESCALATE` (the running notice, then the plan) instead of the step 3 question, also when the plan is empty. Declined → `{"cancelled": True, "graceful_running": True}`, the entry unchanged. Confirmed → step 4 sets `removing = "immediate"` and the removal continues from step 4; the launcher of the seat, killed at step 5, never reaches its own completion.
  - `removing == "immediate"`, with or without `--graceful`: completion ([§6](#6-completion)) after the stop of step 5, without confirmation (it was given).

## 6. Completion

`seatremove.complete(project, seat)`, used by both modes; each step is repeatable and the configuration entry is deleted **last**, so an interrupted completion is found and finished by the next `seat remove` or `kankan up`.

1. Under the project lock: `alive` → `NEEDS_ACTION` (the agent still runs); `dead` or `unavailable` → the stop of [§4](#4-immediate-removal) step 5 first (leftovers killed, seat ref deleted).
2. Work seat: the inventory of [§4](#4-immediate-removal) step 1 again and the card transitions of [§4.1](#41-card-transitions), then the missing comments. Q&A seat: `qa.end_admin`.
3. Re-read the board and the refs: when an open card is still in one of the seat's lists, or a ref still targets one of them, stop with `NEEDS_ACTION` naming the cards (saved as `remove_error`); nothing below runs.
4. Archive the three lists (`archive_list`; an already archived list is not an error), `git worktree remove --force` the seat worktree (discarding uncommitted changes; a missing worktree is not an error), delete the seat's state and lock files.
5. Delete the configuration entry; logged `seat_removed` with the moves.
6. Return `{"seat", "moved": [{"card", "short", "to"}…], "discarded": [file…], "lists_archived": n, "worktree_removed": bool, "autostart": "unchanged"|"<new arguments>"|"disabled"|"error: …"}`. A graceful waiter returns the same report, read from the log entry `seat_removed`.

## 7. Autostart and stop record

Applied when the removal is requested (immediate step 4, graceful step 2), so the next boot or `kankan start` never asks for the removed seat. `stopall._unit_args` gives the unit's arguments; `agents_cmd.parse_wanted` parses them. For the seat's type the count becomes `min(count, work seats of that type registered and not removing)`, dropped when zero; for the Q&A seat `qa=` is dropped. Unchanged arguments leave the unit alone; empty arguments → `agents_cmd.autostart_disable`; otherwise `agents_cmd.autostart_enable` with the new counts. A `systemctl` failure is reported in the result (`needs_action` at the end) and does not stop the removal. The record `stopped.json` gets the same change of its `up` field; a record left empty is deleted. A type already at its cap is unaffected: counts only go down.

## 8. Q&A first turn with nothing new

In `QaLauncher` both calls of `qa.first_turn` (`qa_launcher.py:346`, the taken-up conversation that crashed between its claim and its first turn, and `:357`, the new conversation) follow this rule.

`qa.first_turn` also returns whether the latest answer of the thread is **complete**: an answer or handoff comment by kankan's account without a chunk prefix, or with the prefix `n/n` (its last part, `qa_answer.py:94`). A thread whose latest marked comment is a part `k/n` with `k < n` (an answer cut short) counts as not answered: the first turn is prompted as today.

When the latest answer is complete and the returned inputs are empty, the launcher does not call `begin_turn`. It records the context as covered — `qa.record_covered(project, lease, task, context)`: under the conversation lock and the project lock, the ref's `covered` set to `qa.coverage(None, Inputs(), context)` in a transaction verifying the seat ref, as `qa_answer._finish` does — and then calls `wait_after_restart(task)`. The following delivery goes through the restart path (`ref_covered` set), so only what arrives later is new and the prompt asks to read the whole thread (`QA_RESTART_FOLLOWUP`). A transaction failure ends the conversation as a `NOT_OWNER` turn start does. When the question was only in the card description the context has no comment: `covered` is then `{"comment": null, "attachments": […]}`, a non-empty value, so the restart path recognises it and the description is not delivered again.

Limitation, as in the existing `first_turn` (dates from Trello): a comment posted while the latest answer was being written is dated before its last part and counts as answered context, not as new.

## 9. Tests

- Unit: `implemented_list` order and `None`; `implemented_destination` in `done`, review `yield`, the `HOLD` rollback, `integrate` and `resolve --done` with a removed implementer (no `KeyError`, no refusal, reported destination); each row of [§4.1](#41-card-transitions) on a fake board, including an `integrated` card in `Review`, a `parked/block` card whose move to `Blocked` failed, a parked handoff, a closed held card, a ref targeting the seat's lists from another list, a malformed ref (refused), a card claimed by another reviewer during the removal; a rerun after a crash between a move and its comment (comment posted once); a failed move (lists not archived, `remove_error`, rerun completes); a crash after the archive (entry still present, rerun completes); labels cleared; uncommitted changes discarded and an unfinished merge aborted; completion of a `dead` seat; autostart and stop record adjustment at request time; `choose`, the `tick` recheck and `acquire` for a `removing` seat; `_candidates`, `live_counts`, `registered_args` skipping it; `up` handling `removing` seats first, also with no other seat registered; CLI flag combinations; `kankan stop <seat>` rejected; `seat start` (stopped seat started and awaited, running seat untouched, unknown seat refused without creating it, Q&A seat, `immediate` seat refused, `graceful` seat started); the repeated request (graceful again: `needs_action` and nothing written; without `--graceful`: one question, declined leaves `graceful`, confirmed or `--yes` removes at once, no terminal without `--yes` is `usage_error`; `immediate` interrupted: completed without a question).
- Launcher (fake agent in tmux): a `removing` seat finishes its implementation, exits and completes; a seat stopped while `removing` is restarted by `up` and completes; a `limited` removing seat without a task exits; Ctrl-C of the wait leaves the flag; the waiter reports a launcher that stopped with work left.
- Q&A: removal of the Q&A seat with an active conversation; graceful removal with a pending answer after a launcher restart completes the answer first; a new conversation on an answered card with nothing new sends no prompt, and a later comment is delivered as the only new input with the restart wording; a thread whose latest answer is a part `1/3` is prompted; a description-only answered card waits.
- The acceptance tests of [functional §5](seat_spec_functional.md#5-acceptance-tests).

## 10. Changes to other technical specifications

- **Initial T§5.2 Local state:** `config.json` seat entries gain `removing` (`"graceful"`, `"immediate"`, absent) and `remove_error`.
- **Initial T§7.3 Occupation:** a `removing` seat is occupied only with `--seat` (by `up` to finish its cards).
- **Initial T§8.4–8.5 State machine, cycle and signals:** a `removing` work seat chooses only its own work, ignores `idle_exit_minutes` while it has work and exits when it has none (also from `limited`); its launcher completes the removal after releasing the seat.
- **Initial T§10.3 Phases and transitions:** the rows of [§4.1](#41-card-transitions); every transition to `implemented` uses `implemented_destination` ([§3](#3-implemented-destination)); cause `seat_removed`.
- **Initial T§10.4 Convergence:** no ref targets an archived seat list after a removal ([§6](#6-completion) step 3).
- **Initial T§16 CLI:** `seat start [--attach]`, `seat stop`, `seat remove --graceful --yes`; `stop` without `seat`.
- **Stopall T§2–T§6:** `_args_from_seats` skips `removing` seats; the stop record is adjusted by removals ([§7](#7-autostart-and-stop-record)); `start` handles `removing` seats ([§5](#seat-behaviour-while-removing)); the seat branch of `_cmd_stop` moves to `seat stop`.
- **Questions T§2 Configuration and seat:** `seat remove <type>-qa` no longer requires a free seat. **T§4 Launcher:** `removing` as a deferred stop ([§5](#seat-behaviour-while-removing)); the first turn with nothing new ([§8](#8-qa-first-turn-with-nothing-new)). **T§5 Conversation record:** `covered` recorded without an answer by `record_covered`.
