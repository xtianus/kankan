# kankan board reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every Trello read of kankan goes through one shared `BoardReader` per project and machine, which states how old its data may be, answers from a file cache when fresh enough, never gives the state before kankan's own write as fresh, shares one fetch among polling processes, and finds the new comments of every card with one board-wide read.

**Architecture:** a new module `kankan/boardreader.py` (`BoardReader`, `LIVE`, `max_age`) is the only caller of `TrelloClient`'s read methods; `Project.open` builds it as `project.board` and installs a write hook on the client so every write marks the units it changed. The cache lives in `<gcd>/kankan/board-cache/` (data only); the write marks (`board-marks.json`) and the locks (`board-<kind>.fetch.lock`, `board-store.lock`) live beside it in `<gcd>/kankan/`. A comment log (`log.json`) is refreshed by the board-wide comment actions read (M7). Callers are migrated module by module to `project.board` with the freshness of technical §8.

**Tech Stack:** Python ≥ 3.10, standard library only at runtime; pytest (`setsid -w .venv/bin/python -m pytest -q -n 16 --dist worksteal` for the full suite); fixtures `kproject`, `fake_trello` (`tests/fakes/fake_trello.py`, `FakeTrello.inject` faults including `delay`), `tests/flow.py`, `tests/helpers.py`, `tests/launcher_helpers.py`; live tests in `tests/live/`.

**Spec:** [board reader functional](../aispecs/boardreader_spec_functional.md) (F) and [board reader technical](../aispecs/boardreader_spec_technical.md) (T). Read both first; the Q&A pool ([`qapool_spec_technical.md`](../aispecs/qapool_spec_technical.md)) builds on this feature and is planned separately in [2026-11-26-qapool.md](2026-11-26-qapool.md).

**Status:** planned, not started; reviewed by the internal advisor and the Codex advisor (2026-11-26), findings folded in.

## Global Constraints

- 4-space indentation; standard library only in `kankan/`; user-facing texts in `kankan/messages.py`.
- After Task 11 no module except `boardreader` calls a read method of `TrelloClient` (`me`, `member`, `card_creator`, `board_members`, `board_admins`, `board`, `board_payload`, `snapshot`, `closed_cards`, `card_place`, `card`, `settings_cards`, `attachments`, `list_info`, `comments`, `find_comment`, and the new comment-actions read). The client's internal read-backs (`_write_once`'s `find`, `comment_once`'s marker search) stay inside `trello` and are never cached.
- Freshness: `LIVE` = a request sent after the call; `max_age(s)` = a request sent at most `s` seconds before the call; `max_age(0)` ≡ `LIVE`. Every unit stores its **send time** (`time.time()` just before sending) and Trello's response date. A unit serves a read only when its send time is ≥ `not_before` **and** after its write mark (T§3, T§4).
- A `LIVE` read never takes a fetch lock (never waits for another process). A `max_age` read takes the unit kind's fetch lock, re-checks the cache, fetches only if still needed; it waits at most the client's time budget + 2 s, then fetches without the lock (T§5).
- Lock order: every existing kankan lock → fetch lock → store lock; nothing else is taken while holding them. Lock files are never deleted and are **not** under `board-cache/`.
- Cache files: JSON, written to a temporary file and renamed, mode 0600 in a 0700 directory; each carries `format: 1`, `board_id`, send time and response date; a mismatching or unparsable file counts as missing. Raw Trello payloads are stored and decoded by the existing decoders of `trello.py`, so cached and live results have the same form.
- Comment cursors are Trello action IDs compared as equal-length hex strings; the local clock never orders comments. The completeness point returned by `new_comments` is never below `after` (T§7).
- Callers keep their existing behaviour apart from freshness; `kankan` command outputs are unchanged.
- **Existing suite and caching.** Many tests change the fake board directly (`FakeTrello.user_move`, `user_comment`, …), which sets no write mark, and then expect the next tick to see it. An autouse fixture in `tests/conftest.py` sets `KANKAN_TEST_NO_CACHE=1`, under which every `max_age` read behaves as `LIVE` (the comment log still serves `new_comments`, refreshed live); the reader's own tests (Tasks 3–7, the caller tests of Tasks 10–11 that count requests) and the process tests unset it. Production code never reads the variable except in `boardreader`.
- **Client construction sites.** `kankan/project.py` (bound reader, hook installed), `kankan/init.py` (two clients before the binding: unbound reader), `kankan/destroy.py` (its own `Ctx` client: unbound reader, always-live methods, no hook needed since destroy removes the cache). Tests that build a `TrelloClient` directly (`test_e2e_support`, `test_destroy_trello`, `test_branches`, `test_destroy_final`, `tests/live/`) stay as they are: the static rule covers `kankan/` only.
- **One commit at the end** (global git rule), `kankan/VERSION` incremented once; revise [`kanban_requirements.md`](../aispecs/kanban_requirements.md) (B3, C1, new M7 with its assumptions: ordered action IDs, `since`/`before` naming a deleted action, `pos` in nested reads) in the same commit; push after committing. Tasks end with a green test run, not a commit.

## Review Focus

1. **Read-after-write** — a fetch sent before a kankan write, or in flight across it, is never given as fresh after the write, through every read method (T§4). Task 3 `test_mark_*`, `test_inflight_*`; Task 4 `test_view_card_mark_*`.
2. **Comment log correctness** — no comment lost and none given twice: `since` exclusive, pages, `origin`, fallback point, never moving a cursor back, a comment arriving between log and card reads, pruning (T§7). Task 6.
3. **Cross-process sharing** — two pollers make one request; a `LIVE` read under the project lock never waits for a slow poll; a deleted cache while processes run loses no mark or lock. Task 7.
4. **Convergence** — decisions from a `max_age` snapshot, a `LIVE` card read before each step; a user move after the snapshot is never reverted; an idle board makes no live read per tick (T§8). Task 10.
5. **Board settings dates** — the quiet period measured from the cached response date, not the cache read time (T§13). Task 11.

---

### Task 0: Live probes before building on them

**Files:**
- Modify: `tests/live/test_trello_live.py` (probes, kept as live tests: nested attachment `pos` and `date` in the board read with `card_attachment_fields`; `pos` in the single-card read; the board read's checklists and a board-nested card decoding like a C1 card; M7 pages, `since` exclusive; `since` and `before` naming a deleted comment; action IDs of one comment equal in the board-wide and per-card reads and ordered by creation)

- [ ] **Step 1:** run them on the test board per [developer manual §3](../docs/developer-manual.md).
- [ ] **Step 2:** record the results in `aiknowledge/trello.md`; a negative result changes Tasks 2, 4 and 6 before they start (nested `pos` missing → T§9's fallback; `since`/`before` unreliable with a deleted cursor → the log keeps the newest surviving ID as its cursor; stop and ask the user if the action IDs are not ordered).

### Task 1: Fake Trello — board-wide comment actions, attachment fields, edits and deletions

**Files:**
- Modify: `tests/fakes/fake_trello.py` (`route`: `GET /1/boards/{id}/actions` with `filter=commentCard`, `since`, `before`, `limit` ≤ 1000 (400 above), newest first; the board read honours `card_attachment_fields` including `pos`, `idMember`, `date`; the card read returns attachment `pos` when asked; `user_edit_comment` changes an action's text, `user_delete_comment` removes it; action IDs created in increasing order by `new_id`; a request counter per route kind for the reader's own tests)
- Test: `tests/test_fake_trello_actions.py`

- [ ] **Step 1: Write failing tests** — `test_actions_newest_first_since_exclusive`, `test_actions_before_pages`, `test_actions_limit_over_1000_is_400`, `test_actions_since_deleted_id_still_filters`, `test_edit_changes_text_no_new_action`, `test_delete_removes_action`, `test_board_read_attachment_fields_with_pos`, `test_card_read_attachment_pos`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_fake_trello_actions.py` — FAIL.
- [ ] **Step 3: Implement** the routes and helpers; IDs keep the existing 24-hex format with a time-ordered prefix.
- [ ] **Step 4: Run** the new file and `tests/test_trello.py` — PASS.

### Task 2: TrelloClient — raw reads for the reader, write hook

**Files:**
- Modify: `kankan/trello.py`
  - `board_comment_actions(board_id, since=None, before=None, limit=1000) -> List[Comment]` (M7, one page) and `newest_comment_action(board_id) -> Optional[Comment]` (`limit=1`); each returned `Comment` also carries `card_id`.
  - board read (`_board_read`): `card_attachment_fields=id,name,fileName,url,bytes,mimeType,idMember,isUpload,pos,date`, decoded into `Card.attachment_items` ordered by `pos` (fallback order by `date` when `pos` is absent, T§9); single-card read (`card`): attachment `pos`, so `attachment_items` comes in card order.
  - Raw reads for the reader: `read_*` variants return `(payload, sent_at, response_date)` with the **raw** payload, which the reader stores; the decoders (`_card`, `_snapshot_from`, `_comment`, …) become module functions the reader calls on stored and fresh payloads alike, so cached and live results are identical. The existing public read methods keep their signatures and `Response`'s existing unpacking until their callers migrate (Task 11 removes the unused ones); each caller group migrates together with the tests that monkeypatch it.
  - `on_write: Optional[Callable[[WriteEffect], None]]`, called after every write method returns **and** after a write whose outcome is uncertain (timeout), never before; `WriteEffect(kind, card_id=None, list_id=None)` with the kinds of T§4's table. `create_check_item` and `delete_check_item` gain a `card_id` parameter (their callers in `boardsettings`/`settings_setup` know the card) so the effect marks that card.
- Modify: `kankan/trello.py` `Comment` (`card_id: Optional[str] = None`).
- Test: `tests/test_trello.py` (extend)

- [ ] **Step 1: Write failing tests** — `test_decoders_same_result_for_raw_and_live`, `test_board_nested_card_decodes_like_card_read` (checklists included), `test_board_comment_actions_pages_and_card_ids`, `test_newest_comment_action_empty_board_is_none`, `test_board_read_attachment_items_ordered_by_pos`, `test_card_attachment_items_ordered_by_pos`, `test_on_write_called_for_each_write_kind` (one per row of T§4), `test_on_write_called_after_timeout`, `test_read_variants_return_send_time_before_request`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_trello.py` — FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** `tests/test_trello.py tests/test_attachments.py` — PASS.

### Task 3: `boardreader` core — freshness, storage, marks, newest wins, in-flight check

**Files:**
- Create: `kankan/boardreader.py` (`LIVE`, `max_age(seconds)`, `Freshness.not_before(now)`; `_Store`: read/write unit files under `board-cache/`, format/board checks, atomic rename under `board-store.lock`, newest-wins on send time; `_Marks`: `board-marks.json` (unit key → mark time) written under the store lock; `_serve(unit_key, not_before)`; `_fetch_checked(unit_key, fetch, called_at)`: after a fetch, re-read the marks and refetch when a mark was set after the fetch's send time and after `called_at`; the write hook `BoardReader.on_write(effect)` translating T§4's table into marks)
- Modify: `kankan/config.py` `Paths` (`board_cache`, `board_marks`, `board_fetch_lock(kind)`, `board_store_lock`)
- Test: `tests/test_boardreader_core.py`

**Interfaces:**
- Produces: `BoardReader(paths, client, board_id)`, `BoardReader.unbound(client)` (no cache: every read live, nothing stored), `on_write(effect)`. Unit keys: `board`, `card:<id>`, `thread:<id>`, `log`, `members`, `immutable`.

- [ ] **Step 1: Write failing tests** — `test_live_needs_request_after_call`, `test_max_age_zero_is_live`, `test_max_age_served_from_cache`, `test_mark_makes_older_unit_stale` (parametrized over T§4's rows), `test_inflight_fetch_across_write_refetched_for_later_call`, `test_inflight_result_returned_to_earlier_call`, `test_newest_wins_slow_older_fetch_not_stored`, `test_format_or_board_mismatch_ignored`, `test_corrupt_file_ignored`, `test_error_stores_nothing`, `test_unbound_reader_never_stores`, `test_cache_deleted_marks_and_locks_kept` (delete `board-cache/` between a fetch and its store: the stale result is not stored, `board-marks.json` intact).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_boardreader_core.py` — FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** — PASS.

### Task 4: Board view, card, list and settings reads

**Files:**
- Modify: `kankan/boardreader.py` (`snapshot(fresh)`: board unit merged with fresher card units; a fresher card unit showing the card closed, deleted or on another board removes it; a card the board does not list is never added; at most `BOARD_PATCH_MAX = 3` stale cards patched by single reads, otherwise a board read; the view's response date is the oldest of its parts; `card(card_id, fresh)` (`None` when deleted, closed card when archived, attachments by `pos`); `list_info(list_id, fresh)` and `settings_cards(list_id, fresh)` answered from the view; a board read refreshes the card units of the open cards it returns and deletes card and thread files of absent cards older than one day)
- Test: `tests/test_boardreader_view.py`

- [ ] **Step 1: Write failing tests** — `test_view_merges_fresher_card_unit`, `test_view_card_mark_forces_patch`, `test_view_patches_at_most_three_then_board_read`, `test_view_response_date_oldest_part`, `test_view_fresher_closed_card_removed`, `test_view_fresher_deleted_card_removed`, `test_view_card_moved_to_other_board_removed`, `test_view_never_adds_unlisted_card`, `test_card_none_when_deleted_closed_when_archived`, `test_list_info_deleted_list_none`, `test_settings_cards_from_view_with_checklists`, `test_attachments_ordered_by_pos_from_board_and_card_read`, `test_board_read_prunes_old_absent_card_files`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** `tests/test_boardreader_view.py tests/test_boardreader_core.py` — PASS.

### Task 5: Threads, members, immutable and always-live reads

**Files:**
- Modify: `kankan/boardreader.py` (`thread(card_id, fresh)` (all pages of M2); `members(fresh)`, `admins(fresh)`; `me(fresh)` keyed by a hash of the token, `me(LIVE)` always asks and replaces; `card_creator(card_id)` immutable; `closed_cards()`, `board_info(short)`, `member(username)`, `board_payload()`, `card_place(card_id)`: always live, never stored, raw results)
- Test: `tests/test_boardreader_reads.py`

- [ ] **Step 1: Write failing tests** — `test_thread_marked_by_comment_post_edit_delete`, `test_members_and_admins_cached`, `test_me_live_after_revoked_credentials` (F test 9), `test_me_cached_per_token_hash`, `test_card_creator_cached_forever`, `test_always_live_reads_never_stored`.
- [ ] **Step 2–4:** run, implement, run — PASS.

### Task 6: Comment log and `new_comments`

**Files:**
- Modify: `kankan/boardreader.py` (`_Log` in `log.json`: entries `{id, card_id, member_id, username, date, text}`, `cursor`, `horizon` (`"origin"` or an ID), send time; start: `newest_comment_action` → cursor and horizon, or empty cursor with horizon `origin`; refresh: `since=cursor` (no `since` while the cursor is empty), pages with `before=<oldest>` while a page is full; pruning entries older than 24 h moving the horizon to the newest dropped ID; M7 unsupported (a 400/404 on the board actions route, not a transient error) → the log is marked unsupported and `new_comments` reads each card's comments (M2) as before the feature, while a transient error fails the read and is retried at the next poll; `new_comments(card_id, after, fresh) -> (comments, point)`: served by the log when `after` is not older than the horizon or the horizon is `origin` (point `max(cursor, after)`); otherwise bring the log to the freshness, take its cursor, read the card's comments (M2 `since=after`, all for an empty `after`) and return point `max(cursor, after, newest read)`)
- Test: `tests/test_boardreader_log.py`

- [ ] **Step 1: Write failing tests** — `test_log_start_sets_cursor_and_horizon`, `test_log_start_empty_board_origin`, `test_origin_serves_empty_after_without_card_read`, `test_refresh_since_exclusive`, `test_refresh_pages_full_pages`, `test_no_new_comment_advances_send_time_only`, `test_serving_point_never_below_after` (fallback returns B, next served call returns ≥ B, B never returned twice), `test_fallback_for_after_older_than_horizon`, `test_fallback_comment_between_log_and_card_read_covered`, `test_fallback_card_without_new_comment_gets_servable_point`, `test_pruning_moves_horizon`, `test_polled_card_never_falls_behind_horizon`, `test_card_unpolled_24h_costs_one_card_read`, `test_edit_and_delete_not_reflected_in_log`, `test_comment_post_marks_log`, `test_m7_unsupported_falls_back_to_card_reads`, `test_m7_transient_error_fails_and_retries`.
- [ ] **Step 2–4:** run, implement, run — PASS.

### Task 7: Fetch locks and process sharing

**Files:**
- Modify: `kankan/boardreader.py` (per-kind fetch locks `board`, `cards`, `threads`, `log`, `members` with the wait bound; `LIVE` skips them)
- Test: `tests/test_boardreader_procs.py` (subprocesses against the fake server with the `delay` fault)

- [ ] **Step 1: Write failing tests** — `test_two_pollers_one_board_request` (F test 5), `test_live_read_does_not_wait_for_slow_poll`, `test_waiter_gives_up_after_budget_and_fetches`, `test_cache_deleted_while_processes_poll` (F test 6).
- [ ] **Step 2–4:** run, implement, run — PASS.

### Task 8: Wiring — `Project.open`, `init`, static rule

**Files:**
- Modify: `kankan/project.py` (`Project.board`: `BoardReader(paths, trello, cfg.board_id)`; `trello.on_write = board.on_write`)
- Modify: `kankan/init.py` (`BoardReader.unbound(client)` before the binding exists; the unbound reader's board reads take the board ID found by `board_info` as an argument, since no binding gives it)
- Create: `tests/test_static_reads.py` (AST scan: only `kankan/boardreader.py` calls the read methods listed in the constraints on a `TrelloClient`; temporarily allow-listed modules shrink to none by Task 11)
- Test: `tests/test_init.py` (unchanged behaviour)

- [ ] **Step 1: Write failing tests** — `test_project_board_installed_and_hook_set`, `test_init_uses_unbound_reader`, `test_static_only_boardreader_reads` (with the allow-list of not yet migrated modules).
- [ ] **Step 2–4:** run, implement, run — PASS.

### Task 9: Test hooks — cache switch and launcher polls

**Files:**
- Modify: `tests/conftest.py` (autouse `KANKAN_TEST_NO_CACHE=1`, a marker `cache` that unsets it), `kankan/boardreader.py` (the switch)
- Modify: `kankan/launcher.py`, `kankan/qa_launcher.py`, `kankan/forward.py` (under `KANKAN_TEST_READS=1`, an audit entry `poll_done` with `seat` and `kind` (`board`, `card`, `comments`, `settings`) when a poll completes)
- Modify: `tests/test_qa_launcher.py` (`wait_board_polls`, `wait_card_polls` wait for `poll_done` entries of the launcher concerned), `tests/launcher_helpers.py` (set the variable for launcher subprocesses)

- [ ] **Step 1:** switch the helpers and set the variable; run `tests/test_qa_launcher.py tests/test_launcher_loop.py` — PASS before any caller migrates (the hook is additive).

### Task 10: Migrate the work cycle — `workflow`, `cards`, `launcher`, `forward`

**Files:**
- Modify: `kankan/workflow.py` (`tick` before the lock: `snapshot(max_age(poll_seconds))`; the locked recheck and claims: `LIVE`; `_gone_card` path: `card(LIVE)`), `kankan/cards.py` (`converge`: decides from the snapshot given; `card(LIVE)` under the project lock before every step that changes Trello or a ref, deciding again on that card), `kankan/launcher.py` (`periodic_board_check`, `check_user_move`: `max_age(poll_seconds)`; `start`, `failed_start`: `LIVE`), `kankan/forward.py` (`poll`: `new_comments(card, cursor, max_age(poll_seconds))`; `comment_cursor[card] = point` after every successful call, also with no comment)
- Test: `tests/test_boardreader_convergence.py`; existing `tests/test_workflow.py`, `tests/test_cards.py`, `tests/test_user_moves.py`, `tests/test_ask.py`, `tests/test_launcher_loop.py`

- [ ] **Step 1: Write failing tests** — `test_user_move_after_snapshot_not_reverted`, `test_idle_board_no_live_card_read_per_tick`, `test_forward_cursor_moves_without_comment`, `test_waiting_human_quiet_card_no_own_request`, `test_kankan_move_never_seen_as_former_list` (F test 4), `test_command_sees_user_move_at_once` (F test 3).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**; remove these modules from the static allow-list.
- [ ] **Step 4: Run** the new file and the existing files listed — PASS.

### Task 11: Migrate the remaining callers

**Files:**
- Modify: `kankan/qa_launcher.py` (`tick`, `check_list_while_waiting`, `poll_active`: `max_age(qa_poll_seconds)`, `new_comments` with the cursor set to the point after every call; `open_conversation`: `thread(LIVE)`), `kankan/qa.py` (`card_state`, `claim`: `LIVE`), `kankan/agentcmds.py` (`card`, `thread`: `LIVE`), `kankan/attachments.py` (`for_task`: attachment order from `card(LIVE)`, the separate F2 read removed), `kankan/boardsettings.py` (periodic read `settings_cards(max_age(settings_poll_seconds))` with the view's cached response date; marker checks `thread(LIVE)`), `kankan/settings_setup.py`, `kankan/cardsections_feedback.py` (`LIVE`), `kankan/notify.py` (`members`/`admins` with the caller's freshness, `me`/`card_creator` immutable), `kankan/doctor.py` (`me(LIVE)`; other reads `LIVE`), `kankan/diag.py`, `kankan/seats.py`, `kankan/seatremove.py`, `kankan/integrate.py`, `kankan/labels.py`, `kankan/agents_cmd.py` (`LIVE`), `kankan/destroy.py` (`board_payload`, `closed_cards`, `card_place` through the reader; state removal covers `board-cache/` and `board-marks.json`, keeps the lock files)
- Test: existing suites; `tests/test_boardreader_callers.py`

- [ ] **Step 1: Write failing tests** — `test_qa_quiet_cards_no_own_request` (F test 11, ten cards), `test_board_without_comments_quiet_card_no_request` (F test 12), `test_one_board_comment_read_serves_ask_and_followup` (F test 2), `test_edited_comment_shown_by_task_not_delivered` (F test 7), `test_settings_cached_read_uses_cached_response_date`, `test_attachments_order_after_reorder_from_cache` (F test 10), `test_destroy_removes_cache_keeps_locks` (F test 8), `test_three_agents_idle_board_read_once_per_qa_poll` (F test 1, also asserting that a card added to `Backlog` is claimed within twice `poll_seconds`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**; the static allow-list becomes empty.
- [ ] **Step 4: Run the full suite** `setsid -w .venv/bin/python -m pytest -q -n 16 --dist worksteal` — PASS.

### Task 12: Live Trello run

**Files:**
- Modify: `tests/live/test_trello_live.py` (the probes of Task 0, now through `BoardReader`: a live `new_comments` across pages and the attachment order of `snapshot` and `card`)

- [ ] **Step 1:** run the live tests per [developer manual §3](../docs/developer-manual.md) on the test board — PASS.

### Task 13: Documents, version, knowledge base

**Files:**
- Modify: `aispecs/kanban_requirements.md` (B3, C1, M7 with its assumptions), `aispecs/boardreader_spec_functional.md` and `_technical.md` (status: implemented locally; remaining live checks if any), `AGENTS.md` (feature list status), `docs/user-manual.md` (meaning of the three poll settings, the cache directory), `docs/developer-manual.md` (`KANKAN_TEST_READS`, the static rule), `aiknowledge/architecture.md` and `aiknowledge/trello.md` (the reader, units, marks, log; observed M7 behaviour), `aiknowledge/testing.md` (`poll_done` helpers), `kankan/VERSION`

- [ ] **Step 1:** update; run the full suite once more — PASS.
- [ ] **Step 2:** one commit, push.
