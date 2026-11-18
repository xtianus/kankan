# kankan questions (Q&A) — technical specification

**Status:** implemented locally; the live agents run with the Q&A dialogue ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents)) remains outstanding.
**Date:** 2026-11-15
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) and [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md); this feature changes them as listed in [§10](#10-changes-to-the-initial-technical-specification) and [§11](#11-changes-to-the-board-settings-technical-specification).
**Companion:** [`questions_spec_functional.md`](questions_spec_functional.md) — what users and agents observe.

**Split rule.** This file describes how kankan implements the feature; the functional file describes what users and agents observe. Section numbers are this feature's own.

## 1. Scope

The Q&A seat, its card ref phase, the launcher's conversation loop, the delivery of follow-ups, the agent commands `answer` and `handoff`, the handoff data given to implementers, and the Trello calls they need ([functional §1](questions_spec_functional.md#1-scope)).

## 2. Configuration and seat

**Configuration.** `config.json` gains `qa_list` (list ID, found or created by `init`, restored by `doctor`) and `qa_poll_seconds` (default 30). A missing `qa_list` in an existing configuration is not an error: the Q&A seat refuses to start until `doctor` or `init` creates it, and every other command works as before. Seats are registered in `seats` as today; `SeatConfig` gains `role` (`work`, the default when absent, or `qa`), and a `qa` seat has no list IDs. `Config.seat_lists()` and the list checks skip `qa` seats; `workflow_lists()` includes `qa_list`, but a missing or archived `qa_list` is not a `config_error` (only the Q&A launcher refuses to run without it).

**Seat name.** `<type>-qa`. `refstore`'s seat pattern becomes `^[a-z]+-([0-9]+|qa)$`, so `seats.seat_type()` (the part before the last `-`) keeps giving the type, and the agent commands, the lease and the launcher work unchanged. The places that parse the seat number skip `qa` seats: the free-seat choice and the next-number computation in `seats`, the per-type cap, the seat sort in `diag`; `reconcile`'s "seats without their lists" skips them too. At most one `qa` seat is registered.

**Replacement.** `agents_cmd` starting a Q&A seat of another type, under the project lock: refuses if the registered `qa` seat is alive; otherwise registers the new seat, rewrites the `qa` card ref (if any) with the new owner in a transaction that verifies the old ref value and the old seat ref, removes the old seat (its seat ref, its worktree if clean, its configuration entry) and moves the conversation directory ([§5](#5-conversation-record-and-delivery)) unchanged, since it is keyed by conversation and not by seat; the old seat's label causes for the moved card go to the new seat, so a follow-up still clears the old seat's `fail` `Error`. Replacements are serialized by a lifecycle lock (`<gcd>/kankan/qa-lifecycle.lock`), also taken by `seat remove <type>-qa`; the lock order is `up.lock` → lifecycle lock → conversation lock → project lock → seat-state lock (`kankan up` registers the Q&A seat while holding `up.lock`); every stage re-checks that its destination is still the registered Q&A seat. The stages are completed by any later replacement or Q&A seat start, also when no Q&A seat is registered any more (the starting seat registers itself first); the old seat's files count as leftovers only while a replacement record (`<gcd>/kankan/qa-replacing.json`) names it, so a plain `seat remove` is never mistaken for a replacement; the record is removed once the replacement it names can no longer complete (its new seat was removed, or another seat is registered or starting). `reconcile` reports a `qa` ref whose owner is not a registered seat (`qa_owner_unregistered`): starting a Q&A seat takes the conversation over.

**Worktree.** `~/coding/.kankan/<project>/<type>-qa`, created like a seat worktree. At every claim, under the seat's ownership, kankan runs `git reset --hard`, `git clean -ffdx` (also nested repositories; the seat file lives in the worktree's private Git directory, outside the tree) and `git checkout --detach <integration tip>`, and records the base commit and the length of the worktree's `HEAD` reflog. The worktree never holds user work, so the reset is safe.

**Q&A record.** `<gcd>/kankan/qa.json` lists the IDs of cards that have been Q&A cards. An entry is added under the project lock before the claim's Git transaction (an entry for a claim that then fails is harmless); every implementation claim (none/`parked` → `impl`, [initial §10.3](initial_spec_technical.md#103-phases-and-transitions)) removes the card's entry under the same lock, right after its transaction. `reconcile` exempts the listed cards from `done_without_merge`; a crash between an implementation claim's transaction and the removal leaves an entry that can only hide that report for that card, and `reconcile --fix` removes the entries of cards that hold an `impl`, `implemented`, `review` or `integrated` ref, or that are archived or no longer exist (an archived card is never reported as `done_without_merge`). The cards missing from the board snapshot are read before the project lock; the removals are applied under it, only for entries whose card ref did not change meanwhile.

## 3. Card ref phase `qa`

Blob fields as in [initial §10.2](initial_spec_technical.md#102-metadata-blobs), with `phase: "qa"`, `owner: <type>-qa`, `target_list: qa_list`, no branch, and:
- `conversation`: a random ID created at claim and kept by every `qa` → `qa` rewrite, the key of the conversation directory and of the comment markers;
- `covered`: the inputs covered by the latest completed answer (the ID of the newest comment delivered in that turn and the IDs of the attachments delivered up to that turn), absent before the first answer.

Transitions, under the project lock with the Git transaction first ([initial §10.3](initial_spec_technical.md#103-phases-and-transitions)); each lock hold is bounded: at most one card read, the Git transaction, and one move with its verification read.

| Transition | Phase | Holder | Trello |
|---|---|---|---|
| Q&A claim | none/`parked` → `qa` | Q&A seat | no move (card verified in `Q&A`, open, no `HOLD`) |
| Answer completed | `qa` → `qa` (`covered` updated) | Q&A seat | none |
| Seat replacement | `qa` → `qa` (new `owner`) | new Q&A seat | none |
| `handoff` | `qa` → `parked` (`cause: handoff`, `target_list: Backlog`, `pos: bottom`, `handoff` comment IDs) | none | move to the bottom of `Backlog`, verified |
| End: card left `Q&A`, archived, deleted, or `HOLD`, `yield` | `qa` → ref deleted | — | none |

The claim from `parked` keeps any `branch` field out of the new blob; the card branch stays as it was, and a later implementation claim finds it through `??kankan-<shortLink>` ([initial §11](initial_spec_functional.md#11-branches-and-worktrees)). Convergence ([initial §10.4](initial_spec_technical.md#104-convergence)) never moves a card in phase `qa`: when the card is not in `qa_list`, closed or gone, the Q&A launcher ends the conversation instead; other launchers ignore `qa` refs. `parked` with `pos: bottom` is converged with `pos=bottom`, except that a handoff whose move is still outstanding (`from_list` is `qa_list`) is cancelled when the card, still in `Q&A`, has `HOLD`: convergence drops `from_list` and `pos` without moving, with `target_list` set to `qa_list`, so the card stays in `Q&A` and is taken again as a new conversation once `HOLD` is removed; a parked ref in `Backlog` is already eligible for implementation ([initial §9.2](initial_spec_functional.md#92-eligibility)).

Every end deletes the ref with a transaction that verifies the seat's ref, so a command of a turn still running afterwards gets `not_owner`. The attachment directory of the card and the conversation directory are deleted at the end unless the transition was `handoff`, with the per-card attachment lock rules of [initial §13.3](initial_spec_functional.md#133-card-attachments); after a handoff the conversation directory is deleted when the card's ref is deleted in `Done`.

## 4. Launcher

**Task identity and turns.** A conversation is one task: the task ID is generated at claim (and again at every launcher start) and stays until the end. A turn starts with each prompt written to the agent (the first prompt, a delivery, a restart prompt, or a user typing in the pane) and gets a random **turn token**, saved in the seat state before the prompt is written and included in it. `answer` and `handoff` must carry the current token; `fail` may carry it (`--turn`, refused with `needs_action` when it is not current) and otherwise, like a missing signal, refers to the current turn. `answer` and `fail` end the turn and not the task; `handoff` and `yield` end the task. The "stopped without a signal" check applies per turn, so an earlier answer never covers a later turn.

**States.** The Q&A launcher uses the states of [initial §8.4](initial_spec_technical.md#84-state-machine) plus `qa_waiting`: after `answer`, `fail`, or a "stopped without a signal" `Error`, with no inactivity check and follow-up polling. In `idle` it reads the board every `qa_poll_seconds` and claims the top eligible `Q&A` card; in `working` the usual inactivity check applies per turn; `closing` after an end waits for the pane to be "ready", sends the adapter's new-conversation command and returns to `idle`. `limited` keeps the conversation; deliveries wait for resumption. A stop request is honoured in `qa_waiting` and `idle` without ending the conversation.

**Polling the active card.** In `working` and `qa_waiting`, every `qa_poll_seconds`, without the project lock: one card read (C1, with the attachment fields of [§8](#8-trello-usage), labels, list and `closed`) and the comment read from the cursor (M2, one request unless more than 50 comments arrived). In `qa_waiting` each poll also reads the `Q&A` list (`list_info`), so an archived or deleted list stops the seat ([§2](#2-configuration-and-seat)). A card no longer in `qa_list`, closed, deleted (404) or with `HOLD` ends the conversation ([§3](#3-card-ref-phase-qa)); in `working` the end is recorded at once (ref deleted, so later commands get `not_owner`) and the clear waits for "ready". The suspension request for `HOLD` asks the agent to `yield` without the "commit WIP" text.

**Restart.** A launcher starting on the Q&A seat with a ref in phase `qa` takes it over with a new task ID; a pending answer or handoff operation ([§6](#6-agent-commands)) is completed first. A restart prompt asking the agent to read the thread with `kankan task --json` and continue is written only when the conversation has uncovered inputs or no completed answer yet (no `covered`); its turn's inputs are the uncovered candidates, or, when neither `turn.json` nor `covered` exists (a crash between the claim and the first turn), the inputs and context of a first turn ([§5](#5-conversation-record-and-delivery)). Otherwise the launcher goes to `qa_waiting` without a prompt and without a comment, and the next delivery uses restart wording: it names the new items and asks the fresh agent to read the whole thread with `kankan task --json`. A model or effort change ([§11](#11-changes-to-the-board-settings-technical-specification)) restarts the agent only in `qa_waiting` or `idle`.

**A turn ending without a signal.** When the inactivity check finds a turn stopped without a signal, and before a delivery or a typed turn starts a new turn, the launcher tries the conversation lock without waiting and completes the current turn's pending operation ([§6](#6-agent-commands)) as a restart does: a completed `answer` is the turn's answer (no `Error`), a completed `handoff` ends the conversation; a busy lock means the command is still posting, and the launcher checks again later.

## 5. Conversation record and delivery

**Conversation directory.** `<gcd>/kankan/qa/<conversation>/` holds `turn.json` (the current turn's token and the IDs of the comments and attachments delivered in it) and the pending operation of [§6](#6-agent-commands). It is keyed by the conversation ID of the ref, so it survives launcher restarts and seat replacement. Files are written atomically.

**What is new.** An item (comment or attachment) is a candidate when it is by a board member other than kankan's account (`notify.input_members`). The inputs of a turn are the candidates not covered by the ref's `covered` and not delivered in an earlier turn of this launcher run: in practice, everything since the last completed answer, including what arrived while that answer was being written. The first turn of a new conversation delivers the description and every item on the card; when the card was answered in an earlier conversation, the candidates dated after the latest comment carrying kankan's answer marker are marked `new` (Trello's dates, never the local clock), the others are context.

**Delivery.** In `qa_waiting`, the candidates found by a poll are delivered at once: a new turn token and the turn's input IDs are saved in `turn.json` **before** the prompt is written through the pane queue ([initial §8.6](initial_spec_technical.md#86-pane-observation-and-writes)), e.g. "New on the Q&A card: 2 comments, 1 attachment (`screenshot.png`) — turn `<token>`: run `kankan task --json` and reply with `kankan answer --turn <token>`". A crash between saving and writing loses the prompt but not the inputs: they stay uncovered and the restart prompt delivers them. The comment texts are not pasted: the agent reads them from `task`, which keeps quoting out of the pane. Candidates found in `working` wait for the end of the turn. After a delivery kankan comments "forwarded to the agent" and removes the `Error` label whose cause is the conversation's `fail` or inactivity. `fail` and the inactivity `Error` do not move any cursor: the inputs of the failed turn stay uncovered and are delivered again with the follow-up. A user typing in the pane while in `qa_waiting` is detected as in [initial §8.5](initial_spec_technical.md#85-cycle-and-signals) and starts a turn whose inputs are the current candidates.

**`task --json`.** `agentcmds.task` for a `qa` task reads the card, its attachments (`attachments.for_task`, which downloads them), the board members and the whole comment thread (M2, paginated, within `task`'s time budget), and sets `new` from `turn.json`, so repeated calls in a turn agree. It then reads the card once more and checks the ref and the seat task: a conversation that ended meanwhile (card left `Q&A`, archived, deleted or on `HOLD`, ref gone or another's) gives `not_owner`, and the downloaded attachment directory is deleted only when no newer task can use it: under the project lock, tried without waiting, the ref is gone or is still this task's conversation with the same seat task (a newer conversation or task, also of the same seat, keeps its files). `role` is `agent` for comments carrying kankan's answer or handoff marker, `kankan` for kankan's other comments, `user` for board members. For implementation tasks `task` also reads the thread and fills `comments` and `handoff` (the text of the comments listed in the parked ref's `handoff` field when the claim came from a handoff, otherwise the latest comment carrying the handoff marker, otherwise `null`).

## 6. Agent commands

**`answer`.** Steps (a crash or a failure part-way is completed by the launcher, [§4](#4-launcher)):
1. Without the project lock: validates the task (`qa`, current task and turn token, else `needs_action` or `usage_error`), the clean worktree and `HEAD` equal to the base commit (`needs_action` with the restore command otherwise), reads the text (argument or `--file`, UTF-8) and splits it.
2. Writes the **pending operation** to the conversation directory: kind, turn token, the turn's input IDs, the text's hash, the chunk plan and the chunks already posted. An existing pending operation for the same turn with the same hash is resumed; with another hash it is refused with `needs_action` (the previous answer is still being posted). A pending operation of an older turn is completed first (its remaining chunks and its step 4, without a signal, since that turn is over), never dropped.
3. For each chunk, without the project lock: reads the card (C1) and stops with `not_owner` if it left `Q&A`, is closed, has `HOLD` or the ref changed; posts the chunk with `comment_once` and the marker `kankan:qa-<conversation>-<turn>-<i>`, so a retry after a crash finds and skips the chunks already posted (M2); records it in the pending operation.
4. Under the project lock: the transaction `qa` → `qa` with the new `covered`, then the signal in the seat state (only while the operation's turn is still the current one), then deletes the pending operation.

Chunking: chunks of at most 16 384 characters including the prefix `n/m`, the marker and, on the last chunk, the mention; split at the last blank line before the limit, else at the last newline, else hard. The commit check: when the worktree's `HEAD` reflog has a commit-creating entry (commit, merge, cherry-pick, revert, rebase, am, pull) since the claim, the answer is posted and the card gets `Error` with the commit warning (label cause `qa_commit`; after a `handoff` the warning says the summary was posted); other entries, such as a checkout, are not reported.

**`handoff`.** Same steps 1–3 with the handoff marker; step 4 is the transition `qa` → `parked` (recording the summary's comment IDs) and the verified move to the bottom of `Backlog`, then the `qa.json` entry stays (removed by the implementation claim). A crash after step 3 is completed by the launcher's restart from the pending operation; a crash after the transition is completed by convergence.

**Other commands.** `done`, `ask`, `sync`, `verify`, `integrate`, `block` refuse a `qa` task with `usage_error`; `answer`/`handoff` refuse a non-`qa` task with `usage_error`. `fail [--turn <t>] "<reason>"` posts its comment as usual but does not move the comment cursor of the conversation ([§5](#5-conversation-record-and-delivery)); with `--turn`, a token that is not the current one gives `needs_action` and nothing is posted, and without it `fail` refers to the current turn (its op-id and signal carry the current token). `fail` validates the task, the ref and the token, reads the card, labels, comments and signals under the conversation lock, so a turn cannot start meanwhile and a delayed `fail` never overwrites a newer turn's signal. `fail` and `note` read the card first and give `not_owner` when it left `Q&A`, is archived, deleted or on `HOLD`; `note` works as usual otherwise; `yield` ends the conversation.

## 7. Implementation tasks of handed-off cards

The implementation claim of a card whose `parked` ref has `cause: handoff` copies the summary's comment IDs into the `impl` ref, so `task` returns the same `handoff` after a restart. The skill tells the implementer that the summary prevails over the description.

## 8. Trello usage

The feature uses existing capabilities with four revisions of [`kanban_requirements.md`](kanban_requirements.md), made in the implementation commit as required by the project rule:

- **M1:** comments have a maximum length (Trello: 16 384 characters); kankan splits longer texts.
- **C1:** the single-card read returns the attachments with ID, name, file name, URL, size, MIME type, uploader, upload flag and creation date (`attachment_fields=id,name,fileName,url,bytes,mimeType,idMember,isUpload,date`), for follow-up polling.
- **F2:** the attachment list includes the creation date (`date`).
- **C3:** a card can also be moved to the **bottom** of a list (`pos=bottom`), for `handoff`.
- **L1:** `init` and `doctor` create the `Q&A` list at a numeric `pos` halfway between `Blocked` and the next open list (from the B3 read), or at `bottom` when `Blocked` is the last open list or missing from the read.

M2 (comment reads) is also used for implementation tasks, to give them the card's thread. The B3 board read is unchanged. The Q&A seat adds, per `qa_poll_seconds`, one board read while idle, or at least two requests (C1 and M2) during a conversation, three in `qa_waiting` (with the `Q&A` list read of [§4](#4-launcher)).

## 9. Tests

The fake Trello server gains attachment `date` and the full C1 attachment fields, `pos=bottom` on moves, and comment-length enforcement (400 beyond 16 384 characters). The fake agent gains a scripted Q&A mode (answer, answer after a follow-up, handoff, fail, stop without a signal, a forbidden commit). Unit tests cover chunking, the pending operation and its resumption, the computation of `new` from `covered` and `turn.json`, the seat-name pattern and the seat-number parsers skipping `qa` seats, configuration without `qa_list` and seats without `role`. Integration tests with a private tmux server cover the acceptance tests of [functional §8](questions_spec_functional.md#8-acceptance-tests); the crash points are after each answer chunk, after the last chunk before the `covered` transaction, after the handoff summary, after the `handoff` transition, between saving `turn.json` and writing to the pane, and during seat replacement; the independence test delays Trello responses for one seat at a time. A live Trello test checks the comment length limit, `pos=bottom` and the attachment fields of C1; the live agents run ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents)) gains a Q&A dialogue with a follow-up attachment and a handoff implemented by another seat.

## 10. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md), by its section numbers. That file is not edited. Where the board settings feature already replaced the same text, the replacement below includes its additions.

**§5.2 Local state.** Add:

| Q&A record | `<gcd>/kankan/qa.json` ([§2](#2-configuration-and-seat)) |
| Q&A conversations | `<gcd>/kankan/qa/<conversation>/` ([§5](#5-conversation-record-and-delivery)) |
| Q&A seat worktree | `~/coding/.kankan/<project>/<type>-qa` |

**§5.2 Local state.** Replace the text starting “| Seat state | `<gcd>/kankan/seats/<seat>.state.json` (current…” with:

| Seat state | `<gcd>/kankan/seats/<seat>.state.json` (current task, signals, label causes, comment cursor, test verifications, stop request, model and effort of the running agent, planned agent restart; for the Q&A seat the current turn token) |

**§5.3 Components.** Add:

| `qa` | Q&A seat policy: claim, conversation end, conversation record, delivery, answer chunking and pending operations, handoff. |

**§8.4 State machine.** Add:

| `qa_waiting` | Q&A seat after `answer`, `fail` or an inactivity `Error`: follow-up polling ([§4](#4-launcher)) | no |

**§8.5 Cycle and signals.** Add:

> On the Q&A seat a task is a conversation: `answer` and `fail` end a turn and not the task; follow-ups start new turns, each with its own token, in the same agent conversation ([§4](#4-launcher)).

**§10.1 Primitives (`refstore`).** Replace the text starting “Inputs validated before reaching Git: …” with:

> Inputs validated before reaching Git: card ID `^[0-9a-f]{24}$`, shortLink `^[A-Za-z0-9]{8}$`, seat `^[a-z]+-([0-9]+|qa)$`.

**§10.2 Metadata blobs.** Add:

> Phase `qa` has `conversation` and `covered`; a `parked` ref with `cause: handoff` and the `impl` ref claimed from it have `handoff` (comment IDs); a `parked` ref may have `pos: bottom` ([§3](#3-card-ref-phase-qa)).

**§10.3 Phases and transitions.** Add the rows of [§3](#3-card-ref-phase-qa).

**§10.4 Convergence.** Add:

- cards in phase `qa` are never moved; `parked` refs with `pos: bottom` are moved to the bottom of the target list.

**§18.1 Strategy.** Add the tests of [§9](#9-tests).

## 11. Changes to the board settings technical specification

How this feature changes [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md), by its section numbers. That file is not edited.

**§3 Model and effort.** Add:

> A third role, `questions`, with its setting card titled `Q&A Agents`, seeds and probes, as `implementation` and `review`, active only when a `qa` seat is configured: `seeds`, `boardconfig` and the adapter validation accept the role, `init`/`doctor` ask its seeds and create its card only then, its probes run only on the Q&A seat; the `Q&A Agents` card is always part of the evaluated settings, so it never becomes an unknown setting when the Q&A seat is removed; and its absence or invalidity never affects the evaluation of the other roles or the start of `work` seats. The Q&A seat pins the `questions` pair at claim and at every start; the planned restart for a changed pair happens only in `idle` or `qa_waiting`, never during a turn, and there is no fallback to another role's pair.
