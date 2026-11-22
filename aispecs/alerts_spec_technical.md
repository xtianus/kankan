# kankan alerts — technical specification

**Status:** specified, not implemented.
**Date:** 2026-11-22
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md), [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md) and [`cardinstructions_spec_technical.md`](cardinstructions_spec_technical.md); this feature changes them as listed in [§9](#9-changes-to-the-initial-technical-specification) and [§10](#10-changes-to-the-board-settings-and-card-instructions-technical-specifications).
**Companion:** [`alerts_spec_functional.md`](alerts_spec_functional.md) — what users and agents observe. "F§n" below refers to its sections.

## 1. Scope

A module `kankan/alerts.py` posts the alerts of [F§3](alerts_spec_functional.md#3-alerts) on the `Alerts` card; the launcher, the supervisor, the agent commands' code check and `kankan up` call it on their failure paths; `settings_setup` and `boardconfig` handle the card like the `Instructions` card.

## 2. Posting

`alerts.post(project, kind, text, *, seat=None, action)` never raises and returns whether the alert was posted. `kind` is one of `launcher_stopped` (A1), `launcher_failed` (A2), `seat_given_up` (A3), `registry_lost` (A4), `code_mismatch` (A5), `up_failed` (A6); the title, the action text and the comment layout are constants in `messages.py`.

1. **Key.** `key = sha1(kind \0 seat \0 text)[:16]`, with `seat` empty for `up_failed`. `text` is the problem text only: the counter, the host name, the version line and the mention are outside it, so they never make two occurrences different.
2. **Lock.** The alerts lock `<gcd>/kankan/alerts.lock`, taken with a timeout of 5 s, part of the budget below (busy: log `alert_unposted` with `reason: busy` and return). It covers every step below, so a second process of the same clone sees the first one's attempt and comment. It is a leaf lock: no other kankan lock is taken while it is held, and `post` is never called while the project lock is held.
3. **Rate limit.** The record `<gcd>/kankan/alerts.json` is read: `{"card": <id or null>, "admins": {"at", "members": [[id, username], ...]} or null, "alerts": {key: {"at", "pending"}}}`. Entries of `alerts` whose `at` is set and older than `alerts.REPEAT_HOURS` (24) are dropped; an entry whose `at` was cleared by a reset stays until its next occurrence. When the entry of `key` has an `at` less than `alert_min_interval_minutes` ago, its `pending` is incremented, the record is written (atomic write), `alert_suppressed` is logged with kind, seat and `pending`, and `post` returns without any Trello request. Otherwise the entry gets `at` = now before any Trello request, so the limit applies to attempts: when Trello is unreachable, a repeat does not retry every few seconds.
4. **Card and comments.** The card is checked (open, in `cfg.settings_list`) and its comments are read. When the board client can read a card's comments together with the card (`TrelloClient.card_with_comments`: on Trello, C1 with `fields=closed,idList&actions=commentCard&actions_limit=1000&action_memberCreator_fields=username`, comments newest first), this is one request; otherwise it falls back to two, the card (C1) and then its comments (M2, `TrelloClient.comments`, every page). The fallback is also used when the combined read is not supported by the board (the request is refused, or the response carries no `actions` field): the client then remembers it for the rest of the process. With a cached card ID, the cached card is read this way. Otherwise the `kankan` list is read (L6) and the open card named `Alerts` with the lowest `pos` is taken and read the same way; none: one is created at `pos=top` with `messages.ALERTS_DESC` (C6), found again by name after a create that timed out, as `settings_setup` does for its cards, and has no comments. The ID found is cached in the record. No `settings_list` configured, or the list archived or deleted: `alert_unposted` with `reason: no_list`. The card holds about 30 of kankan's comments plus the user's, far below the 1000 of one combined read.
5. **Previous comment.** The alert comments are kankan's own (author `kankan_member_id`) whose text ends with a marker `kankan:alert-<16 hex>-<digits>-<digits>`; comments of other authors are ignored by this step and by step 9. The previous comment is the newest alert comment whose marker carries `key` and whose date is within 24 hours of the local clock (a skew of minutes does not matter at this scale); none: this occurrence is the first, and `pending` is discarded, since the comment it would add to is gone.
6. **Mention.** The board's admins other than kankan's account (`notify.kankan_member_id`, recorded by `init`): the card is always kankan's, so its creator is not read. They come from `admins` in the record, read again with B6 when this occurrence is the first, when the cached list is missing or older than 24 hours; the new list is cached. When B6 fails, the cached list is used if there is one, otherwise the comment has no mention, and `mention_failed` is logged, as `notify.mention` does.
7. **Comment.** The text of F§3 ("Comment") with the mention, posted with `TrelloClient.comment_once` and the operation ID `alert-<key>-<N>-<epoch seconds>` (M1, M2 after a timeout). `N` (0 for a first occurrence) is part of the ID so that a repeat within the same second as its previous comment never has the previous comment's marker: `comment_once` would otherwise return the previous comment instead of posting. With a previous comment, the first line ends with ` +N since <YYYY-MM-DD hh:mm> UTC`: the previous counter and time are read from the previous comment's first line (`\+(\d+) since (\d{4}-\d\d-\d\d \d\d:\d\d) UTC$`; without them the counter is 0 and the time is the previous comment's date in UTC), and `N` is the previous counter + 1 + `pending`. After the post, `pending` is set to 0 (atomic write).
8. **Replace.** After the new comment is posted, the previous one is deleted (M6, 404 is success); never when the comment step 7 returned is the previous one. A failure logs `alert_previous_kept` with the error and is otherwise ignored: both comments stay until step 9 removes the older.
9. **Pruning.** Of the alert comments read in step 4 plus the new one, all but the latest 30 (`alerts.KEEP`) are deleted (M6), like `boardsettings._prune` keeps 10 on setting cards; a failure is logged and ignored.

`alert_posted` is logged with kind, seat, card and `repeat` (`N`, 0 for a first occurrence). Every `KankanError` or `OSError` after step 3 and before the new comment is posted logs `alert_unposted` with the reason and the error, and increments `pending`: the occurrence was not shown, and the next posted comment counts it. Nothing is deleted then, so the previous comment stays. The whole post, lock wait included, has a budget of 15 s (`alerts.TIMEOUT`): each Trello request gets the time left as its timeout, so a busy lock or a stalled Trello delays the failure path by at most that.

A repeat within the limit makes no request; a repeat after it makes three (the card with its comments, the post, the delete), four with the fallback, plus B6 at most once a day; a first occurrence makes the card read (one or two requests), B6 and the post.

**Reset.** `alerts.reset(project)`, called by `kankan up`, `kankan start` and `kankan agent` before they start any agent, sets `at` of every entry to null and clears the cached admins, under the alerts lock; `pending` is kept, so the counter stays true. An error writing the record is logged and ignored.

**Text.** The problem text is kankan's own error message. It can contain local paths, the host name and versions, which every board member can read; this is accepted, since the board's members are the people the agents work for. It must never contain the credential: `alerts.post` removes the API key and the token from the text (a hostile or faulty server can echo them in an error body) before computing the key and posting. It must be the same at every occurrence of the same problem: no process IDs, temporary paths, times or counters, which would make every occurrence a different alert and defeat both the repeats and the rate limit; the message constants of the posting points are written so.

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

**`settings_setup`.** `ensure_alerts_card(project, *, write)` runs in `settings_setup.ensure` after `ensure_instructions_card`, so that on a new board the `Alerts` card, created last at the top, is above the `Instructions` card:
- none: create at `pos=top` with `ALERTS_DESC` (C6);
- one: rewrite the description when its normalized form differs (C9, same normalization as the `Instructions` card), reporting `messages.ALERTS_CARD_UPDATED`;
- two or more: no write (the settings evaluation sets `Error`).

**No reordering.** `ensure_instructions_card` still creates a missing card at `pos=top`, so a card recreated while the `Alerts` card exists lands above it. Neither function moves an existing card: `ensure_instructions_card` no longer moves its card to the top, and `messages.INSTRUCTIONS_CARD_MOVED` is removed. `doctor --no-fix` reports "Alerts card missing", "Alerts card outdated", "Instructions card missing" and "Instructions card outdated" and writes nothing. Launchers never write either card except `alerts.post` creating a missing `Alerts` card at the top.

**`boardconfig`.** `ALERTS_TITLE = "Alerts"` gets a pseudo-key `alerts`, handled like `INSTRUCTIONS_KEY`: never an unknown setting; duplicates get `Error` through the same path as duplicate setting titles. The `Error` written there is owned by the board settings records like other setting-card errors.

**`destroy`.** No change: the card is one of the cards of kankan's lists that `destroy` deletes; `alerts.json` is local state removed with the rest of `<gcd>/kankan`, and `alerts.lock` is handled like kankan's other lock files.

## 5. Configuration

`alert_min_interval_minutes` (integer, default 5, minimum 0; 0 removes the rate limit) in the project configuration, validated by `config` like `qa_poll_seconds`. The repeat window (`alerts.REPEAT_HOURS`, 24, also the age of the cached admins), the number of comments kept (`alerts.KEEP`, 30) and the budget (`alerts.TIMEOUT`, 15 s) are constants.

## 6. Concurrency

- The alerts lock serializes posts across the processes of one clone; the second of two processes with the same key is suppressed by the rate limit, or, with the limit at 0, finds the first one's comment and replaces it.
- Each clone keeps its own rate limit: clones on different machines can each post the same alert once per interval.
- Clones on different machines share no lock: two of them posting the same alert at once can both find the same previous comment, both post and both delete it (one gets 404). Two comments with the same counter then stay; the next repeat replaces only the newer, and pruning removes the other. This is accepted.
- The alerts lock is a leaf; `post` is called only on failure paths, outside the project lock, the conversation locks and the settings locks. `launcher.main` posts after the exception has left every `with` block of the launcher.

## 7. Security

- Alert texts come from kankan's messages and error texts, never from Trello text typed by users. The counter and the time read back from the previous comment are taken only from kankan's own comments and only through the pattern of [§2](#2-posting) step 5; anything else gives `N` 1.
- The record holds the admins' Trello IDs and usernames, which every board member can see anyway. The record and the lock live under `<gcd>/kankan` like the other state files, opened without following symbolic links (`config.open_nofollow`).

## 8. Trello usage

No new endpoint and no new capability: existing capabilities get new uses, added to their usage notes in [`kanban_requirements.md`](kanban_requirements.md) when implemented:
- C1 with the card's comments nested, when the board supports it (one read checks the card and gives the previous comments; the same comment data as M2); otherwise C1 then M2. To be recorded as an optional form of C1 with that fallback as its workaround. L6 (`kankan` list read), C6 (create `Alerts` at the top), C9 (`doctor` rewrites its description), M1/M2 (the alert comment, found again after a timeout), M3 (mention of the admins), M6 (deleting the replaced comment, pruning to 30), B6 (the admins, cached for up to 24 hours).
- The nested parameters of C1 (`actions=commentCard`, `actions_limit`, `action_memberCreator_fields`) are to be checked on Trello when implemented, and recorded in [`aiknowledge/trello.md`](../aiknowledge/trello.md); if Trello does not return the comments that way, the client uses the fallback only and `card_with_comments` is dropped.
- C3 is no longer used within the `kankan` list: `doctor` does not move the `Instructions` card.
- The only new requirement is a board behaviour outside the API: deleting a comment removes the notification its mention created (M3), checked by hand on Trello on 2026-11-22. Without it, each repeat would leave one more notification.

## 9. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md). That file is not edited.

**§8 Processes and launcher.** Add: "On the failure paths of [alerts T§3](alerts_spec_technical.md#3-posting-points), the launcher, the supervisor, the agent commands' code check and `kankan up` without a terminal call `alerts.post` after printing their message; the exit codes and the supervisor's restart policy are unchanged."

**§8.2 Supervisor.** Add: "When it gives up, it posts `seat_given_up` before waiting."

## 10. Changes to the board settings and card instructions technical specifications

**Board settings.** The reserved names of the `kankan` list are `Instructions` and `Alerts` ([§4](#4-the-card-on-the-board), `boardconfig`).

**Card instructions T§6.** `ensure_instructions_card` runs before `ensure_alerts_card` ([alerts T§4](alerts_spec_technical.md#4-the-card-on-the-board)); it still creates its card at `pos=top`. The clause "when another open card of the list read has a lower `pos`, it moves the card to the top (C3, `pos=top` within the same list) and reports `messages.INSTRUCTIONS_CARD_MOVED`; both reports are joined with "; " when both apply" is removed. In the `doctor --no-fix` reports, "Instructions card not at the top of the kankan list" is removed.

## 11. Tests

- `tests/test_alerts.py` with the fake Trello server: post with mention of the admins, without reading the card's creator; admins cached, read again at a first occurrence, after 24 hours and after a reset, stale list used when B6 fails; rate limit: a repeat within `alert_min_interval_minutes` makes no request and counts in `pending`, the next post's counter includes it, 0 removes the limit, a failed post counts as an attempt, `reset` lets the next occurrence post and keeps `pending`; a repeat after the limit makes three requests, four with the fallback (a fake server refusing the nested parameters or ignoring them, remembered for the rest of the process); with the limit at 0: first occurrence without counter; second with `+1 since` the first comment's time and the first deleted; third with `+2` and the same time; a repeat within the same second as the previous comment (fake clock not advanced) still posts and deletes only the previous one; repeat after 24 hours (a fake clock) and after the user deleted the comment start again without counter; different texts kept apart; the user's comments never matched nor deleted; delete of the previous comment failing (both stay, `alert_previous_kept`); post failing (previous comment kept); card cached, archived (recreated at the top), duplicated (upper one used); no `kankan` list; Trello failing (logged, no exception, budget respected with the fake server's `delay` fault); pruning to 30 alert comments.
- Launcher and supervisor: a fatal start (archived `Q&A` list) posts `launcher_stopped` and still exits `FATAL_EXIT`; an injected exception posts `launcher_failed` and is re-raised; the supervisor's give-up posts once.
- Agent commands: the mismatch test of `tests/test_cli.py` also checks the alert, a second command suppressed by the rate limit, and the replacement with `+2` after a reset.
- `up`: without a terminal (stdin not a TTY) an `up` error posts `up_failed`; with a TTY it does not.
- `up`, `start` and `agent` call `alerts.reset` before starting agents.
- `config`: `alert_min_interval_minutes` default 5, 0 accepted, negative refused.
- `settings_setup`/`doctor`: on a new board `Alerts` first and `Instructions` second; a missing `Instructions` recreated above an existing `Alerts`; rewrite; reordered cards left in place; `--no-fix` reports; `boardconfig`: `Alerts` reserved, duplicates get `Error`.
