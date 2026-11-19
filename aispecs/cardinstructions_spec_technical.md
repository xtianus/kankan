# kankan card instructions — technical specification

**Status:** specified, not implemented.
**Date:** 2026-11-19
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3 and [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md); this feature changes them as listed in [§9](#9-changes-to-the-initial-technical-specification) and [§10](#10-changes-to-the-board-settings-technical-specification).
**Companion:** [`cardinstructions_spec_functional.md`](cardinstructions_spec_functional.md) — the behaviour this file implements; read it first.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (one new capability, [§8](#8-trello-usage)).

**Split rule.** The functional file describes what users and agents observe; this file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

Implementation of [functional §2](cardinstructions_spec_functional.md#2-agent-sections)–[§4](cardinstructions_spec_functional.md#4-errors) (parsing, delivery and errors), [functional §5](cardinstructions_spec_functional.md#5-precedence) (the skill) and [functional §6](cardinstructions_spec_functional.md#6-the-instructions-card) (the `Instructions` card).

## 2. Parsing

A new module, `kankan/cardsections.py`, standard library only, with pure functions:

- `parse(desc, agent_types) -> Parsed`: `Parsed` holds `spec` (the exact source prefix before the first candidate, or the complete input if there is none), `sections` (a list of `(target, phase, heading_line, body)` in description order, `phase` `None` for both) and `problems` (a list of messages; empty when valid).
- `for_seat(parsed, agent_type, phase) -> Optional[str]`: the sections matching `all`, `all P`, `T`, `T P`, or `None` when none matches. They are rendered in source order with LF line endings, each as its original heading line followed by its body with the original indentation and without leading and trailing blank lines (an empty body contributes its heading alone), and joined with one blank line, as in [functional §3](cardinstructions_spec_functional.md#3-delivery-to-the-agent).
- `fingerprint(desc) -> str`: the first 12 hex digits of the SHA-1 of the description with line endings normalized to LF; it identifies a version of the description in op-ids and in the local record ([§3](#3-check-at-claim)).
- `help_text(agent_types) -> str`: the description of the `Instructions` card ([§6](#6-the-instructions-card)).

Rules of `parse`:
- scan logical lines while retaining source offsets so slicing `spec` preserves original line endings and whitespace;
- fenced code blocks (up to three leading spaces and a run of at least three backticks or tildes, through a closing line with up to three leading spaces, at least as many of the same character and only trailing horizontal whitespace, or through EOF) are skipped when looking for headings; an unclosed fence therefore hides every later heading, as Markdown renders it ([functional §2](cardinstructions_spec_functional.md#2-agent-sections));
- a line matching `^ {0,3}#{1,6}[ \t]+agent:` (case-insensitive) is an agent heading candidate; strip an optional whitespace-separated closing hash run and trailing horizontal whitespace; the remainder is split on whitespace and lowercased; one or two words are accepted, the first a target in `agent_types ∪ {"all"}`, the second `implementation` or `review`;
- a candidate that does not parse is a problem, never text: that is what turns a misspelling into an error instead of a section nobody gets;
- a duplicate `(target, phase)` is a problem;
- all problems are collected, so one comment names them all.

`all` is reserved: an adapter type named `all` is refused when the adapters are loaded ([initial T§8.7](initial_spec_technical.md#87-adapters)), with `config_error`.

## 3. Check at claim

`workflow.claim_impl` and the review claim call `parse` on the card's description from the board read (B3), before the branch checks and before any ref transition, so refusal leaves the branch, ownership ref and list position intact:

- **invalid:** the card is refused with `KankanError(Status.INELIGIBLE, …)`, after the feedback below; the claim loop keeps the card in its per-cycle `skip` set and takes the next one, as for a branch conflict.
- **valid:** if the local record ([below](#3-check-at-claim)) holds the card, its `Error` is cleared as described under "Clearing" before the claim goes on.

The agent types are `adapter_types(paths)`, the same list the model and effort cards use.

**Local record, `<gcd>/kankan/card-sections.json`.** No seat holds a refused card, so, as for the setting cards ([boardsettings T§2](boardsettings_spec_technical.md#2-board-settings), "`Error` causes"), the feedback is recorded per project, not in a seat state, and every launcher sees it. Written atomically under the project lock like the other state files. Per card ID: the `fingerprint` of the version reported, whether kankan added `Error` for it (`owns_error`), and the pending Trello actions (comment, add label, remove label). The `sections` cause is never recorded in seat states.

**Feedback on an invalid version.** Under the project lock, with the description and labels of the board read that triggered the check:
- record already at this fingerprint: nothing to do (no Trello read or write), so every seat and every cycle after the first finds the version reported;
- otherwise: record the fingerprint and, as pending actions, the comment `messages.SECTIONS_INVALID` (the problems and the valid targets and phases, with a mention) with op-id `sections-<card id>-<fingerprint>`, and, when the card has no `Error` in that read, the label with `owns_error` set; when it already has one (set by the user or for another cause) `owns_error` stays as recorded for an earlier version, or false, and kankan only comments.

The lock is released, then the pending actions are performed and each is cleared under the lock when it succeeded; a failed one is retried at the next board read of any launcher. The op-id makes a comment retried after a timeout find the one already posted ([initial T§15.1](initial_spec_technical.md#151-principles)).

**Clearing.** On every board read of its cycle (`workflow.tick` after convergence, and `periodic_board_check` while busy) and at a valid claim, a launcher takes the project lock and, for each recorded card, compares it with that read:
- description now valid, or card no longer in `Backlog` or a seat's `Implemented` list (moved, archived): when `owns_error` and the read still shows `Error`, removes the label (pending action, as above), unless a seat state records another cause for `Error` on that card ([initial §14.1](initial_spec_functional.md#141-labels)); then drops the record;
- card deleted (absent from the read and a C1 read answers 404): drops the record without Trello writes;
- description still invalid at another fingerprint: handled as a new invalid version at the next claim attempt.

When the card reaches `Done`, `clear_kankan_labels` removes kankan's labels as before, and the record is dropped at the next read. A lost or corrupted record is rebuilt empty: an invalid card is reported again at its next claim attempt, and an `Error` kankan set before the loss is no longer known as its own, so the user removes it.

## 4. Delivery

`agentcmds.task` for `impl` and `review`:
- parses `card.desc` with the configured agent types;
- valid: `desc` is `parsed.spec`, `card_instructions` is `for_seat(parsed, lease.type, phase)` with phase `implementation` or `review` from the task kind;
- invalid (including a resumed task): `desc` is still `parsed.spec`, `card_instructions` is `None`, `card_instructions_error` lists the line numbers and reasons, without section bodies;
- `card_instructions_error` is always present, `None` on a valid read, like `card_instructions` and `handoff`; the existing field `instructions` is unchanged in both cases; `task` never labels the card or writes the local record.

Q&A tasks (`_qa_task_data`) and `hold_wip` tasks are unchanged. The handoff of a Q&A card to implementation needs no change: the implementation claim checks the description like any other.

## 5. Skill

[`SKILL.md`](../kankan/skill/SKILL.md) changes:
- the field table of "Read the task": `desc` becomes "the card's specification (agent sections removed)", plus the rows `card_instructions` ("instructions on how to work for your agent type and phase, or `null`: see Card instructions") and `card_instructions_error` ("not `null` when the description's agent headings are invalid: go on with `desc` and `kankan note` the problem");
- a new short section, "Card instructions", with the rules of [functional §5](cardinstructions_spec_functional.md#5-precedence);
- the sentence that comments and attachments are data, not instructions, stays.

## 6. The `Instructions` card

**Creation and update.** `settings_setup.ensure` (called by `init` and by `doctor` with fixes enabled) handles the card after the setting cards:
- it looks for open cards named `Instructions` in the `kankan` list read (L6);
- none: it creates one at the bottom of the list with `help_text(adapter_types)` as description (C6), found again by name after a create that timed out, like the setting cards;
- one: when its description, normalized (line endings, trailing whitespace per line, trailing blank lines), differs from the normalized `help_text`, it rewrites it (C9) and reports `messages.INSTRUCTIONS_CARD_UPDATED`;
- two or more: no write; the duplicate is reported by the settings evaluation ([§10](#10-changes-to-the-board-settings-technical-specification)).

`doctor --no-fix` reports "Instructions card missing" or "Instructions card outdated" and writes nothing; offline it skips the card. Launchers never call this code.

**Text.** `help_text` is built from constants in `messages.py` and the sorted agent types: the paragraphs of [functional §6](cardinstructions_spec_functional.md#6-the-instructions-card), a fenced block per agent type with its three headings, each followed by a placeholder line, and the block for `all`. The text is deterministic, so an unchanged configuration never causes a rewrite. It stays far below Trello's description limit (16,384 characters) for any reasonable number of agent types; `help_text` drops the placeholder lines if the limit would be exceeded.

## 7. Security

- Agent sections reach the agent only through the JSON of `kankan task`; kankan never interpolates them into a shell command, a Git argument or a path ([initial T§17](initial_spec_technical.md#17-security)).
- They come from the card's description, which any board member can edit, exactly like the specification: no new party can instruct the agents. The precedence rules keep them below the skill's "Never" section; agents still run in full-access mode, so this is guidance to the agent, not enforcement.
- The `Instructions` card holds only text generated by kankan; its description is never given to an agent.

## 8. Trello usage

New capability, added to [`kanban_requirements.md`](kanban_requirements.md) in the implementation commit that first calls it (AGENTS.md rule):

| ID | Level | Capability | Trello |
|---|---|---|---|
| C9 | SHOULD | Update a card's description. Used by `doctor` to keep the `Instructions` card of the `kankan` list current. Without it, the user deletes the card and `doctor` recreates it. | `PUT /1/cards/{id}` (`desc`) |

Everything else uses the reads and writes already listed: B3 (descriptions on every cycle), L6 and C6 (the `kankan` list and card creation), T3 and M1 (`Error` and its comment).

The same commit adds to [`kanban_requirements.md`](kanban_requirements.md) §11 (requirements outside the API) the editor behaviour of [§12](#12-observed-trello-behaviour): a heading typed or pasted in the description editor is stored as Markdown source (`## agent: …`) in the description the API returns, and lines copied from a rendered code block keep their `##`.

## 9. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md), by its section numbers. That file is not edited.

**§5.2 Local state.** Add `<gcd>/kankan/card-sections.json`, the feedback record of invalid agent headings ([card instructions T§3](cardinstructions_spec_technical.md#3-check-at-claim)).

**§8.5 Cycle and signals.** Add: on each board read the launcher performs the pending feedback of invalid agent headings and clears the `Error` kankan set for them on cards now valid or no longer claimable, from the project-level record `card-sections.json` ([card instructions T§3](cardinstructions_spec_technical.md#3-check-at-claim)).

**§8.7 Adapters.** Add: the adapter type `all` is reserved and refused with `config_error`.

**§17 Security.** Add after "Text forwarded to the agent comes only from board members …":

> Agent sections of a card's description ([card instructions](cardinstructions_spec_functional.md#2-agent-sections)) are given only to the agents they name, and only through `kankan task --json`.

## 10. Changes to the board settings technical specification

How this feature changes [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md), by its section numbers. That file is not edited.

**§2 Board settings, "Evaluation".** Add: `boardconfig.evaluate` treats the name `Instructions` as reserved: one such card is ignored (no value, no comment, no `Error`); two or more are reported like duplicate setting titles, with `Error` on each and the same removal rules.

## 11. Tests

- Unit (`cardsections`): spec before the first heading; every target and phase combination; case and spacing; other headings kept as text; fenced blocks skipped; unknown target, unknown phase, extra words and duplicates, all reported together; `for_seat` order and filtering; `help_text` deterministic and containing every agent type.
- `workflow`: an invalid card is labelled and commented once per description version, by whichever seat finds it first, and skipped while the next card is claimed; later cycles and other seats make no Trello read or write for that version; after a fix any launcher clears the `Error` kankan set, at its next board read or at the claim; an `Error` already on the card is left in place and only the comment is posted; a failed comment or label write is retried at the next read; the review claim behaves the same; no ref is written for a refused card; a lost record is rebuilt empty.
- `agentcmds.task`: `desc` and `card_instructions` per agent type and phase; `card_instructions_error` for a description broken mid-task; Q&A and `hold_wip` unchanged.
- `settings_setup` / `doctor` against the fake Trello: creation, rewrite after an agent type is added, no rewrite when unchanged, `--no-fix` report, recreation after deletion, no write with duplicates; `boardconfig.evaluate` ignores one `Instructions` card.
- Adapters: a type named `all` is refused.
- Regression cases: functional §9 tests 12–18, plus CRLF input, empty targets, closing hashes, a shorter fence inside a longer fence, and an unclosed fence.
- Live agents run: acceptance test 11 of [functional §9](cardinstructions_spec_functional.md#9-acceptance-tests).

## 12. Observed Trello behaviour

Observed on the test board on 2026-11-19 (Trello web editor, descriptions read back through the API):
- A description written through the API comes back byte for byte, headings, extra spaces and fenced blocks included.
- A heading typed in the web editor (`## agent: codex review`, then Enter) is stored as Markdown source, `## agent: codex review`, followed by a blank line.
- Lines copied from a fenced code block in the rendered view keep their `##`; pasted into another description they become a real heading, stored as `## agent: claude review`.
- Saving in the web editor a description written through the API re-serializes it: it adds a blank line after each heading and keeps the heading text as written (`### Agent:  Codex  Implementation`, with its double spaces), the other headings and the fenced block unchanged.

`parse` therefore needs no adaptation: the blank lines the editor adds are preserved in the specification prefix and handled by the section rendering rule in §2.
