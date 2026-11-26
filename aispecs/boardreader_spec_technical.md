# kankan board reader — technical specification

**Status:** implemented locally (2026-11-26); the live Trello checks pass; the live agents run is outstanding.
**Date:** 2026-11-25
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3, [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md), [`questions_spec_technical.md`](questions_spec_technical.md) and [`destroy_spec_technical.md`](destroy_spec_technical.md); this feature changes them as listed in [§12](#12-changes-to-the-initial-technical-specification) and [§13](#13-changes-to-other-technical-specifications).
**Companion:** [`boardreader_spec_functional.md`](boardreader_spec_functional.md) — the behaviour this file implements; read it first.

**Split rule.** This file describes how kankan implements the feature; the functional file describes what users and agents observe. Section numbers are this feature's own.

## 1. Scope

A new module `kankan/boardreader.py` becomes the only caller of `TrelloClient`'s read methods. Every kankan process reaches Trello for reads through a `BoardReader` (`project.board`, built by `Project.open` around the project's `TrelloClient`), which keeps a cache shared by the processes of the project in `<gcd>/kankan/board-cache/`, with its locks and write marks beside it ([§6](#6-storage)). Writes stay on `TrelloClient`; each write reports the data it changed to the reader, which marks it stale ([§4](#4-freshness-and-invalidation)). There is no reader process: every process runs the module, and the cache, its locks and its timestamps make one process's reads usable by the others.

Before a project exists (`kankan init`, which checks the credentials and finds the board before it saves the binding), `BoardReader.unbound(client)` gives a reader without a cache: every read is live and nothing is stored.

## 2. Interface

Every cacheable read method takes a freshness argument, `fresh` (the reads marked “always live” and the immutable ones below take none):
- `LIVE`: the data must come from a Trello request sent after the method was called;
- `max_age(seconds)`: the data may come from a request sent at most that many seconds before the call (`max_age(0)` behaves as `LIVE`).

Internally both become a **minimum send time** `not_before` (the call time for `LIVE`, the call time minus the age otherwise), compared with the send time recorded with each cached unit ([§3](#3-units-and-timestamps)).

| Method | Unit | Replaces |
|---|---|---|
| `snapshot(fresh)` | board, merged with fresher card units; each card carries its attachments as items (`id`, `name`, `idMember`, `date`, `pos`, …, the fields of [§9](#9-trello-usage)), not only the URLs used for dependencies | `TrelloClient.snapshot`, `board_payload` |
| `card(card_id, fresh)` | card (`None` when deleted, a closed card when archived; attachments in the card's order, by `pos`) | `card`, `attachments` |
| `list_info(list_id, fresh)` | board (lists with `closed`; a list missing from the read is deleted) | `list_info` |
| `settings_cards(list_id, fresh)` | board (cards of that list and their checklists) | `settings_cards` |
| `thread(card_id, fresh)` | thread (every comment of the card) | `comments(card_id)` without `since`, `find_comment` |
| `new_comments(card_id, after, fresh)` | comment log, or the card's comments when the log does not reach back to `after`; returns `(comments, point)`: the card's comments dated after `after`, oldest first, and the completeness point ([§7](#7-comment-log)) | `comments(card_id, since=…)` |
| `members(fresh)`, `admins(fresh)` | members | `board_members`, `board_admins` |
| `me(fresh)`, `card_creator(card_id)` | immutable (the member is kept per credentials, keyed by a hash of the token, and a creator per card); `me(LIVE)` always asks Trello and replaces the kept member | `me`, `card_creator` |
| `closed_cards(fresh)`, `board_info(short)`, `member(username)` | none: always live, never cached (used once by `destroy`, `init` and the live tests) | `closed_cards`, `board`, `member` |
| `board_payload()`, `card_place(card_id)` | none: always live, never cached, returned raw and validated by the caller as today (`destroy`'s strict inventory and its placement check right before each deletion, which treat only 404 as gone) | `board_payload`, `card_place` |

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

Each stored unit records the **send time** of the request that produced it: the local wall-clock time (`time.time()`) taken just before the request was sent, so the data is at least that fresh whatever the response time. It also keeps Trello's **response date** (the HTTP `Date` header, as `Snapshot.response_date` today), which the board settings compare with the cards' activity dates ([§13](#13-changes-to-other-technical-specifications)). Every process runs on the same machine ([initial §2](initial_spec_functional.md#2-scope-and-assumptions)) and compares the same clock. Ordering between comments never uses the local clock nor the action IDs: Trello's object IDs created within one second are not ordered by creation (observed 2026-11-26), so comments are ordered by their action **date**, which Trello gives in milliseconds and by which it orders and filters its reads. A **comment point** (a cursor, a horizon, a completeness point) is such a date, an ISO 8601 string compared as text; the reader sends it as `since=<date>`, which Trello accepts and treats as exclusive, also when the comment it came from was deleted.

A unit satisfies a read when its send time is at or after `not_before` and after its **write mark** ([§4](#4-freshness-and-invalidation)).

**The board view.** `snapshot` returns the board unit with each card replaced by its card unit when that one is fresher. The view satisfies `not_before` when the board unit's send time does (it decides which cards and lists exist) and every card in it, by its own or the board's time, does too. When only a few cards fall short (at most `BOARD_PATCH_MAX`, 3), they are read one by one (C1); otherwise the board is read again. A card missing from a board read is not open (archived, deleted or moved to another board); `card(card_id)` tells which. A card unit fresher than the board unit that shows the card archived, deleted (`None`) or on another board removes the card from the view; a card the board unit does not list is never added by a card unit (it was created or restored after the board read, which the board unit's mark of [§4](#4-freshness-and-invalidation) covers for kankan's own writes). The view's response date is the oldest response date of its parts, so a view patched with newer cards is never dated later than the board read that decides which cards exist. `list_info` and `settings_cards` are answered from the board view, so the card marks of [§4](#4-freshness-and-invalidation) apply to them too.

## 4. Freshness and invalidation

**Write marks.** Every write method of `TrelloClient` reports what it changed through a hook that `Project.open` installs, called after the write returned, and also after a write whose outcome is uncertain (a timeout). Each write marks every unit through which a reader could see the changed data:

| Write | Marks |
|---|---|
| move, label, rename, change the description of a card; add or delete an attachment; checklist writes | that card |
| post a comment | the card's thread and the comment log |
| delete or edit a comment | the card's thread |
| create, delete, archive or restore a card | the board unit and that card |
| create, archive or restore a list; change the board description; create or delete a label | the board unit |

The board view checks each of its cards against the card's mark, so a card mark is enough for `snapshot`, `list_info` and `settings_cards`. A mark is the local time the write returned; a unit or card whose send time is not after its mark is stale for every reader, so no reader is given the state before kankan's own change as fresh ([functional §2](boardreader_spec_functional.md#2-freshness)). A process that crashes after Trello accepted a write and before the hook stored its mark leaves no mark: a `max_age` reader may then be given the former state until the unit is next refreshed (accepted, [§11](#11-accepted-limitations)).

**In-flight fetches.** Before a read returns what it fetched, it checks the marks again: when a mark of the unit was set after the fetch's send time and after the read was called, the result is not returned (nor stored) and the unit is fetched again. A result that predates a write is returned only to a read called before that write.

**Newest wins.** A result is stored only when its send time is later than the stored one's, so a slow fetch that started earlier never replaces a newer result.

**Errors.** A failed fetch raises the client's `KankanError` as today and stores nothing; the method never returns a unit that does not satisfy `not_before`.

## 5. Locks and fetching

- **Polling reads share one fetch.** A read with `max_age` that finds the unit stale takes the unit kind's **fetch lock** (`<gcd>/kankan/board-<kind>.fetch.lock`: `board`, `cards`, `threads`, `log`, `members`), checks the cache again (another process may have fetched meanwhile), fetches only if still needed, stores, and releases. A process waiting on a fetch lock waits at most the client's time budget (15 seconds, retries included) plus 2 seconds, then fetches without the lock.
- **Live reads never wait.** A `LIVE` read fetches at once without the fetch lock: a decision under the project lock never waits for another seat's poll ([questions §5](questions_spec_functional.md#5-interaction-with-other-work)).
- **Storing.** Every write to the cache and to the write marks happens under the **store lock** (`<gcd>/kankan/board-store.lock`), held only to compare send times and rename one file.
- **Lock order.** The fetch locks and the store lock come after every other kankan lock: project lock, seat-state lock, attachment lock, Q&A lifecycle and conversation locks, seat-removal lock → fetch lock → store lock. No other lock is taken while holding them. Lock files are never deleted ([initial §5.2](initial_spec_technical.md#52-local-state)); they live outside `board-cache/` so that deleting the cache never splits a lock between processes.

## 6. Storage

`<gcd>/kankan/board-cache/` (mode 0700; files 0600), JSON files written to a temporary file and renamed: `board.json`, `cards/<card-id>.json`, `threads/<card-id>.json`, `log.json`, `members.json` and `immutable.json`. The write marks are kept outside the cache, in `<gcd>/kankan/board-marks.json`, with the locks of [§5](#5-locks-and-fetching): deleting `board-cache/` while processes run only loses cached data, never a mark or a lock. Each file carries `format` (1), the `board_id`, the send time and Trello's response date; a file with another format or board, or one that cannot be parsed, is ignored as missing (the next read fetches and replaces it). The raw Trello payloads are stored, decoded by the same functions as today, so cached and live data are identical in form. A board read deletes the card and thread files of cards it does not return that are older than one day; the comment log keeps its entries for 24 hours ([§7](#7-comment-log)).

## 7. Comment log

The log holds the new comments of the whole board: for each, its action ID, card ID, author, date and text, as M2 returns them. Its cursor, horizon and every point it returns are comment dates ([§3](#3-units-and-timestamps)).
- **Start.** The first board-wide read with no log asks for the newest comment action (`limit=1`); its date becomes both the log's cursor and its **horizon**: the log is complete for every comment after the horizon. With no comment on the board, the cursor is empty and the horizon is `origin`, a value older than every cursor, the empty one included: the log is complete from the board's beginning, and an empty `after` is served by the log.
- **Refresh.** `GET /1/boards/{id}/actions?filter=commentCard&since=<cursor date>&limit=1000` (newest first by date; `since` excludes the cursor), followed by pages with `before=<oldest date of the previous page>` while a page is full; entries not dated after the cursor are dropped, should Trello return any. While the cursor is empty the read is sent without `since`. The newest date becomes the cursor; with no new comment the cursor stays and the log's send time still advances, so the log is known complete up to that send time.
- **Serving.** `new_comments(card_id, after, …)` returns the logged comments of that card dated after `after`, and the **completeness point** up to which the answer is complete: the later of the log's cursor and `after` (a caller's `after` can be ahead of the log when its previous call used the card read below; the point never moves a caller's cursor backwards). When `after` is empty or older than the horizon (and the horizon is not `origin`), the log cannot answer: the log is first brought to the required freshness, its cursor is taken, and then the card's comments are read (M2 `since=<after>`, or all of them for an empty `after`); the completeness point is the latest of that cursor, `after` and the newest date read (a comment of the card arriving between the two reads is returned and covered). Since the card read was sent after the log's, it covers every comment of the card up to the cursor, so a card with no new comment still gets a completeness point the log can serve next time. There is no board-wide variant: a caller watching several cards asks for each, served by the same log refresh.
- **Pruning.** Entries older than 24 hours are dropped and the horizon moves to the newest dropped date. A caller that polls successfully at least once in 24 hours never falls behind the horizon, since each call moves its cursor to the log's cursor, which is never older than the newest dropped date; a card not polled for 24 hours costs one card read at its next poll.
- **Edits and deletions.** Trello reports neither in this read: an edit changes the text of the existing action and a deletion removes it, with no `updateComment` or `deleteComment` action (observed 2026-11-25 on the test board). The log therefore never changes a comment already logged; the current text of a thread comes only from a thread read, and every agent command reading a thread reads it live ([functional §5](boardreader_spec_functional.md#5-comments)).

The clients keep their own cursors as today (the seat state's `comment_cursor`, the Q&A launcher's cursor) and set them to the returned completeness point after every successful call (a comment date; a cursor stored by an earlier version as an action ID is converted once, at its first use, to that comment's date from a live thread read of its card, or to the start of the second in its ID when the comment is gone), also when no comment of their card arrived, so a quiet card is served by the log from its next poll on and costs no request of its own: today `forward.poll` and `QaLauncher.poll_active` move their cursor only on a comment of their card, which this changes. The reader keeps no per-client state.

## 8. Callers and their freshness

| Caller | Reads | Freshness |
|---|---|---|
| `workflow.tick` before the project lock; `launcher.periodic_board_check`, `check_user_move` | `snapshot` | `max_age(poll_seconds)` |
| Every read under the project lock: the claim recheck in `workflow`, `integrate`, the Q&A claim and handoff, `seatremove`, `labels` | `snapshot`, `card`, `list_info` | `LIVE` |
| `cards.converge` under the project lock | the snapshot of `workflow.tick` (`max_age(poll_seconds)`) to find refs that may need a step; `card` before each step that changes Trello or a ref | the snapshot as given; `card` `LIVE` |
| `launcher.start`, `failed_start`, `seats` (seat creation, list checks, `acquire_qa`) | `snapshot`, `card`, `list_info` | `LIVE` |
| `QaLauncher` idle claim scan, `check_list_while_waiting`, `poll_active` | `snapshot`, `list_info`, `card`, `new_comments` | `max_age(qa_poll_seconds)` |
| `QaLauncher.open_conversation` (thread at the start of a conversation) | `thread` | `LIVE` |
| `forward.poll` (`waiting_human`) | `new_comments` (its card) | `max_age(poll_seconds)` |
| `boardsettings` periodic read | `settings_cards` | `max_age(settings_poll_seconds)` |
| `boardsettings` and `cardsections_feedback` marker checks before a comment | `thread` | `LIVE` |
| `notify` from a launcher; from a command | `members`, `admins` | the caller's poll freshness; `LIVE` |
| `notify` | `me`, `card_creator` | immutable |
| `doctor`'s credentials check | `me` | `LIVE` |
| `init` before the project exists | `me`, `board_info`, `snapshot` | an unbound reader: always live ([§1](#1-scope)) |
| `destroy`'s inventory and placement checks | `board_payload`, `closed_cards`, `card_place` | always live, raw |
| Agent commands (`task`, `answer`, `handoff`, `done`, …), `attachments.for_task` | `card`, `thread`, `snapshot` | `LIVE` |
| Other reads of the CLI commands (`status`, `reconcile`, `doctor`, `destroy`, `merge-request`, `release`, `seat …`, `up`, `agent`) | all | `LIVE` |

**Convergence.** `cards.converge` keeps deciding from the snapshot it is given, which may be as old as `poll_seconds`: a ref whose card the snapshot shows where the ref expects it needs no step and costs no request. Before any step that changes Trello or a ref (a move, a ref deletion, a label change, a `human_moved` report), it reads that card `LIVE` under the project lock and decides again on that card, so a move made by the user after the snapshot is never reverted. An idle board with several seats therefore makes no live read per tick.

The launchers keep their wake-up intervals; a poll interval of 0 makes every poll a live read.

## 9. Trello usage

Three revisions of [`kanban_requirements.md`](kanban_requirements.md), made in the implementation commit:
- **B3:** the board read asks for the full attachment fields of F2 with their position (`card_attachment_fields=id,name,fileName,url,bytes,mimeType,idMember,isUpload,pos,date`), so a board read refreshes the card units completely (checked on the test board on 2026-11-25, without `pos`; `pos` is checked by the live test of [§10](#10-tests)). If that test shows that the nested read does not return `pos`, B3 is revised without it: a card unit refreshed by a board read keeps the order of the card's last single-card read for the attachments it already had and puts the others after them by `date`, and the agent commands, which read the card `LIVE` (C1), are not affected.
- **C1:** the single-card read asks for the attachments' `pos` too, so `card` gives them in the card's order without the separate attachment read (F2) that `attachments.for_task` makes today.
- **New M7 (SHOULD):** read the new comments of a whole board after a point in one request, newest first, with pagination: `GET /1/boards/{id}/actions?filter=commentCard&since=<date>&before=<date>&limit=1000` (at most 1000 per page; `limit` above 1000 is a 400). Edits and deletions of comments are not reported by this read; kankan does not rely on them. kankan relies on the read being ordered newest first by action date, on `since` and `before` accepting an ISO 8601 date with millisecond precision and excluding it, in the board-wide read and in the per-card read (M2), and on an edit keeping the action's date; it does not rely on action IDs being ordered, which within one second they are not (all observed 2026-11-26). Without M7 kankan reads each watched card's comments (M2), as before the feature.

The requests for data the agents share no longer grow with the number of agents ([functional §4](boardreader_spec_functional.md#4-trello-requests)); G6's rate-limit estimate is unchanged and now has more margin.

## 10. Tests

- **Unit:** comment points as dates: comments posted within one second whose IDs are not in creation order delivered in date order, none twice; a cursor stored as an action ID converted (comment present, comment deleted); the completeness point never below `after` (a caller ahead of the log after a fallback read is not moved back and gets no comment twice); the `origin` horizon on a board without comments (an empty `after` served by the log, no card read per poll); a fresher card unit showing the card archived, deleted or on another board removing it from the board view; the cache directory deleted between a fetch and its store (marks and locks kept, the stale result not stored); the `not_before` rule for `LIVE` and `max_age`; write marks for each row of the table of [§4](#4-freshness-and-invalidation) (a fetch sent before a write is stale after it, through `snapshot`, `card`, `list_info`, `settings_cards`, `thread` and `new_comments`); a fetch in flight across a write is fetched again for a read called after the write; newest wins; the board view merging card units, patching at most `BOARD_PATCH_MAX` cards, and its response date; the comment log (start, `since` exclusive, pages, horizon, pruning, the thread fallback and its completeness point for an empty `after`, for a card with no new comment and for a comment arriving during the fallback read); format and board mismatch; a corrupt file ignored; `me(LIVE)` with revoked credentials after a cached `me`; attachments ordered by `pos` from a board read and from a card read.
- **Convergence:** a user move after the tick's snapshot is not reverted; an idle board with several seats makes no live card read per tick.
- **Destroy:** the existing malformed-response tests of the inventory and the placement check run through `board_payload` and `card_place`.
- **Board settings:** the same setting edit applied through a live read and through another process's cached read, with the quiet period measured from the cached response date.
- **Fake Trello:** the board-wide comment actions read (`filter`, `since`, `before`, `limit` up to 1000), the full attachment fields in the board read, edits that change an action's text and deletions that remove it.
- **Processes:** two processes polling with `max_age` while the fake board is slow make one board request; a `LIVE` read under the project lock does not wait for a slow poll of another process (fake Trello `delay` fault).
- **Existing tests:** the helpers that wait for a launcher's polls by counting fake-Trello requests (`wait_board_polls`, `wait_card_polls`) no longer work: a card's comments now come from the board-wide read, and another process's fetch may answer a poll. Under the test hook `KANKAN_TEST_READS=1` each launcher writes an audit-log entry when one of its polls completes (`poll_done`, with the seat and the poll kind); the helpers wait for those entries of the launcher concerned. Request counts are asserted only in the reader's own tests.
- **Static:** only `boardreader` calls `TrelloClient` read methods.
- **Live Trello:** the board-wide comment read (pages, `since` exclusive, edits and deletions not reported, `since` and `before` with a date, IDs of one second not ordered by creation, an edit keeping the date) and the attachment fields of the board and card reads, `pos` included.

## 11. Accepted limitations

- A comment edited or deleted is seen only through a thread read ([§7](#7-comment-log)).
- A backwards jump of the wall clock can let a cached unit count as fresher than it is, for the length of the jump; the units are re-read at the following polls.
- A waiter that gives up on a fetch lock after the time budget makes one more request than needed.
- A reaction to a user's change can take up to about twice the poll setting ([functional §3](boardreader_spec_functional.md#3-poll-settings)).
- A process crashing between a Trello write and the storing of its write mark lets `max_age` readers see the former state until the unit is next refreshed ([§4](#4-freshness-and-invalidation)); live reads, and so every step under the project lock that changes Trello or a ref, are not affected.
- A card watched by no process for 24 hours costs one card read when it is polled again ([§7](#7-comment-log)).
- Two comments with the same millisecond date can lose one of them when a point falls exactly between them ([§3](#3-units-and-timestamps)).
- A cursor stored by an earlier version whose comment was deleted is converted to the start of its second: comments of that second after it are delivered again.

## 12. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md), by its section numbers. That file is not edited.

**§3 Principles.** Add:

11. **Reads state their freshness.** Every Trello read goes through the board reader and says how old its data may be; every step under the project lock that changes Trello or a ref reads the data it acts on live (convergence may find candidates in an older snapshot, [§8](#8-callers-and-their-freshness)).

**§5.2 Local state.** Add:

| Board cache | `<gcd>/kankan/board-cache/` ([§6](#6-storage)) |
| Board cache write marks and locks | `<gcd>/kankan/board-marks.json`, `<gcd>/kankan/board-<kind>.fetch.lock`, `<gcd>/kankan/board-store.lock` ([§5](#5-locks-and-fetching), [§6](#6-storage)) |

**§5.3 Components.** Add:

| `boardreader` | The only reader of Trello: freshness, cache, write marks, fetch sharing, comment log. |

**§5.3 Components.** Replace the text starting “| `trello` | REST client: retries, read-after-write, error …” with:

| `trello` | REST client: retries, read-after-write, error classification, rate limits. Isolates Trello from the rest (see [`kanban_requirements.md`](kanban_requirements.md)); its reads are called only by `boardreader`, its writes report their effects to it. |

**§8.5 Cycle and signals.** Add:

> The cycle's first board read accepts data as old as `poll_seconds`; every check under the project lock reads live, and convergence reads live each card it changes ([§8](#8-callers-and-their-freshness)). In `waiting_human`, comment forwarding sets `comment_cursor` to the completeness point of every comment read, also when no comment arrived ([§7](#7-comment-log)).

**§10.4 Convergence.** Add:

> Convergence decides from the cycle's board read, which may be as old as `poll_seconds`, and reads a card live under the project lock before every step that changes Trello or a ref ([§8](#8-callers-and-their-freshness)).

**§15.1 Principles.** Add:

- **Shared reads:** a read with a maximum age may be answered by another process's request; a live read never waits for another process ([§5](#5-locks-and-fetching)).

**§18.1 Strategy.** Add the tests of [§10](#10-tests).

## 13. Changes to other technical specifications

**Questions §4 Launcher, “Polling the active card”.** The card read (C1) and the comment read become `card` and `new_comments` with `max_age(qa_poll_seconds)`, and the `Q&A` list read becomes `list_info` from the board view; the cursor is set to the completeness point of every `new_comments` call, also when no comment arrived ([§7](#7-comment-log)); the request counts of questions §8 become those of [§9](#9-trello-usage).

**Destroy §3 Inventory and §6 Trello usage.** `destroy` reads through the reader's always-live, uncached methods (`board_payload`, `closed_cards`, `card_place`, [§2](#2-interface)) instead of `TrelloClient`; their requests, raw results and error handling are unchanged. Its state removal (§4 step 5) covers the board cache and `board-marks.json`, and keeps the board-cache lock files like every lock file.

**Board settings §2 Reading.** The settings read becomes `settings_cards` with `max_age(settings_poll_seconds)`, answered from the board view; its `response_date` is the cached response date of that view ([§3](#3-units-and-timestamps)), not the time the cache was read, so the quiet period and the rejection of older observations work as with a live read.
