# kankan board reader — technical specification

**Status:** specified, not implemented.
**Date:** 2026-11-25
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3, [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md) and [`questions_spec_technical.md`](questions_spec_technical.md); this feature changes them as listed in [§12](#12-changes-to-the-initial-technical-specification) and [§13](#13-changes-to-other-technical-specifications).
**Companion:** [`boardreader_spec_functional.md`](boardreader_spec_functional.md) — the behaviour this file implements; read it first.

**Split rule.** This file describes how kankan implements the feature; the functional file describes what users and agents observe. Section numbers are this feature's own.

## 1. Scope

A new module `kankan/boardreader.py` becomes the only caller of `TrelloClient`'s read methods. Every kankan process reaches Trello for reads through a `BoardReader` (`project.board`, built by `Project.open` around the project's `TrelloClient`), which keeps a cache shared by the processes of the project in `<gcd>/kankan/board-cache/`. Writes stay on `TrelloClient`; each write reports the data it changed to the reader, which marks it stale ([§4](#4-freshness-and-invalidation)). There is no reader process: every process runs the module, and the cache, its locks and its timestamps make one process's reads usable by the others.

## 2. Interface

Every read method takes a freshness argument, `fresh`:
- `LIVE`: the data must come from a Trello request sent after the method was called;
- `max_age(seconds)`: the data may come from a request sent at most that many seconds before the call (`max_age(0)` behaves as `LIVE`).

Internally both become a **minimum send time** `not_before` (the call time for `LIVE`, the call time minus the age otherwise), compared with the send time recorded with each cached unit ([§3](#3-units-and-timestamps)).

| Method | Unit | Replaces |
|---|---|---|
| `snapshot(fresh)` | board, merged with fresher card units | `TrelloClient.snapshot`, `board_payload` |
| `card(card_id, fresh)` | card (`None` when deleted, a closed card when archived) | `card`, `card_place`, `attachments` |
| `list_info(list_id, fresh)` | board (lists with `closed`; a list missing from the read is deleted) | `list_info` |
| `settings_cards(list_id, fresh)` | board (cards of that list and their checklists) | `settings_cards` |
| `thread(card_id, fresh)` | thread (every comment of the card) | `comments(card_id)` without `since`, `find_comment` |
| `new_comments(card_id, after, fresh)` | comment log, or the card's comments when the log does not reach back to `after` | `comments(card_id, since=…)` |
| `members(fresh)`, `admins(fresh)` | members | `board_members`, `board_admins` |
| `me()`, `card_creator(card_id)` | immutable (read once; the member is kept per credentials, keyed by a hash of the token, and a creator per card) | `me`, `card_creator` |
| `closed_cards(fresh)`, `board_info(short)`, `member(username)` | none: always live, never cached (used once by `destroy`, `init` and the live tests) | `closed_cards`, `board`, `member` |

`download` is not a board read and stays with the `attachments` module, which already keeps the files ([initial §13.3](initial_spec_functional.md#133-card-attachments)). A test fails if any module other than `boardreader` calls a read method of `TrelloClient`, as for `refstore` and `refs/agent-*`. The reads `trello` makes inside its own writes (the read-back after a timeout and the marker search of `comment_once`, [initial §15.1](initial_spec_technical.md#151-principles)) stay inside the client: they are live by nature and never cached.

## 3. Units and timestamps

| Unit | Content | Refreshed by |
|---|---|---|
| board | description, every list (`closed`, name, position), every open card (as in B3, with the full attachment fields of [§9](#9-trello-usage)), every label, the checklists | a board read (B3) |
| card | one card as in C1, also archived ones | a single-card read (C1), and every board read for the open cards it returns |
| thread | every comment of one card | a per-card comment read (M2, all pages) |
| comment log | the new comments of the whole board after a cursor | a board-wide comment read ([§7](#7-comment-log)) |
| members | board memberships with roles | a membership read (B6) |
| immutable | the token's member; each card's creator | their first read |

Each stored unit records the **send time** of the request that produced it: the local wall-clock time (`time.time()`) taken just before the request was sent, so the data is at least that fresh whatever the response time. Every process runs on the same machine ([initial §2](initial_spec_functional.md#2-scope-and-assumptions)) and compares the same clock. Ordering between comments never uses the local clock: cursors are Trello action IDs, which are time-ordered.

A unit satisfies a read when its send time is at or after `not_before` and after its **write mark** ([§4](#4-freshness-and-invalidation)).

**The board view.** `snapshot` returns the board unit with each card replaced by its card unit when that one is fresher. The view satisfies `not_before` when the board unit's send time does (it decides which cards and lists exist) and every card in it, by its own or the board's time, does too. When only a few cards fall short (at most `BOARD_PATCH_MAX`, 3), they are read one by one (C1); otherwise the board is read again. A card missing from a board read is not open (archived, deleted or moved to another board); `card(card_id)` tells which.

## 4. Freshness and invalidation

**Write marks.** Every write method of `TrelloClient` reports what it changed through a hook that `Project.open` installs, called after the write returned, and also after a write whose outcome is uncertain (a timeout): moving, labelling, renaming or changing the description of a card marks that card; commenting on a card or deleting a comment marks its thread; creating, deleting, archiving or restoring a card, creating, archiving or restoring a list, changing the board description or creating or deleting a label marks the board unit itself; checklist writes mark their card and the board unit. A mark is the local time the write returned; a unit or card whose send time is not after its mark is stale for every reader, so no reader is given the state before kankan's own change as fresh ([functional §2](boardreader_spec_functional.md#2-freshness)).

**Newest wins.** A result is stored only when its send time is later than the stored one's, so a slow fetch that started earlier never replaces a newer result.

**Errors.** A failed fetch raises the client's `KankanError` as today and stores nothing; the method never returns a unit that does not satisfy `not_before`.

## 5. Locks and fetching

- **Polling reads share one fetch.** A read with `max_age` that finds the unit stale takes the unit kind's **fetch lock** (`<gcd>/kankan/board-cache/<kind>.fetch.lock`: `board`, `cards`, `threads`, `log`, `members`), checks the cache again (another process may have fetched meanwhile), fetches only if still needed, stores, and releases. A process waiting on a fetch lock waits at most the client's time budget (15 seconds, retries included) plus 2 seconds, then fetches without the lock.
- **Live reads never wait.** A `LIVE` read fetches at once without the fetch lock: a decision under the project lock never waits for another seat's poll ([questions §5](questions_spec_functional.md#5-interaction-with-other-work)).
- **Storing.** Every write to the cache happens under the **store lock** (`<gcd>/kankan/board-cache/store.lock`), held only to compare send times and rename one file.
- **Lock order.** The fetch locks and the store lock come after every other kankan lock: project lock, seat-state lock, attachment lock, Q&A lifecycle and conversation locks, seat-removal lock → fetch lock → store lock. No other lock is taken while holding them. Lock files are never deleted ([initial §5.2](initial_spec_technical.md#52-local-state)).

## 6. Storage

`<gcd>/kankan/board-cache/` (mode 0700; files 0600), JSON files written to a temporary file and renamed: `board.json`, `cards/<card-id>.json`, `threads/<card-id>.json`, `log.json`, `members.json`, `immutable.json` and `marks.json` (the write marks). Each file carries `format` (1), the `board_id` and the send time; a file with another format or board, or one that cannot be parsed, is ignored as missing (the next read fetches and replaces it). The raw Trello payloads are stored, decoded by the same functions as today, so cached and live data are identical in form. A board read deletes the card and thread files of cards it does not return that are older than one day; the comment log keeps its entries for 24 hours ([§7](#7-comment-log)).

## 7. Comment log

The log holds the new comments of the whole board: for each, its action ID, card ID, author, date and text, as M2 returns them.
- **Start.** The first board-wide read with no log asks for the newest comment action (`limit=1`); its ID becomes both the log's cursor and its **horizon**: the log is complete for every comment after the horizon. With no comment on the board, the horizon is the empty cursor and the log is complete from the start.
- **Refresh.** `GET /1/boards/{id}/actions?filter=commentCard&since=<cursor>&limit=1000` (newest first; `since` excludes the cursor itself), followed by pages with `before=<oldest ID of the previous page>` while a page is full. The newest ID becomes the cursor.
- **Serving.** `new_comments(card_id, after, …)` returns the logged comments of that card with an ID greater than `after`, and the log's cursor, up to which the answer is complete; when `after` is older than the horizon, the log cannot answer and the card's comments are read instead (M2 `since=after`, as today), returning the newest ID read as the point of completeness. There is no board-wide variant: a caller watching several cards asks for each, served by the same log refresh.
- **Pruning.** Entries older than 24 hours are dropped and the horizon moves to the newest dropped ID.
- **Edits and deletions.** Trello reports neither in this read: an edit changes the text of the existing action and a deletion removes it, with no `updateComment` or `deleteComment` action (observed 2026-11-25 on the test board). The log therefore never changes a comment already logged; the current text of a thread comes only from a thread read, and every agent command reading a thread reads it live ([functional §5](boardreader_spec_functional.md#5-comments)).

The clients keep their own cursors as today (the seat state's `comment_cursor`, the Q&A launcher's cursor): the reader keeps no per-client state.

## 8. Callers and their freshness

| Caller | Reads | Freshness |
|---|---|---|
| `workflow.tick` before the project lock; `launcher.periodic_board_check`, `check_user_move` | `snapshot` | `max_age(poll_seconds)` |
| Every read under the project lock: the claim recheck in `workflow`, `cards.converge`, `integrate`, the Q&A claim and handoff, `seatremove`, `labels` | `snapshot`, `card`, `list_info` | `LIVE` |
| `launcher.start`, `failed_start`, `seats` (seat creation, list checks, `acquire_qa`) | `snapshot`, `card`, `list_info` | `LIVE` |
| `QaLauncher` idle claim scan, `check_list_while_waiting`, `poll_active` | `snapshot`, `list_info`, `card`, `new_comments` | `max_age(qa_poll_seconds)` |
| `QaLauncher.open_conversation` (thread at the start of a conversation) | `thread` | `LIVE` |
| `forward.poll` (`waiting_human`) | `new_comments` (its card) | `max_age(poll_seconds)` |
| `boardsettings` periodic read | `settings_cards` | `max_age(settings_poll_seconds)` |
| `boardsettings` and `cardsections_feedback` marker checks before a comment | `thread` | `LIVE` |
| `notify` from a launcher; from a command | `members`, `admins` | the caller's poll freshness; `LIVE` |
| `notify`, `doctor` | `me`, `card_creator` | immutable |
| Agent commands (`task`, `answer`, `handoff`, `done`, …), `attachments.for_task` | `card`, `thread`, `snapshot` | `LIVE` |
| CLI commands (`status`, `reconcile`, `doctor`, `init`, `destroy`, `merge-request`, `release`, `seat …`, `up`, `agent`) | all | `LIVE` |

The launchers keep their wake-up intervals; a poll interval of 0 makes every poll a live read, which the existing tests rely on ([§10](#10-tests)).

## 9. Trello usage

Two revisions of [`kanban_requirements.md`](kanban_requirements.md), made in the implementation commit:
- **B3:** the board read asks for the full attachment fields of F2 (`card_attachment_fields=id,name,fileName,url,bytes,mimeType,idMember,isUpload,date`), so a board read refreshes the card units completely (checked on the test board on 2026-11-25).
- **New M7 (SHOULD):** read the new comments of a whole board after a point in one request, newest first, with pagination: `GET /1/boards/{id}/actions?filter=commentCard&since=<action id>&before=<action id>&limit=1000` (at most 1000 per page; `limit` above 1000 is a 400). Edits and deletions of comments are not reported by this read; kankan does not rely on them. Without M7 kankan reads each watched card's comments (M2), as before the feature.

The requests for data the agents share no longer grow with the number of agents ([functional §4](boardreader_spec_functional.md#4-trello-requests)); G6's rate-limit estimate is unchanged and now has more margin.

## 10. Tests

- **Unit:** the `not_before` rule for `LIVE` and `max_age`; write marks (a fetch sent before a write is stale after it); newest wins; the board view merging card units and patching at most `BOARD_PATCH_MAX` cards; the comment log (start, `since` exclusive, pages, horizon, pruning, the thread fallback); format and board mismatch; a corrupt file ignored.
- **Fake Trello:** the board-wide comment actions read (`filter`, `since`, `before`, `limit` up to 1000), the full attachment fields in the board read, edits that change an action's text and deletions that remove it.
- **Processes:** two processes polling with `max_age` while the fake board is slow make one board request; a `LIVE` read under the project lock does not wait for a slow poll of another process (fake Trello `delay` fault).
- **Existing tests:** they mostly set poll intervals to 0, so every poll stays a live read and the helpers that wait for polls by counting fake-Trello requests (`wait_board_polls`, `wait_card_polls`) keep working. A test with a non-zero interval that waits for polls counts the reader's fetches instead, which the reader records in the audit log only under the test hook `KANKAN_TEST_READS=1`.
- **Static:** only `boardreader` calls `TrelloClient` read methods.
- **Live Trello:** the board-wide comment read (pages, `since` exclusive, edits and deletions not reported) and the attachment fields of the board read.

## 11. Accepted limitations

- A comment edited or deleted is seen only through a thread read ([§7](#7-comment-log)).
- A backwards jump of the wall clock can let a cached unit count as fresher than it is, for the length of the jump; the units are re-read at the following polls.
- A waiter that gives up on a fetch lock after the time budget makes one more request than needed.

## 12. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md), by its section numbers. That file is not edited.

**§3 Principles.** Add:

11. **Reads state their freshness.** Every Trello read goes through the board reader and says how old its data may be; every decision under the project lock reads live.

**§5.2 Local state.** Add:

| Board cache | `<gcd>/kankan/board-cache/` ([§6](#6-storage)) |

**§5.3 Components.** Add:

| `boardreader` | The only reader of Trello: freshness, cache, write marks, fetch sharing, comment log. |

**§5.3 Components.** Replace the text starting “| `trello` | REST client: retries, read-after-write, error …” with:

| `trello` | REST client: retries, read-after-write, error classification, rate limits. Isolates Trello from the rest (see [`kanban_requirements.md`](kanban_requirements.md)); its reads are called only by `boardreader`, its writes report their effects to it. |

**§8.5 Cycle and signals.** Add:

> The cycle's first board read accepts data as old as `poll_seconds`; convergence and every check under the project lock read live ([§8](#8-callers-and-their-freshness)).

**§10.4 Convergence.** Add:

> Convergence reads the board and the cards live.

**§15.1 Principles.** Add:

- **Shared reads:** a read with a maximum age may be answered by another process's request; a live read never waits for another process ([§5](#5-locks-and-fetching)).

**§18.1 Strategy.** Add the tests of [§10](#10-tests).

## 13. Changes to other technical specifications

**Questions §4 Launcher, “Polling the active card”.** The card read (C1) and the comment read become `card` and `new_comments` with `max_age(qa_poll_seconds)`, and the `Q&A` list read becomes `list_info` from the board unit; the request counts of questions §8 become those of [§9](#9-trello-usage).

**Board settings §2 Reading.** The settings read becomes `settings_cards` with `max_age(settings_poll_seconds)`, answered from the board unit.
