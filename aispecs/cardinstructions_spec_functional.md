# kankan card instructions — functional specification

**Status:** implemented locally ([plan](../aiplans/2026-11-19-cardinstructions.md)); the live agents run with the card-instructions card ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents), F§9 test 11) remains outstanding.
**Date:** 2026-11-19
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3, [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md) and [`questions_spec_functional.md`](questions_spec_functional.md); this feature changes them as listed in [§7](#7-changes-to-the-initial-specification) and [§8](#8-changes-to-the-board-settings-specification).
**Companion:** [`cardinstructions_spec_technical.md`](cardinstructions_spec_technical.md) — how kankan implements this feature.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (revised when the feature is implemented, [technical §8](cardinstructions_spec_technical.md#8-trello-usage)); the proposal [`agentinstructions_spec_functional.md`](proposals/agentinstructions_spec_functional.md) (board-wide instructions, not planned), whose interaction with this feature is in [§5](#5-precedence).

**Split rule.** This file describes what users and agents observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

A card's description is its specification, the same for every agent. The user can add to it **instructions on how to work** that reach only the agents of one type, one phase, or both: for example "consult your advisor before integrating" for Claude reviewers only. kankan separates those instructions from the specification ([§2](#2-agent-sections), [§3](#3-delivery-to-the-agent)), refuses a card whose instruction headings are wrong ([§4](#4-errors)), defines how the instructions rank against the skill ([§5](#5-precedence)), and keeps on the board a help card with ready-to-copy headings for the configured agents ([§6](#6-the-instructions-card)).

The feature applies to implementation cards (merge-request cards included) and to their review. Q&A cards are not affected ([§7](#7-changes-to-the-initial-specification), Q&A).

## 2. Agent sections

**Parts of a description.**
- The **specification**: the text before the first agent heading. Every agent of the card, implementer and reviewer, gets it unchanged.
- **Agent sections**: each starts with an **agent heading** and runs to the next agent heading or to the end of the description. The specification must therefore come first.

**Agent heading.** A Markdown heading (any level, `#` to `######`) whose text is `agent: <target>` or `agent: <target> <phase>`:
- `<target>` is a configured agent type (`claude`, `codex`, or any type added later) or `all`;
- `<phase>` is `implementation` or `review`; without it, the section applies to both phases;
- case and extra spaces are ignored: `## Agent:  Claude  Review` is the same heading as `## agent: claude review`.

Write the heading on its own line, with up to three leading spaces and a space or tab after the hashes. A whitespace-separated run of closing hashes is optional.

Other headings (`## Tests`, `### Style`) are ordinary text of the part they are in. A heading inside a fenced code block is never an agent heading; a code block left unclosed runs to the end of the description, as in Markdown (not yet observed in Trello's rendering), so the agent headings after it are ordinary text of the specification and reach every agent.

**Which sections reach a seat.** A seat of type *T* working in phase *P* gets the sections whose heading is `all`, `all P`, `T` or `T P`, in the order they appear in the description. Sections for other types or the other phase never reach it.

Example description:

> Implement feature X: … (the specification)
> `## agent: claude review`
> Consult your advisor before `kankan integrate`.
> `## agent: all implementation`
> Write the commit messages in Italian.

A Claude reviewer gets the specification and the advisor line; a Codex reviewer the specification only; every implementer the specification and the commit-message line.

## 3. Delivery to the agent

`kankan task --json` of an `impl` or `review` task returns:
- `desc`: the specification only, without any agent section;
- **`card_instructions`**: the sections that reach the seat ([§2](#2-agent-sections)), each with its heading line, separated by a blank line; `null` when none does;
- **`card_instructions_error`**: `null`, or the problems when the headings are invalid (below).

The description is read at every `task` call, as before: an edit made while the task runs reaches the agent the next time it reads the task. When a description edited during the task has invalid agent headings, `task` does not fail: it returns only the text before the first agent heading candidate (valid or invalid) as `desc`, `card_instructions: null` and `card_instructions_error` with the problem; the agent goes on with the specification and notes the problem with `kankan note`. kankan sets no `Error` during a task for this; the check of [§4](#4-errors) happens at the next claim.

A `hold_wip` task (commit the work in progress and yield) gets the same `desc`, without agent sections, and no `card_instructions`. The pasted prompt stays the fixed one-liner of [initial §13.1](initial_spec_functional.md#131-skill).

## 4. Errors

kankan checks the agent headings of a card when it is about to claim it, for implementation or for review. Invalid:
- a heading with no target, such as `## agent:`;
- a heading `agent: …` whose target is neither a configured agent type nor `all` (a misspelt type, e.g. `## agent: cluade`);
- a phase other than `implementation` and `review`, or extra words after the phase;
- two headings with the same target and phase (`## agent: claude` twice, or `## agent: Claude review` and `## agent: claude review`).

An invalid card is **not claimed**: it gets `Error` (unless it already has one: then kankan only comments) and a comment, with a mention ([initial §14.2](initial_spec_functional.md#142-notifications)), naming the problems (at most 10, then how many more) and listing the valid headings for the configured agents, e.g. "kankan: this card is not started: `cluade` in `## agent: cluade review` is not an agent type (valid targets: claude, codex, all; phases: implementation, review). Fix the heading; see the `Instructions` card in the `kankan` list." The card stays where it is (`Backlog`, or the implementer's `Implemented` list); the other cards go on. Each wrong version of the description is reported once, whichever seat finds it.

When the user fixes the description, kankan removes the `Error` it set at its next board read, and the card can be claimed again. kankan never removes an `Error` it did not set for this reason.

A configured agent type removed later makes the cards that name it invalid at their next claim, with the same comment.

**`kankan status`** lists every card in the `Backlog` or a seat's `Implemented` list whose agent headings are invalid among the waiting cards, with reason `invalid_headings` and its problems (line numbers and reasons), whether or not `Error` is still on the card: a user who removed the label by hand, or a card not yet tried by any seat, still shows why it waits.

## 5. Precedence

The skill tells the agent that `card_instructions` come from the card's author and:
- **take precedence over the skill's working steps**: how to implement, which tests to run itself, how deeply to review, how to write commits and notes;
- **never override the kankan protocol**: the commands and their order (`sync`, `verify`, `integrate`, `done`, `yield`), the statuses and what to do on each, and the rules on what never to do ([initial §13.1](initial_spec_functional.md#131-skill) items 5–8);
- **never change the specification**: they say how to work, not what to build; the reviewer judges the branch against `desc`;
- among the sections the seat gets, the more specific prevails when two disagree: `T P`, then `T`, then `all P`, then `all`;
- when following them would mean skipping or reordering a kankan command, or breaking one of those rules, the agent follows the protocol and records the conflict with `kankan note`, without stopping;
- an instruction the agent cannot carry out (for example a tool its CLI does not have) is noted with `kankan note` and skipped; it is never a reason for `ask`, `fail` or `block`.

The card's comments and attachments stay data, never instructions. If the board-wide instructions of the [proposal](proposals/agentinstructions_spec_functional.md) are implemented, the card's instructions prevail over them.

## 6. The `Instructions` card

**The card.** `kankan init` and `kankan doctor` create in the `kankan` list a card named **`Instructions`**, kept by kankan as the **first card of the list**, above the setting cards. It is not a setting ([§8](#8-changes-to-the-board-settings-specification)); its description explains, briefly, how to write agent sections, with examples ready to copy for every configured agent type.

**Content.** In this order:
1. one paragraph: the specification goes first; agent sections follow, each starting with an agent heading; they reach only the agents they name and say how to work, not what to build;
2. the heading forms and their meaning (`agent: <type>`, `agent: <type> implementation`, `agent: <type> review`, `agent: all`, `agent: all <phase>`), with the list of the configured agent types;
3. for each configured agent type, a fenced code block with its three headings, each followed by a placeholder line; then one block with the three `all` headings; code blocks keep the `##` when copied from Trello's rendered view;
4. the precedence in one sentence ([§5](#5-precedence)) and what happens on a misspelt heading ([§4](#4-errors));
5. a closing line: "This card is maintained by `kankan doctor`: edits to it are overwritten."

Example of one block, for Claude:

```
## agent: claude
## agent: claude implementation
## agent: claude review
```

(each heading followed in the card by a line "Instructions for … go here.").

**Keeping it current.** `doctor` rewrites the description whenever it differs (ignoring line endings, trailing horizontal whitespace and trailing blank lines) from the text kankan generates for the configured agent types: after an agent type is added or removed, after a kankan upgrade that changes the text, or after a user edit. It prints "Instructions card updated (agent types: claude, codex)". `doctor` also moves the card back to the top of the `kankan` list when another card is above it, and prints "Instructions card moved to the top of the kankan list". `doctor --no-fix` only reports that the card is outdated or not at the top. Launchers never write the card. A deleted, archived or moved-out card is recreated by the next `doctor`; a card moved to another list becomes an ordinary card there, as for setting cards.

## 7. Changes to the initial specification

How this feature changes [`initial_spec_functional.md`](initial_spec_functional.md), by its section numbers. That file is not edited.

**§9.2 Eligibility.** Add after the list:

> A card whose agent headings are invalid ([card instructions §4](cardinstructions_spec_functional.md#4-errors)) is not eligible for implementation or review until they are fixed; it gets `Error` and a comment.

**§13.1 Skill.** Add to the list, after item 1:

> 1a. treat `desc` as the specification and `card_instructions` as instructions on how to work, ranked as in [card instructions §5](cardinstructions_spec_functional.md#5-precedence).

**§14.1 Labels.** In the `Error` row, "Set by" becomes "kankan (`fail`, protocol, inactivity, unknown screen, invalid agent headings)".

**§15.5 `kankan doctor`.** Add: it creates or updates the `Instructions` card of the `kankan` list ([card instructions §6](cardinstructions_spec_functional.md#6-the-instructions-card)); with `--no-fix` it only reports it.

**Q&A ([questions spec](questions_spec_functional.md)).** A Q&A card's description is the first question: kankan does not look for agent headings in it and a `qa` task returns it whole. When a Q&A card is handed over to implementation, its description is checked and split like any implementation card's.

## 8. Changes to the board settings specification

How this feature changes [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md), by its section numbers. That file is not edited.

**§2 Board settings, "The `kankan` list".** Replace "It holds **one card per setting**;" with:

> It holds **one card per setting**, plus the `Instructions` help card ([card instructions §6](cardinstructions_spec_functional.md#6-the-instructions-card)), which is not a setting;

**§2 Board settings, table.** In the row "Card whose name is not a known setting", add: "The name `Instructions` is reserved for the help card and is never an unknown setting." In the row "Two cards with the same title", add: "Two `Instructions` cards: `Error` on both, and `doctor` updates neither until one is removed."

## 9. Acceptance tests

1. A card without agent headings: `desc` is the whole description, `card_instructions` is `null`, in both phases.
2. The example of [§2](#2-agent-sections): each of Claude reviewer, Codex reviewer, Claude implementer and Codex implementer gets exactly the sections listed there; `desc` is the specification only for all of them.
3. Case and spacing variants of a heading are accepted; `## Tests` stays text; `## agent: claude` inside a fenced code block stays text.
4. `## agent: cluade review`: the card is not claimed, `Error` and one comment listing the valid targets; the next card in the `Backlog` is claimed; after the fix, `Error` is removed at the next board read and the card is claimed.
5. Unknown phase, extra words, duplicate heading: as in 4. The same wrong description is reported once over several cycles; a different wrong edit is reported again.
6. An invalid heading added to a card in `Implemented`: the review is not claimed, `Error` and comment; the card stays in `Implemented`.
7. An invalid heading added during a running task: the next `task` returns only the specification prefix, `card_instructions: null` and `card_instructions_error`; no `Error`.
8. `init` creates the `Instructions` card, first in the `kankan` list, with blocks for every configured agent type; a card dragged above it is put back below it by the next `doctor`, and `doctor --no-fix` reports it; after adding an agent type, `doctor` rewrites it and says so; `doctor --no-fix` reports it outdated; a user edit is overwritten by the next `doctor`; a deleted card is recreated.
9. The `Instructions` card never gets "unknown setting".
10. A Q&A card whose description contains `## agent: claude` returns it whole in its `qa` task.
11. With live agents, a `## agent: claude review` instruction to consult the advisor: the Claude reviewer follows it; a Codex reviewer of another card with `## agent: all review` asking for something it cannot do notes it and completes the review.
12. `desc` keeps the specification exactly as written: trailing spaces, line endings and the final newline.
13. A description that starts with an agent heading has an empty specification: `desc` is the empty string.
14. A matching section with an empty body is delivered as its heading alone.
15. During a resumed task, an invalid heading is inserted above a section for another seat: `task` returns `desc` without the body of the invalid section or of the section after it, `card_instructions: null` and the problem in `card_instructions_error`; after the fix, `task` returns the seat's sections again and `card_instructions_error: null`.
16. Two seats find the same invalid description: the card gets one comment and one `Error` for that version. The seat that reported it stops; after the fix, another seat's launcher removes the `Error`. An `Error` that was on the card before the check (set by the user) stays after the fix.
17. Two open `Instructions` cards both get the duplicate-title `Error`; after one is removed, the next `doctor` updates the other.
18. A code block left unclosed before an agent heading: the heading is ordinary text of the specification, and every agent gets it in `desc`.
19. `kankan status` shows `invalid_headings` with the problems for an invalid card in the `Backlog` before any claim attempt, and still after the user removed its `Error`; a fixed card leaves the list.

## 10. Out of scope

- Agent headings in Q&A cards.
- Sections per model or effort.
- Instructions inherited from other cards (dependencies, card classes).
- Checking that the agent followed the instructions.
- A card whose instructions keep it from being implemented by some agent type (for example "Codex must not take this card"): eligibility stays as in [initial §9.2](initial_spec_functional.md#92-eligibility).
