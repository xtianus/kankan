# kankan Q&A agent pool — technical specification

**Status:** specified, not implemented; requires the board reader ([`boardreader_spec_technical.md`](boardreader_spec_technical.md)), implemented first.
**Date:** 2026-11-25
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3, [`questions_spec_technical.md`](questions_spec_technical.md), [`seat_spec_technical.md`](seat_spec_technical.md), [`stopall_spec_technical.md`](stopall_spec_technical.md) and [`boardreader_spec_technical.md`](boardreader_spec_technical.md); this feature changes them as listed in [§13](#13-changes-to-the-initial-technical-specification) and [§14](#14-changes-to-other-technical-specifications).
**Companion:** [`qapool_spec_functional.md`](qapool_spec_functional.md) — the behaviour this file implements; read it first.

**Split rule.** This file describes how kankan implements the feature; the functional file describes what users and agents observe. Section numbers are this feature's own.

## 1. Scope

A new module `kankan/qapool.py` holds the pool policy: cache readings and expiry ([§3](#3-prompt-cache-readings)), the routing round ([§5](#5-routing-round)), opening and closing agents ([§6](#6-opening-and-closing)), the answer record ([§5](#5-routing-round)) and the title prefixes ([§8](#8-title-prefixes)). `QaLauncher` runs in two roles: the **router** (`<type>-qa`), which also answers cards, and the **members** (`<type>-qa2`, …), which only answer the cards offered to them ([§4](#4-roles-and-offers)). A hidden command, `kankan statusline`, records Claude's cache expiry ([§3](#3-prompt-cache-readings)). Every Trello read goes through the board reader.

## 2. Seats and configuration

- **Names.** The seat pattern becomes `^[a-z]+-([0-9]+|qa([2-9]|[1-9][0-9]+)?)$` in `refstore` (inputs validated before reaching Git) and in `destroy`'s ref pattern. `seats.seat_type()` (the part before the last `-`) is unchanged. `qapool.router(cfg)` gives the registered `<type>-qa`, `qapool.members(cfg)` the registered `<type>-qaN`; every registered `qa` seat has the router's type (checked when a member is registered).
- **Registration.** A member is registered by the router only ([§6](#6-opening-and-closing)), with `role: qa`, no lists, and its worktree created like a seat worktree. The places that count or list seats for the user skip members: `seat_cap` and the per-type counts (as for every `qa` seat today), `kankan up` without arguments and `agents_cmd.registered_args`, the stop record and the autostart arguments (both keep `qa=<type>` only).
- **Configuration.** `config.json` gains `qa_prefix_warm` (default `🟢`) and `qa_prefix_cold` (default `⚪`): non-empty strings without whitespace, validated by `config`.
- **Adapters.** Each adapter gains `prompt_cache`: `{"expiry": "exact" | "estimated", "lifetime_minutes": <int>, "reporter": <name or null>}`. Defaults: Claude `{"expiry": "exact", "lifetime_minutes": 60, "reporter": "claude_status_line"}`, Codex `{"expiry": "estimated", "lifetime_minutes": 30, "reporter": null}`. `exact` requires a known reporter; the adapter validation refuses `exact` with none.

## 3. Prompt cache readings

**Reporter `claude_status_line`.** The launcher of a Q&A seat whose adapter names this reporter appends `--settings {"statusLine":{"type":"command","command":"<gcd>/kankan/bin/kankan statusline"}}` to the agent command (the wrapper of [initial technical §7.4](initial_spec_technical.md#74-identity-and-process-data), already first on the agent's `PATH`). Claude Code runs the status line command after every request of the agent, also in the middle of a turn, with the agent's environment and the worktree as working directory, and passes it a JSON object on standard input whose `prompt_cache` is `null` before the first request and then carries `expires_at` (Unix seconds: the request time plus the cache lifetime), `ttl` (a string such as `"1h"`) and `warm`; a `--settings` status line replaces the user's (Claude Code 2.1.332, observed on 2026-11-25 with a throwaway agent). `kankan statusline`:
1. identifies the seat as agent commands do (`KANKAN_SEAT`/`KANKAN_SESSION`, or the agent file), and does nothing more when it cannot;
2. writes `<gcd>/kankan/seats/<seat>.cache.json` atomically: `expires_at`, `ttl`, the agent's process ID and start time, and the time of the reading;
3. runs the user's own status line, if any, with the same standard input and working directory and a 5-second timeout, and prints its output: the first `statusLine` of type `command` in the worktree's `.claude/settings.local.json`, `.claude/settings.json`, then `~/.claude/settings.json` (Claude Code's precedence, the `--settings` override excepted);
4. always exits 0; an error is written to the audit log, never to the status line.

**Expiry.** `qapool.expiry(project, seat)` gives `(time, exact)`:
- `exact`: from `<seat>.cache.json` when it belongs to the seat's current agent process (same process ID and start time) and has a non-null `expires_at` (a past `expires_at` means cold);
- otherwise `estimated`: the end of the agent's last turn plus `lifetime_minutes`. The launcher records `turn_ended_at` in the seat state when a turn ends (a signal, a turn ending without one, or the agent ready after a typed turn); an agent without a finished turn since its start has no expiry and is cold.

A seat is **warm** when its expiry is in the future. An agent restart changes the process, so the reading no longer counts, and clears `turn_ended_at`: a restarted agent is cold.

## 4. Roles and offers

**Router.** The launcher of `<type>-qa` keeps every duty of today's Q&A launcher for its own conversation, replaces the idle claim of the top `Q&A` card (questions technical §4) with the routing round ([§5](#5-routing-round)) run every `qa_poll_seconds` in every state (also while its own agent answers), and owns the title prefixes ([§8](#8-title-prefixes)).

**Members.** The launcher of `<type>-qaN` never scans the `Q&A` list. It watches its own card as today (`poll_active`, through the board reader with `max_age(qa_poll_seconds)`) and takes the cards offered to it.

**Offers.** The router gives a card to a seat by writing `qa_offer` (`card`, `offer_id`, `at`) in that seat's state, under the **pool lock** (`<gcd>/kankan/qa-pool.lock`). An offer is taken by the seat's launcher when it is in `idle` or `qa_waiting` and its pending answer, if any, is settled:
- an offer of the card it already holds is dropped (its own polling delivers the input);
- otherwise it ends its conversation (`qa.end`, the card stays in `Q&A`), sends the adapter's new-conversation command and claims the offered card (`qa.claim`, which re-checks under the project lock that the card is in `Q&A`, open, without `HOLD`, with no other holder); the first turn marks the items newer than the latest complete answer as `new` (`qa.first_turn`);
- an offer that cannot be taken (the card left, got `HOLD` or another holder) is dropped and logged; the router routes the card again at its next round.

The router takes its own offers in-process. A seat clears `qa_offer` when it takes or drops it.

**Lock order.** `up.lock` → Q&A lifecycle lock → pool lock → conversation lock → project lock → seat-state lock → board-cache locks. The pool lock is held only to read the seat states and write offers, member registrations and the answer record, never across a Trello request or a process start.

## 5. Routing round

The router, every `qa_poll_seconds`:
1. **Reads**, through the board reader with `max_age(qa_poll_seconds)`: the board view and, per card in `Q&A`, `new_comments(after=<the card's checked mark>, card_id=…)`, served from the board-wide comment log ([board reader technical §7](boardreader_spec_technical.md#7-comment-log)) when the mark is not older than the log's horizon, otherwise by a read of that card's comments. There is no router cursor: each card keeps its own marks in the answer record (step 3), so a card that waited one or more rounds is examined again at every round. It reads the seat states, expiries and `qa` refs locally.
2. **Ended cards.** A member whose card left `Q&A`, was archived, deleted or got `HOLD` ends its conversation as today ([questions technical §4](questions_spec_technical.md#4-launcher)); the router marks it for closing ([§6](#6-opening-and-closing)).
3. **Cards needing an agent.** The **answer record** (`<gcd>/kankan/qa-answered.json`) keeps per card: `answered` (ID and date of the newest complete answer comment, updated by `qa_answer` when an answer completes), `delivered` (ID of the newest comment and date of the newest attachment delivered in any turn of the card, updated when a turn starts, so the inputs of a turn that ended in `fail` or without a signal count as delivered) and `checked` (the comment ID up to which the card's comments are known: after each round, the log's cursor when the log served the card, or the newest ID of the card's own read). A card not in the record is seeded from one live thread read: `answered` from the newest comment carrying the answer marker that `qa._complete` accepts, `delivered` equal to it, `checked` the newest comment ID; a card without any answer is seeded as never delivered. For each card in `Q&A` without `HOLD` whose `qa` ref names no alive seat of the pool, the router compares its inputs with the record. The card needs an agent when nothing was ever delivered and it has no `parked` handoff ref (a new card: its description is the question), or it has a comment by an input member (`notify.input_members`) with an ID greater than `delivered`, or an attachment by one dated after it: a card whose last turn failed waits for new input, as today ([questions §3.4](questions_spec_functional.md#34-errors)), and the new turn's prompt then marks everything after the latest answer as new, the failed turn's inputs included. Its **wait key** is the date of its oldest such input (the card's creation, from its ID, for a new card). Entries of cards that are archived or no longer exist are dropped by `reconcile --fix`.
4. **Choice**, cards in wait-key order. The step holds the Q&A lifecycle lock and then the pool lock (the order of [§4](#4-roles-and-offers)), and takes no other lock and makes no Trello request while holding them. For each card: the cold member or router holding the card (normally none: step 3 skips held cards, but an offer may be outstanding), then the available seat with the lowest number (idle or `qa_waiting`, no pending answer, no outstanding offer, cold or with an ended card), then a new member, registered and given the offer in this step and started after the locks are released ([§6](#6-opening-and-closing)); when every seat is busy or an opening failed in the previous round, the idle warm seat with the soonest expiry; otherwise the card waits. No member is opened while any seat of the pool is `limited`.
5. **Closing after a search.** When step 4 handled at least one card, every other idle cold member without an outstanding offer is closed.
6. **Prefixes** ([§8](#8-title-prefixes)).

## 6. Opening and closing

**Opening.** In step 4 of [§5](#5-routing-round), under the Q&A lifecycle lock and the pool lock: the router registers the lowest free `<type>-qaN` (configuration write) and writes the offer into its new seat state. After releasing both locks it starts the member's supervisor and launcher as `kankan agent <type> --qa --seat <seat>` would (`agents_cmd`, without the preflight already passed by the router; the model check of the `questions` pair is the router's). The member's launcher starts cold and takes the offer when ready. When the start raises, or the launcher does not reach `idle` within the startup deadline, the router logs `qa_open_failed`, closes the member as below, and the card falls back as in step 4 at the next round.

**Closing.** The router closes a member by marking it for graceful removal (`seatremove`'s mark, `removing: graceful`), never through `seatremove.remove` or `request_graceful`: those are the user's removal paths and also lower the autostart unit and the stop record ([seat technical §7](seat_spec_technical.md#7-autostart-and-stop-record)), which a member never appears in. The member's launcher exits from `idle`/`qa_waiting` once no answer is pending (`removal_drained`). At every round the router completes, outside its locks, the removal of each marked member whose launcher is gone (seat liveness `free` or `dead`) with `seatremove.complete`, which removes its worktree, seat ref, state and configuration entry. A member stopped by `kankan stop` or `kankan seat stop <type>-qa` is completed the same way at the router's next round or start. Members left registered after a crash or an immediate stop are completed when the router starts ([§9](#9-stop-start-and-type-change)).

## 7. Expiry note

`qa_answer` appends the note to the last chunk when it plans an answer (step 1 of [questions technical §6](questions_spec_technical.md#6-agent-commands)), so the note is part of the pending operation and a completed answer carries the note planned for it. For an `exact` expiry the time is the `expires_at` of the seat's current reading, written after the request that ran `kankan answer` (the requests that end the turn move the real expiry a few seconds later, never earlier); when there is no such reading or its `expires_at` is not in the future, the note is the estimated one: the planning time plus `lifetime_minutes`. The wording is `messages.QA_CACHE_EXACT` or `messages.QA_CACHE_ESTIMATED`. The time is shown as `HH:MM` in local time, with the zone's IANA name from `TZ`, `/etc/timezone` or the target of `/etc/localtime`, else the abbreviation of `time.strftime('%Z')`. The note counts towards the chunk budget. `handoff` gets no note.

## 8. Title prefixes

- **Record.** `<gcd>/kankan/title-prefixes.json`: card ID → the prefix kankan wrote (`"🟢"`, `"⚪"`, or any configured value). Only the router writes titles and the record.
- **Desired prefix**, per card at each round: warm while a seat of the pool holds the card and is warm (or answering); cold while it holds it and is cold, or once the card's conversation ended by a switch or a closing and the card is still in `Q&A`; none when the card left `Q&A`, is archived, or was never held. A card that does not need a change is not touched.
- **Write.** The router reads the card live through the board reader, computes the title without the recorded prefix (if the title no longer starts with the recorded prefix and a space, the user edited it: the record is dropped and that title is the base), and writes `desired + " " + base`, or `base`, with `TrelloClient.set_card_name` (C11). The record is updated after the write is confirmed; a crash in between leaves a prefix kankan does not know, which it then treats as the user's text.
- **Start and stop.** When the router starts, it removes every recorded prefix ([§9](#9-stop-start-and-type-change)). A graceful stop of the router writes the cold prefix on every card it recorded warm before exiting; an immediate stop does not, and the next start removes them.
- **Plain titles.** `qapool.plain_title(paths, card)` removes a recorded prefix; `agentcmds.task` (`card.title`), `integrate` (the merge commit title), `diag` (status and claims) and every comment kankan writes with a card title use it.

## 9. Stop, start and type change

**Router start** (launcher start of `<type>-qa`, also after a reboot, under the Q&A lifecycle lock): (1) completes the pending answer or handoff of every conversation directory (`qa_answer.complete_pending` without a signal); (2) ends every `qa` ref of every pool seat with `qa.end_admin` (the cards stay in `Q&A`; the conversation directories go as today); (3) removes the members left registered (`seatremove.complete`); (4) removes the recorded prefixes; (5) runs its first routing round. A launcher restart of the router by its supervisor while members are alive (their seat liveness is `alive`) skips steps 2 and 3 for those members: they keep their cards.

**Stop.** `stopall` and `seat stop <type>-qa` treat every seat of the pool as a seat to stop (members included), gracefully or with `seat recover --kill`; members are removed as in [§6](#6-opening-and-closing). The stop record keeps `qa=<type>`, so `kankan start` starts only the router.

**Type change.** `kankan up qa=<other>` (and `agent <other> --qa`) with a pool of another type: under the lifecycle lock, kills every seat of the pool (`seat recover --kill`), removes them all (`seatremove.complete`), registers `<other>-qa` and starts it; its router start completes the pending answers. `qa.replace_seat`, the replacement record `qa-replacing.json` and the conversation takeover are removed; `reconcile` keeps reporting a `qa` ref whose owner is not registered (`qa_owner_unregistered`), which the next router start ends.

**Seat commands.** `cli` refuses `seat start`, `seat stop` and `seat remove` on a member (`usage_error`, naming the router); on the router, `seat stop` stops the pool, `seat start` starts the router, `seat remove` removes the members and then the router.

## 10. Trello usage

One revision of [`kanban_requirements.md`](kanban_requirements.md), made in the implementation commit:
- **C11 (SHOULD):** update a card's title. Used for the Q&A title prefix. There is no conditional update: a title edit made by the user in the same moment can be overwritten, or overwrite the prefix (G8). Renaming a card does not notify its watchers (checked by the user on 2026-11-25). `PUT /1/cards/{id}` (`name`).

The routing round adds no request of its own beyond the board reader's shared reads, plus one live thread read per card seeded into the answer record and one live card read and one write per prefix change.

## 11. Status

`diag.status` lists the pool: per seat, its card (shortLink and plain title), launcher state, and cache (`warm until HH:MM`, `exact` or `estimated`, or `cold`); and the cards needing an agent with their wait key. `--json` gives the same fields.

## 12. Tests

- **Unit:** the seat pattern; `expiry` (exact reading of the current process, a reading of a previous process ignored, estimated from `turn_ended_at`, cold without a turn); the choice of [§5](#5-routing-round) step 4 with every combination of warm, cold, busy, ended and limited seats; wait-key order; closing only after a search; the answer record and its seeding (complete and cut-short answers); prefix computation (record, user edit, several characters, removal); the note's wording, time and zone, and its place in the chunk budget; `kankan statusline` (reading written, the user's status line chained from each settings file, no seat, a failing user command).
- **Integration** (private tmux server, fake agents in Q&A mode, fake Trello): the acceptance tests of [functional §8](qapool_spec_functional.md#8-acceptance-tests). The fake agent writes cache readings through a fake status line; tests set lifetimes in seconds to make agents go cold.
- **Crash points:** between registering a member and starting it; between writing an offer and taking it; between ending the old conversation and claiming the offered card; between a prefix write and its record update.
- **Live agents run** ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents)): two Q&A cards answered by two Claude agents, the exact note and the warm and cold prefixes, a closing after expiry with a short lifetime; with Codex, the estimated note.

## 13. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md), by its section numbers. That file is not edited.

**§5.2 Local state.** Add:

| Q&A cache readings | `<gcd>/kankan/seats/<seat>.cache.json` ([§3](#3-prompt-cache-readings)) |
| Q&A answer record | `<gcd>/kankan/qa-answered.json` ([§5](#5-routing-round)) |
| Q&A title prefixes | `<gcd>/kankan/title-prefixes.json` ([§8](#8-title-prefixes)) |
| Q&A pool lock | `<gcd>/kankan/qa-pool.lock` ([§4](#4-roles-and-offers)) |

**§5.3 Components.** Add:

| `qapool` | Q&A pool: cache expiry, routing, offers, opening and closing agents, answer record, title prefixes. |

**§8.7 Adapters.** Add:

> `prompt_cache` gives how the agent type's cache expiry is known (`exact` with a reporter, or `estimated`) and the lifetime used for estimates ([§2](#2-seats-and-configuration)).

**§10.1 Primitives (`refstore`).** The seat pattern becomes `^[a-z]+-([0-9]+|qa([2-9]|[1-9][0-9]+)?)$`.

**§16.3 Internal commands.** Add:

> `kankan statusline`: Claude Code's status line command for Q&A agents; records the cache expiry and shows the user's own status line ([§3](#3-prompt-cache-readings)).

## 14. Changes to other technical specifications

**Questions** ([`questions_spec_technical.md`](questions_spec_technical.md)):
- **§2 Configuration and seat.** Several `qa` seats of one type are registered (the pool); “Replacement” is replaced by the type change of [§9](#9-stop-start-and-type-change).
- **§3 Card ref phase `qa`.** `owner` is any seat of the pool; a new end: the seat took another card or closed (`qa.end`, card stays in `Q&A`).
- **§4 Launcher.** The idle claim of the top card is replaced by offers ([§4](#4-roles-and-offers)) and the router's routing round ([§5](#5-routing-round)); “Restart” applies only to a router restart with alive members ([§9](#9-stop-start-and-type-change)); the launcher records `turn_ended_at`.
- **§6 Agent commands.** `answer` appends the expiry note and updates the answer record.
- **§8 Trello usage.** C11 is added ([§10](#10-trello-usage)).

**Seat commands** ([`seat_spec_technical.md`](seat_spec_technical.md)): §2 CLI refuses members; §5 graceful removal also closes members ([§6](#6-opening-and-closing)); §8 (Q&A first turn with nothing new) applies to every pool seat taking a card it does not hold.

**Stop and start all** ([`stopall_spec_technical.md`](stopall_spec_technical.md)): the inventory and the stop include the members; the record keeps `qa=<type>`.

## 15. Accepted limitations

- An immediate stop leaves warm prefixes on the cards until the next router start removes them.
- A crash between a prefix write and its record leaves a prefix kankan treats as the user's text.
- A comment posted while an answer was being written, followed by a stop and start, counts as answered ([functional §4.2](qapool_spec_functional.md#42-a-card-that-needs-an-agent)).
- An estimated expiry can be wrong in both directions: a card may go to a new agent although the old cache was still warm, or stay with an agent whose cache already expired.
