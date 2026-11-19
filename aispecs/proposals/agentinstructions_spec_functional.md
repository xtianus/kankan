# kankan agent instructions from the board — functional specification

**Status:** proposal, not planned. It is not to be implemented until a real need for it is found; until then the repository's own instruction files (`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex) cover the known cases ([§1](#1-scope)).
**Date:** 2026-11-19
**Base:** [`initial_spec_functional.md`](../initial_spec_functional.md) v1.3 and [`boardsettings_spec_functional.md`](../boardsettings_spec_functional.md); this feature changes them as listed in [§5](#5-changes-to-the-initial-specification) and [§6](#6-changes-to-the-board-settings-specification).
**Companion:** [`agentinstructions_spec_technical.md`](agentinstructions_spec_technical.md) — how kankan would implement this feature.
**Related documents:** [`kanban_requirements.md`](../kanban_requirements.md) (no new endpoint, [technical §7](agentinstructions_spec_technical.md#7-trello-usage)).

**Split rule.** This file describes what users and agents observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

The user gives the agents standing instructions on **how** to work, per role and optionally per agent type, from the board: for example "when reviewing, consult your advisor before integrating" for Claude only, or "write commit messages in Italian" for every implementer. The instructions are changed from Trello while agents run, without a commit on the integration branch and without touching the project's own instruction files.

The feature adds two setting cards to the `kankan` list ([§2](#2-instruction-cards)), delivers their text to the agent with the task ([§3](#3-delivery-to-the-agent)) and defines how it ranks against the skill ([§4](#4-precedence)).

The instructions shape how the agent works; they never change what kankan enforces. A policy that kankan must enforce (for example a review that integrates without `verify`) needs a setting of its own, not free text ([§8](#8-out-of-scope)).

**When this is worth implementing.** The repository's `CLAUDE.md` (read by Claude Code only) and `AGENTS.md` (read by Codex) can already hold phase-specific lines such as "when your kankan task `kind` is `review`, …". This feature is worth its cost when instructions must change from the board while agents run, must stay out of the project's instruction files (which humans use too), or must sit on the board next to the model and effort they go with.

## 2. Instruction cards

**Cards.** Two description-value setting cards ([boardsettings §2](../boardsettings_spec_functional.md#2-board-settings)), **`Implementation Instructions`** and **`Review Instructions`**, hold the instructions for every task of the `implementation` role (merge-request cards included) and of the `review` role. Their default is empty: no instructions. kankan creates them empty, with the help comment of [boardsettings §2](../boardsettings_spec_functional.md#2-board-settings) explaining the format below and the precedence of [§4](#4-precedence).

**Agent sections.** The description is Markdown text in two parts:
- the **shared part**: the text before the first agent heading, given to every agent type;
- **agent sections**: each starts with a line `## agent: <type>`, where `<type>` is an agent type (`claude`, `codex`, or any type added later; case is ignored), and runs to the next agent heading or to the end of the description. Its text is given only to seats of that agent type, after the shared part.

Other headings (`## Tests`, `### Style`) are ordinary text of the part they are in. The reserved `agent:` prefix keeps a misspelt type (`## agent: cluade`) from being silently delivered to every agent: it is an error.

Example: a `Review Instructions` card with "Keep review fixes small." followed by `## agent: claude` and "Consult your advisor before `kankan integrate`." gives Claude reviewers both lines and Codex reviewers the first one only.

**Validity.** Invalid, with the behaviour of [boardsettings §2](../boardsettings_spec_functional.md#2-board-settings) (the last valid value stays in force, `Error`, a comment naming the problem):
- an agent heading naming an unknown type ("`cluade` is not an agent type (known: claude, codex); still using the previous instructions");
- two sections for the same type;
- a description longer than 4,000 characters (the agent reads it at every task start).

An empty agent section is valid and gives that type the shared part only. When an agent type disappears from the adapters, a card with its section becomes invalid when it is judged again.

**When a change applies.** As for every board setting: from the next task start; a running task keeps the text it started with.

**`kankan status`** shows, per card, whether instructions are set, their length and which agent sections exist (e.g. "Review Instructions: set, 212 characters, sections: claude"), not the text itself.

## 3. Delivery to the agent

`kankan task --json` gains the field **`board_instructions`**: the shared part followed by the section for the seat's agent type, or `null` when that text is empty. It is present in `impl` and `review` tasks (from the card of the task's role), and `null` in `hold_wip` tasks. The pasted prompt stays the fixed one-liner of [initial §13.1](../initial_spec_functional.md#131-skill): the instructions are never pasted into the agent's terminal.

The text is the one in force when the task was assigned. A task resumed by a new session of the seat ([initial §15.2](../initial_spec_functional.md#152-situations-and-behaviour)) gets the text in force at its new assignment, as for model and effort.

The claim comment notes when instructions are in force (e.g. "reviewing with claude `opus`/`high`, with board instructions"), so a card's history shows which reviews followed them.

## 4. Precedence

The skill tells the agent that `board_instructions` come from the board's owner and:
- **take precedence over the skill's working steps**: how to implement, which tests to run itself, how deeply to review, how to write commits and notes;
- **never override the kankan protocol**: the commands and their order (`sync`, `verify`, `integrate`, `done`, `yield`), the statuses and what to do on each, and the rules on what never to do ([initial §13.1](../initial_spec_functional.md#131-skill) items 5–8);
- when following them would mean skipping or reordering a kankan command, or breaking one of those rules, the agent follows the protocol and records the conflict with `kankan note`, without stopping;
- an instruction the agent cannot carry out (for example a tool its CLI does not have) is noted with `kankan note` and skipped; it is never a reason for `ask`, `fail` or `block`.

Unlike the card's description, comments and attachments, which are the user's data about the card, `board_instructions` are instructions to the agent. They do not change how the agent treats the card's content.

## 5. Changes to the initial specification

How this feature changes [`initial_spec_functional.md`](../initial_spec_functional.md), by its section numbers. That file is not edited.

**§13.1 Skill.** Add to the list, after item 1:

> 1a. follow the board instructions of the task (`board_instructions`) for how to work, ranked as in [agent instructions §4](proposals/agentinstructions_spec_functional.md#4-precedence): above the skill's working steps, never above the kankan commands, statuses and rules.

**§14.2 Notifications.** No change: an invalid instruction card is reported with a mention like any setting card.

## 6. Changes to the board settings specification

How this feature changes [`boardsettings_spec_functional.md`](../boardsettings_spec_functional.md), by its section numbers. That file is not edited.

**§1 Scope.** After "never move to the board: anyone who can edit the board can change a board setting.", add:

> Free-text instructions for the agents are the one exception to "policy choices only": they are delivered to agents that run in full-access mode, so whoever can edit the board can instruct the agents, as card descriptions already allow ([agent instructions §1](proposals/agentinstructions_spec_functional.md#1-scope)). kankan never runs that text itself.

**§2 Board settings, "Description values".** Replace "Each such setting has a fixed set of allowed values or a bounded number; matching ignores case." with:

> Each such setting has a fixed set of allowed values, a bounded number, or bounded free text with a format of its own (the instruction cards, [agent instructions §2](proposals/agentinstructions_spec_functional.md#2-instruction-cards)); matching of fixed values ignores case.

## 7. Acceptance tests

1. Empty instruction cards: `task --json` has `board_instructions: null` in both phases.
2. A `Review Instructions` card with a shared part and a `claude` section: a Claude reviewer gets both, a Codex reviewer the shared part only, implementers get nothing.
3. `## agent: cluade`: `Error` and a comment naming the known types; the previous instructions stay in force; fixing the heading removes `Error`.
4. Two `## agent: claude` sections, and a description over 4,000 characters: invalid as in 3.
5. An edit while a review runs: the running task keeps its text; the next task gets the new one.
6. A heading `## Tests` in the shared part is delivered as text, not treated as a section.
7. With live agents, a review instruction that conflicts with the protocol ("do not run `kankan verify`"): the agent still runs `verify` and `integrate` and leaves a `kankan note` about the conflict.
8. `kankan status` shows the length and sections, not the text.

## 8. Out of scope

- Q&A seats (role `questions`): their answers and handoffs follow the skill only. A third card can be added if a need appears.
- Sections per model or effort, per card class, or per card.
- Policies kankan enforces (skipping `verify`, other integration rules): each needs a setting with fixed values.
- Checking that the agent followed the instructions.
- Pasting the instructions into the agent's prompt or system prompt.
