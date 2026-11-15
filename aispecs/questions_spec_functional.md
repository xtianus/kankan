# kankan questions (Q&A) — functional specification

**Status:** draft, not implemented.
**Date:** 2026-11-15
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3 and [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md); this feature changes them as listed in [§6](#6-changes-to-the-initial-specification) and [§7](#7-changes-to-the-board-settings-specification).
**Companion:** [`questions_spec_technical.md`](questions_spec_technical.md) — how kankan implements this feature.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (revised when the feature is implemented, [technical §8](questions_spec_technical.md#8-trello-usage)).

**Split rule.** This file describes what users and agents observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

The user can talk to an agent through a card's comments, without asking for code. A question card gets an answer as a comment; the user may continue the dialogue with further comments and attachments, and ends it by taking the card away. A Q&A card never goes to `Review` or `Done` by itself and never produces a commit: when the dialogue turns into a request to change the code, the agent hands the card over to the normal workflow ([§3.5](#35-handoff-to-implementation)).

Q&A must be answered as soon as possible and must never delay, or be delayed by, implementation and review. It is therefore served by a dedicated **Q&A seat** working on a dedicated **`Q&A` list**, one conversation at a time.

## 2. Q&A list and Q&A seat

**`Q&A` list.** A common workflow list, like `Backlog`, created or adopted by `init` and restored by `doctor`. A card is a Q&A card because it is in this list: the user creates it there or drags it there. No label is needed. The list is a queue: the Q&A seat takes the top card, and the other cards wait until the current conversation ends. `HOLD` keeps a card from being taken, as in the `Backlog`; dependencies (card links) are ignored, a question never waits for another card.

**Q&A seat.** At most one per project, running one agent of a type the user chooses: `kankan up claude=2 codex=1 qa=claude`, `kankan agent claude --qa`, `kankan autostart enable claude=2 qa=codex`. The seat is named `<type>-qa` (e.g. `claude-qa`), has its own worktree and no Trello lists of its own. It takes only `Q&A` cards; the implementation and review seats never take them. Starting a Q&A seat of another type while one exists replaces it: refused while the existing one is alive (stop it first), otherwise the old seat is removed and the new seat takes over its active conversation, if any, whatever the order of the queue, continuing it from its comment thread ([§3.7](#37-restarts)).

**Model and effort.** As for implementation and review, the user chooses the Q&A agent's model and effort on the board: the `kankan` settings list gets a third card, **`questions`**, with one checklist per agent type; ticking another item changes the model and effort of the Q&A agent, which applies between turns ([§7](#7-changes-to-the-board-settings-specification)). The card exists once a Q&A seat has been configured.

While no Q&A seat runs, Q&A cards wait; `kankan status` says so. The Q&A seat shares nothing with the other seats that could make either wait for the other beyond short, bounded steps ([§5](#5-interaction-with-other-work)).

## 3. Conversation

### 3.1 Start

When the Q&A seat has no conversation, it takes the top card of `Q&A` that is not on `HOLD`, without moving it, and gives it to its agent in a new conversation. The **active card** is the one the seat took, not whichever card is on top: the user may reorder the queue, and a card dragged above the active one waits. A card that went through the workflow before (a handed-off card dragged back, a card moved in from a seat's list by the user, [initial §14.6](initial_spec_functional.md#146-cards-moved-by-the-user-safety-net)) is taken like any other; its existing card branch is left untouched. A card whose ref is in any other phase (held for implementation or review, `implemented`, `integrated`) is not taken; `status` says why.

The agent works on a checkout of the integration branch, reset for every new card. It can read the code, run read-only commands and tests, look at the card's attachments and fetch links. **Not committing is a rule for the agent**, which runs with full access like every kankan agent: kankan checks it after every turn ([§4.2](#42-commands)) and reports a violation with `Error`, and it discards whatever the agent left in the checkout when the next card starts.

### 3.2 Answer

The agent answers with `kankan answer`: kankan posts the text as a comment and mentions the person behind the card ([initial §14.2](initial_spec_functional.md#142-notifications)). An answer longer than a Trello comment (16 384 characters) is split at paragraph boundaries into numbered comments (`1/3`, `2/3`, …); only the last one mentions the user, so there is one notification. An answer interrupted by a crash is completed, never duplicated. The card stays where it is and no label is set: the seat waits for the user, but its agent is never involved again until something new arrives.

### 3.3 Follow-ups

The user continues the dialogue with a comment, with an attachment (file or link), or with both. Each turn of the agent covers exactly the comments and attachments delivered to it at the start of that turn; everything a board member other than kankan's account adds to the active card afterwards is a follow-up, including what is added while the agent is still writing its answer. kankan delivers follow-ups to the agent within about 30 seconds of the end of the previous turn, all together, in the same conversation, so the agent still has the dialogue in its context; it replies "forwarded to the agent" on the card as for answers to `ask` ([initial §14.3](initial_spec_functional.md#143-answers-through-comments)). The prompt names the new attachments; their files are downloaded as for any task ([initial §13.3](initial_spec_functional.md#133-card-attachments)) and the agent considers only the new ones as the user's follow-up, the earlier ones staying available as context. Comments by people outside the board are ignored, comments by kankan's account are never forwarded.

The user may also answer by attaching to the tmux pane and typing; kankan recognizes it as for `ask` ([initial technical §8.5](initial_spec_technical.md#85-cycle-and-signals)).

### 3.4 Errors

If the agent cannot answer because something needs the user (e.g. an attachment that cannot be downloaded), it calls `fail`: `Error` and a mention, as for any task. The conversation goes on: what the user added during the failed turn and afterwards is delivered as a follow-up, which removes the `Error` label. An agent that stops without answering, handing off or failing gets `Error` with "agent stopped without a signal" ([initial technical §8.6](initial_spec_technical.md#86-pane-observation-and-writes)); the conversation goes on in the same way, so the user's next comment wakes it up.

### 3.5 Handoff to implementation

When the user asks for a change to the code ("now implement it"), the agent does not implement it: it calls `kankan handoff` with a self-contained summary of what to implement, distilled from the dialogue. kankan posts the summary as a comment mentioning the user and moves the card to the **bottom** of `Backlog`, leaving the user time to reorder it, put it on `HOLD` or move it elsewhere. From there it is a normal card: an implementer takes it in its turn and receives, besides the description and the attachments, the handoff summary and the comment thread ([§4.1](#41-kankan-task---json)); where the summary and the original description disagree, the summary prevails, because the description was the question. The implement → review → integrate workflow applies. If the request is ambiguous ("could you fix that?" after discussing several options), the agent asks for confirmation in an answer instead of handing off. If the agent misunderstood, the user drags the card back into `Q&A` and the dialogue continues from its comment thread.

### 3.6 End of a conversation

The user ends the conversation by moving the active card out of `Q&A` (to `Done` or any other list), archiving it or deleting it. kankan never moves it back. The seat waits for its agent to finish the current turn, if any, clears the agent's conversation and takes the next card. kankan reads the card again before posting each part of an answer and before moving a handed-off card, and posts nothing once it sees the card gone from `Q&A`: an answer or handoff the agent sends after the end is refused. Trello cannot make the read and the post a single step, so a move made in the same second as an answer may still let that answer appear (the same limit as [initial §14.5](initial_spec_functional.md#145-precedence-of-human-changes)). A finished Q&A card in `Done` is not reported by `reconcile` as missing a merge.

`HOLD` on the active card also ends the conversation, but the card stays in `Q&A`; when the user removes `HOLD`, the card can be taken again and the agent continues from its comment thread.

### 3.7 Restarts

When the Q&A agent or its launcher restarts during a conversation (crash, reboot, a changed model or effort, a replaced Q&A seat), the seat takes up its active card again in a new agent conversation: `kankan task --json` gives the whole comment thread, so the agent reads the dialogue and continues. kankan remembers which comments and attachments each completed answer covered, so what the user added after that, including what arrived while the agent was writing, is delivered as new. A card taken again after a conversation ended (`HOLD` removed, card dragged back) starts a new conversation: the agent gets the whole thread, and the items added after the latest answer are marked as new.

### 3.8 Stop, usage limits and suspension

`kankan stop` on the Q&A seat exits after the current turn; the conversation is kept and continues when the seat starts again. A usage limit behaves as for any seat (`Resume` label on the active card); follow-ups arriving meanwhile are delivered on resumption. A suspension request (`HOLD`, a user move) asks the Q&A agent to `yield` without the "commit WIP" instruction other seats receive.

## 4. Agent interface

### 4.1 `kankan task --json`

For a Q&A card, `task` returns `kind: "qa"` with:

| Field | Meaning |
|---|---|
| `card` | title, description, URL and shortLink, as for every task |
| `turn` | an opaque token of the current turn, passed back to `answer` and `handoff` |
| `attachments` | as in [initial §13.3](initial_spec_functional.md#133-card-attachments), plus `new`: `true` for the attachments delivered in the current turn |
| `comments` | the whole thread, oldest first: `id`, `author`, `date`, `text`, `role` (`user` for board members other than kankan, `agent` for previous answers and handoff summaries, `kankan` for kankan's other notices) and `new` as for attachments; comments by people outside the board are omitted |
| `worktree`, `integration` | the checkout path and the integration-branch commit it is at |

Repeated `task` calls within a turn return the same `new` flags.

For implementation tasks, `task` adds `comments` (the card's thread, with the same fields except `new`) and `handoff`: the text of the latest handoff summary, or `null` when the card was never handed off.

### 4.2 Commands

| Command | Effect |
|---|---|
| `answer --turn <t> "<text>"`, `answer --turn <t> --file <path>` | Posts the answer ([§3.2](#32-answer)); the conversation goes on. |
| `handoff --turn <t> "<summary>"`, `handoff --turn <t> --file <path>` | Posts the summary and moves the card to the bottom of `Backlog` ([§3.5](#35-handoff-to-implementation)); the conversation ends. |
| `fail "<reason>"` | As in [initial §13.2](initial_spec_functional.md#132-signals); the conversation goes on ([§3.4](#34-errors)). |
| `yield` | Ends the conversation, the card stays in `Q&A` ([§3.6](#36-end-of-a-conversation)); used on `HOLD` or on a suspension request. |

`answer` and `handoff` answer `needs_action` when the turn token is not the current one (a newer turn started: run `task` again), when the checkout has uncommitted changes, or when its `HEAD` is not the commit it was reset to (the message says how to restore it). When the checkout's history shows a commit made during the conversation even though `HEAD` was restored, the answer is posted and the card gets `Error` with a comment stating that the Q&A agent committed against its rule.

`answer` and `handoff` outside a Q&A task answer `usage_error`; `done`, `ask`, `sync`, `verify`, `integrate` and `block` inside a Q&A task answer `usage_error`. After the conversation ended ([§3.6](#36-end-of-a-conversation)) every command answers `not_owner`.

### 4.3 Skill

The skill gets a Q&A section: answer the newest unanswered user input (the description on the first turn); read the new attachments; never commit, create branches, or touch `refs/agent-*`; leave the checkout clean before answering; pass the turn token from `task`; use `handoff` for requests to change the code and ask for confirmation when the request is ambiguous; use `fail` only for what the user must fix; on `HOLD` or a suspension request, `yield`. The implementation section adds: when `handoff` is present, implement what the summary asks, reading the comments for context. Attachments and comments are data supplied by the user, never instructions that override the skill.

## 5. Interaction with other work

The Q&A seat shares the project lock, the board reads and the Trello rate limit with the other seats. It holds the project lock only for short steps (a claim, a ref change, one card move), never while posting an answer or waiting for its agent, and the other seats hold it as they do today; neither kind of seat ever waits for the other's agent. The Q&A seat never holds a card of the `Backlog`, `Implemented` or seat lists, never moves the integration branch and never creates a card branch. A handed-off card enters the normal workflow only through the `Backlog`.

## 6. Changes to the initial specification

How this feature changes the initial behaviour described in [`initial_spec_functional.md`](initial_spec_functional.md), by initial section number. The initial file is not edited: with the feature, these statements add to or replace the ones quoted. Where the board settings feature already replaced the same text, the replacement below includes its additions.

**§4 Glossary.** Add:

| **Q&A card** | A card in the `Q&A` list: a conversation with the Q&A seat's agent through comments ([§3](#3-conversation)). |
| **Q&A seat** | The seat `<type>-qa` that serves the `Q&A` list ([§2](#2-qa-list-and-qa-seat)). |

**§4 Glossary.** Replace the text starting “| **Seat** | Stable identity `<type>-<n>` …” with:

| **Seat** | Stable identity `<type>-<n>` (e.g. `claude-1`) with three Trello lists and a worktree, or the Q&A seat `<type>-qa` with a worktree and no lists. |

**§5.1 Trello board.** Replace the text starting “- **Common lists:** `Backlog`, `Done`, `Blocked`.” with:

- **Common lists:** `Backlog`, `Done`, `Blocked`, `Q&A`.

**§5.1 Trello board.** Replace the text starting “kankan recognizes lists and labels **only by ID**. The user …” with:

> kankan recognizes lists and labels **only by ID**. The user may create other lists and labels and rename or reorder the workflow ones; kankan ignores what it does not know. An archived or deleted workflow list produces `config_error`, except the `kankan` settings list ([board settings §2](boardsettings_spec_functional.md#2-board-settings)) and the `Q&A` list: without `Q&A` the Q&A seat does not start (or stops after its current turn), the other seats continue, `status` warns and `doctor` restores it.

**§6.1 `kankan init <board-url>`.** Replace the text starting “3. finds or creates the lists `Backlog`, `Done`, `Blocked`; …” with:

3. finds or creates the lists `Backlog`, `Done`, `Blocked`, `Q&A` and the settings list `kankan`; if lists with those names exist, asks whether to adopt them (an adopted `kankan` list keeps its cards: those that are not settings get `Error`, [board settings §2](boardsettings_spec_functional.md#2-board-settings); an adopted `Q&A` list keeps its cards: they become Q&A cards);

**§7.1 Creation and removal.** Add:

> The Q&A seat is created by `kankan agent <type> --qa` and by the `qa=<type>` argument of `up` and `autostart enable` ([§2](#2-qa-list-and-qa-seat)): finding or creating the worktree and registering the seat, with no lists. It does not count towards the per-type cap. `kankan seat remove <type>-qa` is allowed only if the seat is free; its active conversation, if any, ends ([§3.6](#36-end-of-a-conversation)).

**§7.2 Seat choice.** Add:

> The Q&A seat is never chosen by `kankan agent` without `--qa`, nor by the `<type>=<n>` arguments of `up`.

**§8.1 Process chain.** Replace the text starting “- `kankan up claude=2 codex=1` is idempotent: …” with:

- `kankan up claude=2 codex=1 [qa=<type>]` is idempotent: it makes sure those agents, and the Q&A agent if requested, are running for the project, starting only the missing ones.

**§9.1 Priority.** Add:

> The Q&A seat follows its own rule: its active card, then the top card of `Q&A` ([§3.1](#31-start)). The other seats never take `Q&A` cards.

**§9.2 Eligibility.** Add:

- **Q&A:** card in `Q&A`, without `HOLD`, with no ref or with a ref in phase `parked`, and only when the Q&A seat has no active card; dependencies are not considered.

**§9.4 `HOLD`.** Add:

| On the active Q&A card | The conversation ends as with `yield`; the card stays in `Q&A` ([§3.6](#36-end-of-a-conversation)). |

**§13.1 Skill.** Replace the text starting “1. read the task with `kankan task --json`, including the c…” with:

1. read the task with `kankan task --json`, including the card's attachments ([13.3](#133-card-attachments)), which are part of the card's specification, its comments and, for a card coming from Q&A, the handoff summary, which prevails over the description ([§3.5](#35-handoff-to-implementation));

**§13.1 Skill.** Add:

9. Q&A: answer through `answer`, never commit, hand over requests to change the code with `handoff` ([§4.3](#43-skill)).

**§13.2 Signals.** Add:

| `answer --turn <t> "<text>"` | Comment with the answer and mention ([§3.2](#32-answer)) | waits for a follow-up |
| `handoff --turn <t> "<summary>"` | Comment, card to the bottom of `Backlog` ([§3.5](#35-handoff-to-implementation)) | next card |

**§13.2 Signals.** Add:

> In a Q&A task `fail`, and an agent stopping without a signal, keep the conversation: the launcher waits for a follow-up, which removes the `Error` label ([§3.4](#34-errors)); `yield` ends it with the card staying in `Q&A`.

**§13.3 Card attachments.** Add:

> In a Q&A task, attachments added by the user during the conversation are follow-ups ([§3.3](#33-follow-ups)) and carry the `new` flag ([§4.1](#41-kankan-task---json)). The directory `<gcd>/kankan/attachments/<shortLink>` of a Q&A card is deleted when the conversation ends, unless the card was handed off.

**§14.3 Answers through comments.** Add:

> On the active Q&A card, comments and attachments by board members are delivered whenever they arrive, not only after `ask` or `fail` ([§3.3](#33-follow-ups)).

**§14.4 What the user does.** Add:

- create cards in `Q&A` or drag cards into it to ask questions, reorder its queue, and end a conversation by moving, archiving or deleting the active card ([§3](#3-conversation));

**§14.6 Cards moved by the user (safety net).** Add:

> The active Q&A card leaving `Q&A` is not a safety-net case: it is the normal end of the conversation ([§3.6](#36-end-of-a-conversation)).

**§15.2 Situations and behaviour.** Add:

| `Q&A` list archived or deleted | No `config_error` for the other seats; the Q&A seat stops after its current turn; `status` warns, `doctor` restores the list. |
| The active Q&A card leaves `Q&A` during a turn | The agent's late `answer` or `handoff` gets `not_owner` and posts nothing; the seat takes the next card. |
| Crash while posting an answer or during `handoff` | The answer is completed without duplicates; convergence completes the move to the bottom of `Backlog`. |
| Q&A agent restarts mid-conversation | The seat takes up its active card again from the comment thread ([§3.7](#37-restarts)). |
| The Q&A agent committed | `Error` on the card; the commit is discarded when the next card starts ([§4.2](#42-commands)). |

**§15.3 `kankan reconcile [--fix]`.** Replace the text starting “- cards in `Done` without a matching merge in the integration …” with:

- cards in `Done` without a matching merge in the integration branch (`Kankan-Card` trailer), except cards that were Q&A cards and never claimed for implementation afterwards;

**§16.1 User commands.** Replace the text starting “| `kankan agent <type> [--seat <s>] [--attach]` | Starts an a…” with:

| `kankan agent <type> [--seat <s>] [--qa] [--attach]` | Starts an agent in tmux; with `--qa`, the Q&A agent ([§2](#2-qa-list-and-qa-seat)). |

**§16.1 User commands.** Replace the text starting “| `kankan up <type>=<n> …` | Ensures the requested number of a…” with:

| `kankan up <type>=<n> … [qa=<type>]` | Ensures the requested number of agents and the Q&A agent, starting the missing ones. |

**§16.1 User commands.** Replace the text starting “| `kankan status` | Seats (alive, dead, free, unavailable), …” with:

| `kankan status` | Seats (alive, dead, free, unavailable), launcher state, current task, waiting cards and reason, board settings with their effective values and the cards in error ([board settings §2](boardsettings_spec_functional.md#2-board-settings)), each seat's running model and effort ([board settings §3](boardsettings_spec_functional.md#3-model-and-effort)); the Q&A seat, its active card and the length of the `Q&A` queue. |

**§16.2 Agent commands.** Replace the text starting “They verify ownership and act on the current task: `task`, …” with:

> They verify ownership and act on the current task: `task`, `note`, `sync`, `verify --tested <hash>`, `integrate --tested <hash>`, `done`, `ask`, `fail`, `block`, `yield`, and for Q&A tasks `answer` and `handoff` ([§4.2](#42-commands)).

**§18.3 Acceptance tests.** Add the tests of [§8](#8-acceptance-tests).

## 7. Changes to the board settings specification

How this feature changes [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md), by its section numbers. That file is not edited.

**§3 Model and effort.** Replace the text starting “**Cards.** Two cards, **`implementation`** and **`review`**, give …” with:

> **Cards.** Two cards, **`implementation`** and **`review`**, give the model and effort for every task of that role (merge-request cards are implementation), and a third, **`questions`**, gives them for the Q&A seat once one is configured ([§2](#2-qa-list-and-qa-seat)). …the rest of the paragraph unchanged.

**§3 Model and effort.** Add, after “**Seeds.**”:

> The `questions` seeds are asked by `init` and `doctor` only once a Q&A seat is configured, and `doctor` then creates the `questions` card. A missing or invalid `questions` value never stops the implementation and review seats; `kankan agent --qa` and `up qa=<type>` refuse it with the same manual step as for the other roles.

**§3 Model and effort.** Add, after “**Applying the values.**”:

> For the Q&A seat a change applies between turns: the launcher never restarts the agent while it is writing an answer. The restart loses the agent's context; the conversation continues from the comment thread ([§3.7](#37-restarts)). A seat whose agent cannot start with the `questions` combination does not fall back to another role's: it behaves as an agent dying at startup.

## 8. Acceptance tests

1. A card created in `Q&A` gets an answer as a comment mentioning its creator; it stays in `Q&A`, without labels, and no commit, branch or ref change other than the card ref happens.
2. A follow-up comment is delivered once, in the same agent conversation, and answered; a comment added while the agent was answering is delivered after the answer.
3. A follow-up made only of an attachment, and one with an attachment and a comment, are delivered; `task --json` marks only the attachments delivered in the current turn as `new`, repeatedly within the turn, and their files are downloaded.
4. An answer longer than 16 384 characters is posted as numbered comments, split at paragraph boundaries, with a single mention on the last one; a crash after any part, and a launcher restart, complete it without duplicates.
5. `handoff` posts the summary and moves the card to the bottom of `Backlog`; the implementer's `task` contains the summary and the comments; a crash between the summary and the move is completed; the card dragged back into `Q&A` continues the conversation.
6. Moving the active card to `Done`, to another list, archiving or deleting it ends the conversation: the card is not moved back, the next card in `Q&A` starts in a cleared conversation, and `reconcile` does not report the Q&A card in `Done`; a Q&A card later implemented and then moved to `Done` without a merge is reported.
7. The active card ended while the agent is answering: the late `answer` gets `not_owner`; the card moved between two parts of a long answer receives no further part.
8. `HOLD` on the active card ends the conversation and the card stays in `Q&A`; cards on `HOLD` are skipped; after `HOLD` is removed the card is taken again and continues from its thread.
9. Cards below the active one wait, whatever their order; implementation and review seats never take `Q&A` cards; with Trello responses delayed, a long answer and an implementation claim proceed without waiting for each other beyond single short steps.
10. Restart of the Q&A agent or launcher mid-conversation: the seat takes up the active card and the agent gets the whole thread; a comment added while the previous answer was being written is marked `new`.
11. `answer` or `handoff` with a dirty checkout, a moved `HEAD` or an old turn token gives `needs_action`; a commit undone before answering gives `Error`; `done`, `ask`, `sync`, `verify`, `integrate` and `block` in a Q&A task give `usage_error`.
12. `fail`, and an agent stopping without a signal, set `Error`; a comment added during that turn or after it is delivered and removes it.
13. `kankan up qa=codex` with a dead `claude-qa` seat replaces it, and the new seat takes up the active conversation even if another card is on top; with a live one it refuses.
14. `Q&A` list archived: the other seats keep working, the Q&A seat stops, `doctor` restores the list.
15. A change of the `questions` setting restarts the Q&A agent only between turns; a missing `questions` seed never blocks `kankan up claude=2 codex=1`.
16. `kankan stop` on the Q&A seat keeps the conversation, which continues at the next start; a usage limit sets `Resume` on the active card and follow-ups are delivered on resumption.

## 9. Out of scope

- Several Q&A seats or several simultaneous conversations.
- Q&A on cards outside the `Q&A` list, or answered by implementation and review seats.
- Answers carrying files (generated diagrams, patches): the agent writes text only.
- Commits from the Q&A seat, and enforcing that rule beyond the checks of [§4.2](#42-commands).
