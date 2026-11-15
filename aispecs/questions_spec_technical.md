# kankan questions (Q&A) — technical specification

**Status:** draft, not implemented.
**Date:** 2026-11-15
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) and [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md); this feature changes them as listed in [§9](#9-changes-to-the-initial-technical-specification) and [§10](#10-changes-to-the-board-settings-technical-specification).
**Companion:** [`questions_spec_functional.md`](questions_spec_functional.md) — what users and agents observe.

**Split rule.** This file describes how kankan implements the feature; the functional file describes what users and agents observe. Section numbers are this feature's own.

## 1. Scope

The Q&A seat, its card ref phase, the launcher's conversation loop, the delivery of follow-ups, the agent commands `answer` and `handoff`, and the Trello calls they need ([functional §1](questions_spec_functional.md#1-scope)).

## 2. Configuration and seat

**Configuration.** `config.json` gains `qa_list` (list ID, found or created by `init`, restored by `doctor`) and `qa_poll_seconds` (default 30). The Q&A seat is registered in `seats` like the others, with no list IDs; `Config.seat_lists()` skips it, `workflow_lists()` includes `qa_list` but a missing or archived `qa_list` is not a `config_error` (only the Q&A launcher refuses to run without it).

**Seat name.** `<type>-qa`. `refstore`'s seat pattern becomes `^[a-z]+-([0-9]+|qa)$`, so `seats.seat_type()` (the part before the last `-`) keeps giving the type, and the agent commands, the lease and the launcher work unchanged. The places that parse the seat number skip Q&A seats: the free-seat choice and the next-number computation in `seats`, the per-type cap, the seat sort in `diag`; `reconcile`'s "seats without their lists" skips them too. At most one `*-qa` seat is registered; `agents_cmd` replaces a dead one of another type (`seat remove` semantics, plus the end of its conversation, [§3](#3-card-ref-phase-qa)) and refuses when it is alive.

**Worktree.** `~/coding/.kankan/<project>/<type>-qa`, created like a seat worktree. At every claim, under the seat's ownership, kankan runs `git reset --hard`, `git clean -fdx` (the seat file lives in the worktree's private Git directory, outside the tree) and `git checkout --detach <integration tip>`. The worktree never holds user work, so the reset is safe.

**Q&A record.** `<gcd>/kankan/qa.json` lists the card IDs that have been Q&A cards and were not handed off, written atomically at claim and updated at handoff. `reconcile` exempts them from `done_without_merge`; `reconcile --fix` removes the entries of deleted cards.

## 3. Card ref phase `qa`

Blob fields as in [initial §10.2](initial_spec_technical.md#102-metadata-blobs), with `phase: "qa"`, `owner: <type>-qa`, `target_list: qa_list`, no branch. Transitions, each under the project lock with the Git transaction first ([initial §10.3](initial_spec_technical.md#103-phases-and-transitions)):

| Transition | Phase | Holder | Trello |
|---|---|---|---|
| Q&A claim | none/`parked` → `qa` | Q&A seat | no move (card verified in `Q&A`, open, no `HOLD`) |
| `handoff` | `qa` → `parked` (`cause: handoff`, `target_list: Backlog`, `pos: bottom`) | none | move to the bottom of `Backlog`, verified |
| End: card left `Q&A`, archived, deleted, or `HOLD`, `yield` | `qa` → ref deleted | — | none |

The claim from `parked` keeps any `branch` field out of the new blob; the card branch stays as it was, and a later implementation claim finds it through `??kankan-<shortLink>` ([initial §11](initial_spec_functional.md#11-branches-and-worktrees)). Convergence ([initial §10.4](initial_spec_technical.md#104-convergence)) never moves a card in phase `qa`: when the card is not in `qa_list`, closed or gone, the Q&A launcher ends the conversation instead; other launchers ignore `qa` refs. `parked` with `pos: bottom` is converged with `pos=bottom`; a parked ref in `Backlog` is already eligible for implementation ([initial §9.2](initial_spec_functional.md#92-eligibility)).

Every end deletes the ref with a transaction that verifies the seat's ref, so a command of a turn still running afterwards gets `not_owner`. The attachment directory of the card is deleted at the end unless the transition was `handoff`, with the per-card attachment lock rules of [initial §13.3](initial_spec_functional.md#133-card-attachments).

## 4. Launcher

**Task identity and turns.** A conversation is one task: the task ID is generated at claim and stays until the end. The seat state's `qa` record holds the card, a turn counter, the delivery cursors ([§5](#5-delivery-of-follow-ups)) and the turn's outcome. `answer` and `fail` are non-terminal signals carrying the task ID and the turn number; `handoff` and `yield` are terminal. A signal with an old turn number is ignored for the "stopped without a signal" check, so an earlier answer never covers a later turn.

**States.** The Q&A launcher uses the states of [initial §8.4](initial_spec_technical.md#84-state-machine) plus `qa_waiting` (after `answer` or `fail`: no inactivity check, follow-up polling). In `idle` it reads the board every `qa_poll_seconds` and claims the top eligible `Q&A` card; in `working` the usual inactivity check applies per turn; `closing` after an end waits for the pane to be "ready", sends the adapter's new-conversation command and returns to `idle`.

**Polling the active card.** In `working` and `qa_waiting`, every `qa_poll_seconds`: one card read (C1 with attachments, labels, list and `closed`) and one comment read from the cursor (M2). A card no longer in `qa_list`, closed, deleted (404) or with `HOLD` ends the conversation ([§3](#3-card-ref-phase-qa)); in `working` the end is recorded at once and the clear waits for "ready". Both reads together are two requests every 30 seconds for the whole project.

**Restart.** A launcher starting on the Q&A seat with a ref in phase `qa` resumes it as a new task ID with the turn counter reset; the first prompt asks the agent to read the thread with `kankan task --json` and continue. A model or effort change ([§10](#10-changes-to-the-board-settings-technical-specification)) restarts the agent only in `qa_waiting` or `idle`.

## 5. Delivery of follow-ups

**What is new.** An item (comment or attachment) is new when it is by a board member other than kankan's account (`notify.input_members`) and has not been delivered in an earlier turn. The seat state records, per conversation, the IDs of the attachments and the cursor of the last comment delivered; both are saved **before** the delivery is written to the pane (a crash can lose a follow-up, never deliver it twice, as in [initial §14.3](initial_spec_functional.md#143-answers-through-comments)). The first turn delivers the description and every item already on the card. On a restart, when the seat state has no record for the active card, kankan rebuilds it from the thread: the items dated up to the latest comment with kankan's answer marker count as delivered, the later ones are new; dates are Trello's (comment `date`, attachment `date`), never the local clock.

**Delivery.** In `qa_waiting`, the new items found by a poll are delivered at once: the turn counter is incremented, the cursors are saved, and one prompt is written through the pane queue ([initial §8.6](initial_spec_technical.md#86-pane-observation-and-writes)), e.g. "New on the Q&A card: 2 comments, 1 attachment (`screenshot.png`) — run `kankan task --json` and reply with `kankan answer`". The comment texts are not pasted: the agent reads them from `task`, which keeps quoting out of the pane. Items found in `working` wait for the end of the turn. After a delivery kankan comments "forwarded to the agent"; after a delivery that follows `fail`, it removes the `Error` label whose cause is that `fail`. A user typing in the pane while in `qa_waiting` is detected as in [initial §8.5](initial_spec_technical.md#85-cycle-and-signals) and starts a turn without a delivery.

**`task --json`.** `agentcmds.task` for a `qa` task reads the card, its attachments (`attachments.for_task`, which downloads them), the board members and the whole comment thread (M2, paginated), and marks `new` from the seat state's record of the current turn. `role` is `agent` for comments carrying kankan's answer or handoff marker, `kankan` for kankan's other comments, `user` for board members.

## 6. Agent commands

**`answer`.** Validates the task (`qa`, current task ID and turn), the clean worktree and `HEAD` equal to the commit recorded at claim (`needs_action` otherwise), reads the text (argument or `--file`, UTF-8), and under the project lock re-checks the ref (`not_owner` if the conversation ended). It splits the text into chunks of at most 16 384 characters including the prefix `n/m`, the anti-duplicate marker and, on the last chunk, the mention: split at the last blank line before the limit, else at the last newline, else hard. Each chunk is posted with its own `kankan:<op-id>` marker derived from the task ID, turn and chunk index, so a retry finds and skips the chunks already posted (M2). The signal is recorded after the last chunk is confirmed.

**`handoff`.** Same checks; posts the summary (split like an answer) with the handoff marker and the mention, then the transition `qa` → `parked` and the verified move to the bottom of `Backlog`, then updates `qa.json`.

**Other commands.** `done`, `ask`, `sync`, `verify`, `integrate`, `block` refuse a `qa` task with `usage_error`; `answer`/`handoff` refuse a non-`qa` task with `usage_error`. `fail` and `note` work as usual; `yield` ends the conversation.

## 7. Trello usage

The feature uses existing capabilities with three revisions of [`kanban_requirements.md`](kanban_requirements.md), made in the implementation commit as required by the project rule:

- **M1:** comments have a maximum length (Trello: 16 384 characters); kankan splits longer texts.
- **F2:** attachments carry their creation date (`date` in the requested fields), used to rebuild the delivery record ([§5](#5-delivery-of-follow-ups)).
- **C3:** a card can also be moved to the **bottom** of a list (`pos=bottom`), for `handoff`.

The B3 board read is unchanged; the polling of the active card uses C1 and M2.

## 8. Tests

The fake Trello server gains attachment `date` fields, `pos=bottom` on moves, and comment-length enforcement (400 beyond 16 384 characters). The fake agent gains a scripted Q&A mode (answer, answer after a follow-up, handoff, fail, stop without a signal). Unit tests cover chunking, the delivery record and its reconstruction from a thread, the seat-name pattern and the seat-number parsers skipping `*-qa`. Integration tests with a private tmux server cover the acceptance tests of [functional §8](questions_spec_functional.md#8-acceptance-tests); the crash points are after the `handoff` transition, after each answer chunk, and between saving the delivery cursor and writing to the pane. A live Trello test checks the comment length limit, `pos=bottom` and attachment `date`; the live agents run ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents)) gains a Q&A dialogue with a follow-up attachment and a handoff.

## 9. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md), by its section numbers. That file is not edited.

**§5.2 Local state.** Add:

| Q&A record | `<gcd>/kankan/qa.json` ([§2](#2-configuration-and-seat)) |
| Q&A seat worktree | `~/coding/.kankan/<project>/<type>-qa` |

**§5.2 Local state.** Replace the text starting “| Seat state | `<gcd>/kankan/seats/<seat>.state.json` (current…” with:

| Seat state | `<gcd>/kankan/seats/<seat>.state.json` (current task, signals, label causes, comment cursor, test verifications, stop request; for the Q&A seat the conversation record of [§4](#4-launcher)) |

**§5.3 Components.** Add:

| `qa` | Q&A seat policy: claim, conversation end, delivery record, answer chunking, handoff. |

**§8.4 State machine.** Add:

| `qa_waiting` | Q&A seat after `answer`/`fail`: follow-up polling ([§4](#4-launcher)) | no |

**§8.5 Cycle and signals.** Add:

> On the Q&A seat a task is a conversation: `answer` and `fail` end a turn and not the task; follow-ups start new turns in the same agent conversation ([§4](#4-launcher)).

**§10.3 Phases and transitions.** Add the rows of [§3](#3-card-ref-phase-qa).

**§10.4 Convergence.** Add:

- cards in phase `qa` are never moved; `parked` refs with `pos: bottom` are moved to the bottom of the target list.

**§10.1 Primitives (`refstore`).** Replace the text starting “Inputs validated before reaching Git: …” with:

> Inputs validated before reaching Git: card ID `^[0-9a-f]{24}$`, shortLink `^[A-Za-z0-9]{8}$`, seat `^[a-z]+-([0-9]+|qa)$`.

**§18.1 Strategy.** Add the tests of [§8](#8-tests).

## 10. Changes to the board settings technical specification

How this feature changes [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md), by its section numbers. That file is not edited.

**§3 Model and effort.** Add:

> A third role, `questions`, with its setting card, seeds and probes, as `implementation` and `review`. The Q&A seat pins the `questions` pair at claim and at every restart; the planned restart for a changed pair happens only in `idle` or `qa_waiting`, never during a turn, and there is no fallback to another role's pair.
