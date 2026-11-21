# kankan alerts — technical specification

**Status:** specified, not implemented.
**Date:** 2026-11-22
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md), [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md) and [`cardinstructions_spec_technical.md`](cardinstructions_spec_technical.md); this feature changes them as listed in [§9](#9-changes-to-the-initial-technical-specification) and [§10](#10-changes-to-the-board-settings-and-card-instructions-technical-specifications).
**Companion:** [`alerts_spec_functional.md`](alerts_spec_functional.md) — what users and agents observe. "F§n" below refers to its sections.

## 1. Scope

A module `kankan/alerts.py` posts the alerts of [F§3](alerts_spec_functional.md#3-alerts) on the `Alerts` card; the launcher, the supervisor, the agent commands' code check and `kankan up` call it on their failure paths; `settings_setup` and `boardconfig` handle the card like the `Instructions` card.

## 2. Posting

`alerts.post(project, kind, text, *, seat=None, action)` never raises and returns whether the alert was posted. `kind` is one of `launcher_stopped` (A1), `launcher_failed` (A2), `seat_given_up` (A3), `registry_lost` (A4), `code_mismatch` (A5), `up_failed` (A6); the title, the action text and the comment layout are constants in `messages.py`.

1. **Key.** `key = sha1(kind \0 seat \0 text)[:16]`.
2. **Lock.** The alerts lock `<gcd>/kankan/alerts.lock`, taken with a timeout of 10 s (busy: log `alert_unposted` with `reason: busy` and return). It is a leaf lock: no other kankan lock is taken while it is held, and `post` is never called while the project lock is held.
3. **Repeats.** The record `<gcd>/kankan/alerts.json` (`{"card": <id or null>, "sent": {key: {"at", "kind", "seat"}}}`) is read; entries older than `alert_repeat_hours` (config, default 24, at least 1) are dropped; a key still present gives `alert_suppressed` in the audit log and no post.
4. **Card.** The cached `card` ID is checked with one C1 read (open, in `cfg.settings_list`); otherwise the `kankan` list is read (L6) and the open card named `Alerts` with the lowest `pos` is taken; none: one is created at `pos=top` with `messages.ALERTS_DESC` (C6), found again by name after a create that timed out, as `settings_setup` does for its cards. No `settings_list` configured, or the list archived or deleted: `alert_unposted` with `reason: no_list`.
5. **Comment.** The text of F§3 ("Comment") with `notify.mention(project, card_id)` (the card is kankan's, so the board's admins other than kankan's account), posted with `TrelloClient.comment_once` and the operation ID `alert-<key>-<epoch seconds>` (M1, M2 after a timeout).
6. **Record.** On success the key is written with the current time (atomic write) and `alert_posted` is logged with kind, seat and card.
7. **Pruning.** kankan's own alert comments beyond the latest 50 are deleted (M6), like `boardsettings._prune` keeps 10 on setting cards; a failure is logged and ignored.

Every `KankanError` or `OSError` after step 2 logs `alert_unposted` with the reason and the error; the record is unchanged, so the next occurrence tries again. The whole post uses the Trello client with a total budget of 15 s (`alerts.TIMEOUT`): the per-request timeout is reduced so that a stalled Trello delays the failure path by at most that.

**Text.** The problem text is kankan's own error message. It can contain local paths, the host name and versions, which every board member can read; this is accepted, since the board's members are the people the agents work for. It must never contain the credential: `alerts.post` removes the API key and the token from the text (a hostile or faulty server can echo them in an error body) before posting.

## 3. Posting points

| Kind | Where | Condition |
|---|---|---|
| `launcher_stopped` | `launcher.main`, in the `except KankanError` branch | status `config_error` or `usage_error` (exit `FATAL_EXIT`), including `QaLauncher`'s missing `Q&A` list |
| `launcher_failed` | `launcher.main`: the `except KankanError` branch for other statuses except `network_error`, and a new `except Exception` branch that posts and re-raises | any exit the supervisor restarts |
| `seat_given_up` | `supervisor.run`, before the endless wait | `MAX_RESTARTS` reached; the project is opened there (best effort: an error to open it is printed and the post skipped) |
| `registry_lost` | `Launcher.registry_lost`, after setting `stop_after_task` | once per launcher (the method returns early afterwards) |
| `code_mismatch` | `agentcmds._check_code`, before raising | the project is opened from the command's working directory; any error to open it skips the post |
| `up_failed` | `cli`, around the `up` command | a `KankanError` from `up` and `sys.stdin.isatty()` false |

The seat is passed for every kind but `up_failed`. The launcher and the supervisor print the same message in the pane as before, then post: the pane remains the first place an attached user looks.

## 4. The card on the board

**`settings_setup`.** `ensure_alerts_card(project, *, write)` runs in `settings_setup.ensure` before `ensure_instructions_card`:
- none: create at `pos=top` with `ALERTS_DESC` (C6);
- one: rewrite the description when its normalized form differs (C9, same normalization as the `Instructions` card), reporting `messages.ALERTS_CARD_UPDATED`;
- two or more: no write (the settings evaluation sets `Error`).

**Order.** After both cards are ensured, when the open cards of the list read are not ordered `Alerts`, `Instructions`, others (by `pos`), `Instructions` is moved to the top and then `Alerts` to the top (two C3 moves with `pos=top`, no numeric positions), reported by `messages.ALERTS_ORDER_RESTORED`. `ensure_instructions_card` no longer moves its card by itself. `doctor --no-fix` reports "Alerts card missing", "Alerts card outdated" and "kankan list order (Alerts, Instructions) not kept" and writes nothing. Launchers never reorder; `alerts.post` creates at the top only.

**`boardconfig`.** `ALERTS_TITLE = "Alerts"` gets a pseudo-key `alerts`, handled like `INSTRUCTIONS_KEY`: never an unknown setting; duplicates get `Error` through the same path as duplicate setting titles. The `Error` written there is owned by the board settings records like other setting-card errors.

**`destroy`.** No change: the card is one of the cards of kankan's lists that `destroy` deletes; `alerts.json` is local state removed with the rest of `<gcd>/kankan`, and `alerts.lock` is handled like kankan's other lock files.

## 5. Configuration

`alert_repeat_hours` (integer, default 24, minimum 1) in the project configuration, validated by `config` like `qa_poll_seconds`.

## 6. Concurrency

- The alerts lock serializes posts across the processes of one clone; two processes with the same key post once.
- The alerts lock is a leaf; `post` is called only on failure paths, outside the project lock, the conversation locks and the settings locks. `launcher.main` posts after the exception has left every `with` block of the launcher.
- A post racing `kankan doctor`'s reordering can leave the order wrong until the next `doctor`; this is accepted.

## 7. Security

- Alert texts come from kankan's messages and error texts, never from Trello text typed by users.
- The record and the lock live under `<gcd>/kankan` like the other state files, opened without following symbolic links (`config.open_nofollow`).

## 8. Trello usage

No new endpoint; new uses, recorded in [`kanban_requirements.md`](kanban_requirements.md) when implemented:
- C1 (cached card check), L6 (`kankan` list read), C6 (create `Alerts` at the top), C9 (`doctor` rewrites its description), C3 (`doctor` restores the order within the list), M1/M2 (the alert comment, found again after a timeout), M3 (mention of the admins), M6 (pruning to 50), B6 (the admins).

## 9. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md). That file is not edited.

**§8 Processes and launcher.** Add: "On the failure paths of [alerts T§3](alerts_spec_technical.md#3-posting-points), the launcher, the supervisor, the agent commands' code check and `kankan up` without a terminal call `alerts.post` after printing their message; the exit codes and the supervisor's restart policy are unchanged."

**§8.2 Supervisor.** Add: "When it gives up, it posts `seat_given_up` before waiting."

## 10. Changes to the board settings and card instructions technical specifications

**Board settings.** The reserved names of the `kankan` list are `Instructions` and `Alerts` ([§4](#4-the-card-on-the-board), `boardconfig`).

**Card instructions T§6.** "when another open card of the list read has a lower `pos`, it moves the card to the top" is replaced by the order rule of [§4](#4-the-card-on-the-board): `Instructions` is second, after `Alerts`, and the moves are done by the order step.

## 11. Tests

- `tests/test_alerts.py` with the fake Trello server: post with mention; repeat suppression and expiry (a fake clock); different texts; card cached, archived (recreated at the top), duplicated (upper one used); no `kankan` list; Trello failing (logged, no exception, budget respected with the fake server's `delay` fault); pruning to 50.
- Launcher and supervisor: a fatal start (archived `Q&A` list) posts `launcher_stopped` and still exits `FATAL_EXIT`; an injected exception posts `launcher_failed` and is re-raised; the supervisor's give-up posts once.
- Agent commands: the mismatch test of `tests/test_cli.py` also checks the alert and its suppression on a second command.
- `up`: without a terminal (stdin not a TTY) an `up` error posts `up_failed`; with a TTY it does not.
- `settings_setup`/`doctor`: creation, rewrite, order restoration, `--no-fix` reports; `boardconfig`: `Alerts` reserved, duplicates get `Error`.
