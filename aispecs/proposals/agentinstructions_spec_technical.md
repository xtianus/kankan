# kankan agent instructions from the board — technical specification

**Status:** proposal, not planned. It is not to be implemented until a real need for it is found ([functional §1](agentinstructions_spec_functional.md#1-scope)).
**Date:** 2026-11-19
**Base:** [`initial_spec_technical.md`](../initial_spec_technical.md) v1.3 and [`boardsettings_spec_technical.md`](../boardsettings_spec_technical.md); this feature changes them as listed in [§8](#8-changes-to-the-initial-technical-specification) and [§9](#9-changes-to-the-board-settings-technical-specification).
**Companion:** [`agentinstructions_spec_functional.md`](agentinstructions_spec_functional.md) — the behaviour this file implements; read it first.
**Related documents:** [`kanban_requirements.md`](../kanban_requirements.md) (unchanged, [§7](#7-trello-usage)).

**Split rule.** The functional file describes what users and agents observe; this file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

Implementation of [functional §2](agentinstructions_spec_functional.md#2-instruction-cards) (the two instruction cards and their agent sections), [functional §3](agentinstructions_spec_functional.md#3-delivery-to-the-agent) (the `board_instructions` field) and [functional §4](agentinstructions_spec_functional.md#4-precedence) (the skill's precedence rules).

## 2. Setting kind: sectioned text

`boardconfig`'s schema gains a third kind next to the fixed-value description value and the checklist value: **sectioned text**, a description value whose validator parses agent sections ([§3](#3-parsing)). The settings are `implementation_instructions` (title `Implementation Instructions`) and `review_instructions` (title `Review Instructions`), with the empty value as default.

- **Canonical value:** a mapping from `"*"` (the shared part) and each agent type present to its text, each stripped of surrounding whitespace. Comparison with the value in force uses this form, so whitespace-only edits change nothing.
- **Inputs fingerprint:** the hash of the description, as for every description value ([boardsettings T§2](../boardsettings_spec_technical.md#2-board-settings)).
- **Schema fingerprint:** the hash of the sorted agent types from `adapter_types(paths)` and the length limit, so adding or removing an adapter re-judges both cards ([functional §2](agentinstructions_spec_functional.md#2-instruction-cards), last validity paragraph).
- **Storage:** the last valid canonical value is kept in `board-settings.json` like any other; the 4,000-character limit keeps the file small.
- **"applied" comments** do not quote the text: they say "applied: instructions updated (212 characters, sections: claude), from the next task" or "applied: reset to no instructions".

## 3. Parsing

A pure function, `boardconfig.parse_sections(text, agent_types)`, returns the canonical value or the list of problems:

- Line endings are normalized to `\n`; the length check applies to the normalized description.
- A line matching `^##[ \t]+agent:[ \t]*(\S+)[ \t]*$` (case-insensitive) is an agent heading; its type is lowercased and must be in `agent_types`. Trello stores the description as Markdown source, so the heading arrives as written; a heading the user typed inside a fenced code block is still a heading (accepted limitation, kept simple).
- Any other line, headings included, belongs to the current part.
- Problems: unknown type, duplicate type, over-length. All are reported together in one comment.

## 4. Snapshot and delivery

**Snapshot.** Where `workflow` builds the task record with `role`, `model` and `effort` (the task choice, before the assignment), it adds `board_instructions`: `boardconfig.effective(paths, key)` for the role's key, reduced to the shared part joined with the seat's agent-type section by a blank line, or `None` when empty. The text is resolved for the seat's agent type at that moment, so `agentcmds.task` needs no adapter lookup. `hold_wip` and Q&A task records carry none. The record lives in the seat state, so a running task keeps its text whatever happens to the card ([functional §3](agentinstructions_spec_functional.md#3-delivery-to-the-agent)).

**Delivery.** `agentcmds.task` copies `current.get("board_instructions")` into the response for `impl` and `review`; the launcher's pasted prompt (`messages.PROMPT`) is unchanged.

**Claim comment.** `CLAIMED` and `REVIEW_CLAIMED` gain an optional suffix ", with board instructions" when the record's text is not `None`; the comment's op-id is unchanged, so a retried claim does not post twice.

**`kankan status`.** Shows for each card "none" or the length and the section list, and marks a card in error as for other settings; the text is never printed, so `status` output pasted into an issue does not leak it.

## 5. Skill

[`SKILL.md`](../../kankan/skill/SKILL.md) changes:
- the field table of "Read the task" gains the row `board_instructions`: "the board's standing instructions for this role and agent type, or `null`: see Board instructions";
- a new short section, "Board instructions", states the rules of [functional §4](agentinstructions_spec_functional.md#4-precedence): above the working steps of implementation and review, never above the commands, their order, the statuses or the "Never" section; a conflict or an instruction that cannot be carried out is noted with `kankan note` and the protocol continues;
- the sentence that card content is data, not instructions, stays as it is: board instructions are a separate input.

The skill text is shipped with kankan and installed by `skill_install`; no per-board text enters it.

## 6. Security

- kankan never interpolates the text into a shell command, a Git argument or a path; it travels only in the JSON of `task` ([initial T§17](../initial_spec_technical.md#17-security), "no Trello text interpolated into shell commands").
- Whoever can edit the `kankan` list can instruct every agent of the project at once, which is more reach than one card's description. kankan does not check who last edited the description (it would need the card's actions, a new endpoint): board membership decides, as for card descriptions. The help comment and the user manual say so.
- The precedence rules keep the instructions from overriding the "Never" section of the skill; agents still run in full-access mode, so this is guidance to the agent, not enforcement.

## 7. Trello usage

None new: description values already use B3, C6 and M1–M2 ([`kanban_requirements.md`](../kanban_requirements.md)). If the feature is implemented with a change to that, the requirements file is revised in the same commit (AGENTS.md rule).

## 8. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](../initial_spec_technical.md), by its section numbers. That file is not edited.

**§17 Security.** Add after "Text forwarded to the agent comes only from board members …":

> The instruction cards of the `kankan` list ([agent instructions](proposals/agentinstructions_spec_functional.md#2-instruction-cards)) are text given to every agent of their role; they reach the agent only through `kankan task --json`.

## 9. Changes to the board settings technical specification

How this feature changes [`boardsettings_spec_technical.md`](../boardsettings_spec_technical.md), by its section numbers. That file is not edited.

**§2 Board settings, "Schema".** Replace "kind (description value: a fixed set of values or an integer with bounds, with a default; checklist value: …)" with "kind (description value: a fixed set of values, an integer with bounds, or sectioned text ([agent instructions T§2](proposals/agentinstructions_spec_technical.md#2-setting-kind-sectioned-text)), with a default; checklist value: …)", and "A board setting never holds a command, a path or free text passed to a shell" with "A board setting never holds a command or a path, and kankan never passes its text to a shell".

## 10. Tests

- Unit: `parse_sections` (shared part only, sections, case of the type, unknown and duplicate types, other headings kept as text, length limit, CRLF); the canonical form ignores whitespace-only edits.
- `boardconfig.evaluate` with the new kind: valid, invalid with the last valid value kept, reset by an empty description, re-judged when an adapter type is removed.
- `workflow` snapshot per agent type and role; `agentcmds.task` field in `impl`, `review`, `hold_wip`; the running task keeps its text after a card edit.
- Fake agent: the claim comment suffix; `status` output without the text.
- Live agents run: the conflict case of [functional §7](agentinstructions_spec_functional.md#7-acceptance-tests) item 7.
