# kankan stop all and start — functional specification

**Status:** implemented locally ([plan](../aiplans/2026-11-20-stopall.md)).
**Date:** 2026-11-20
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3 and [`questions_spec_functional.md`](questions_spec_functional.md); this feature changes them as listed in [§7](#7-changes-to-the-initial-specification).
**Companion:** [`stopall_spec_technical.md`](stopall_spec_technical.md) — how kankan implements this feature.

**Split rule.** This file describes what users observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

kankan has no single process to stop: each agent runs in its own tmux session, and `kankan stop <seat>` stops one seat. `kankan stop` without a seat stops **every** agent of the project (work seats and the Q&A seat) and disables autostart, in one command that can be repeated safely; `kankan start` starts again what it stopped.

`kankan stop <seat>` keeps its initial behaviour ([initial §8](initial_spec_functional.md#8-processes-and-launcher), [§16.1](initial_spec_functional.md#161-user-commands)).

## 2. `kankan stop [--graceful] [--yes]`

1. **Inventory.** kankan lists the seats that are running, the project's tmux sessions and whether the autostart unit is installed. When nothing runs, no session is open and autostart is off, it says so and exits successfully, without asking anything.
2. **Confirmation.** It prints what it will stop and disable and asks `[y/N]`; without `--graceful` it says that the agents' work will be interrupted and resumed at the next start. `--yes` skips the question; without a terminal (scripts, `--json` piped) `--yes` is required (`usage_error` otherwise). Any answer other than `y`/`yes` cancels: nothing is changed.
3. **Stop record.** It records what is running and whether autostart is enabled, for `kankan start` ([§3](#3-kankan-start)).
4. **Autostart.** It disables and removes the autostart unit, so the agents do not come back at the next boot. A unit already absent is not an error. A `systemctl` failure does not abort the stop: it is reported and makes the result `needs_action`.
5. **Immediate stop** (default). kankan quits every running seat at once, as `kankan seat recover <seat> --kill` does: the agent and its launcher are terminated, the seat keeps its cards, and their next start resumes them from the work left in the seat worktree (an interrupted Q&A answer is completed, [questions §3.7](questions_spec_functional.md#37-restarts)). Each card held by such a seat gets a comment saying that `kankan stop` stopped its seat. kankan also closes the project's remaining tmux sessions (for example a session showing a launcher error).
6. **Graceful stop** (`--graceful`). Every running seat gets the stop request of `kankan stop <seat>`. kankan says that it is waiting for the agents to stop gracefully, each after its current task (a Q&A agent after its current answer), and that `kankan stop` without `--graceful` quits them immediately. While it waits it prints each running seat with its launcher state and card whenever that changes. A seat can wait for a long time: a task in progress, a question waiting for your answer (`waiting_human`), a usage limit with a task (`limited`). Ctrl-C ends the waiting only: the stop requests stay and the agents still stop after their task; the command then reports `needs_action` with the seats still running. Sessions in which no agent runs (a launcher error) are reported, not closed.
7. **Final check.** kankan checks that no seat runs and none has leftover processes (`unavailable`), that no tmux session of the project is open and that the autostart unit is gone. Anything left is listed with what to do, and the result is `needs_action` (exit 14); otherwise the command succeeds.
8. **Report.** It lists the seats stopped, the seats already stopped, the sessions closed, the autostart outcome (`disabled`, `already disabled` or the error), and ends saying that `kankan start` resumes everything, with the commands it will run.

Running the command again is always safe: what is already stopped or disabled is reported as such, never as an error, and the stop record of the previous run is kept (the report still names `kankan start`).

## 3. `kankan start`

`kankan start` restores what the last `kankan stop` stopped: it runs `kankan up` with the recorded counts (the Q&A agent included) and, if autostart was enabled, `kankan autostart enable` with the same counts. It then deletes the stop record. `kankan up` starts only the missing agents, so agents started meanwhile are not duplicated.

Without a stop record (nothing was stopped, or `kankan start` already ran), it complains (`needs_action`) and prints the commands that start agents: `kankan up …` and `kankan autostart enable …`, with the counts of the seats registered in the project (or placeholders when none is), preceded by `kankan init <board-url>` when the clone is not bound to a board.

The counts come from the autostart unit when one was installed (they are what you asked for), otherwise from the seats that were running.

## 4. Immediate and graceful stop

| | `kankan stop` | `kankan stop --graceful` |
|---|---|---|
| Running task | interrupted; resumed at the next start | finished |
| Q&A answer being written | interrupted; completed at the next start | finished |
| Duration | seconds | until every current task ends |
| Card comments | one per held card | none |

## 5. Errors

| Situation | Result |
|---|---|
| No terminal and no `--yes` | `usage_error`, nothing changed |
| Confirmation declined | success, "nothing changed" |
| `systemctl` fails | reported; `needs_action` at the end |
| A seat keeps leftover processes | listed with `kankan seat recover <seat> --kill`; `needs_action` |
| A project session stays open | listed with `tmux kill-session -t <name>` or `kankan stop`; `needs_action` |
| Ctrl-C while waiting (`--graceful`) | stop requests kept; `needs_action` listing the running seats |
| `kankan start` without a stop record | `needs_action` with the commands to run |

## 6. Acceptance tests

1. Without seats running and without a unit, `kankan stop` succeeds without asking.
2. With two running seats and an autostart unit, `kankan stop --graceful --yes` disables autostart, waits until both seats exit after their task, reports them stopped and names `kankan start` with the unit's counts; a second run reports everything already stopped and still names `kankan start`.
3. `kankan stop --yes` stops a seat in the middle of a task within seconds; the card keeps its claim, gets a comment and is resumed at the next start.
4. A launcher restarted by its supervisor while `kankan stop --graceful` waits still stops.
5. Declining the confirmation changes nothing; without a terminal and `--yes` the command refuses.
6. A failing `systemctl` makes the result `needs_action` but the seats are still stopped.
7. `kankan start` after a stop starts the same agents and, if it was enabled, autostart; the record is then gone.
8. `kankan start` without a record prints the `up` and `autostart enable` commands, with `kankan init` first in an unbound clone.

## 7. Changes to the initial specification

- **§8.1 Process chain:** `kankan autostart disable` is also run by `kankan stop` without a seat; `kankan start` runs `kankan up` and `kankan autostart enable`.
- **§15.4 Administration commands:** `kankan stop` without a seat performs `seat recover --kill` on every running seat, with its own card comment.
- **§16.1 User commands:** `kankan stop <seat>` is joined by `kankan stop [--graceful] [--yes]` (all seats, autostart disabled) and `kankan start`.
- **§18.3 Acceptance tests:** add the tests of [§6](#6-acceptance-tests).
