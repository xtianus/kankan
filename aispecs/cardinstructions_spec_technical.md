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

- `parse(desc, agent_types) -> Parsed`: `Parsed` holds `spec` (the text before the first agent heading, stripped of trailing whitespace), `sections` (a list of `(target, phase, heading_line, body)` in description order, `phase` `None` for both) and `problems` (a list of messages; empty when valid).
- `for_seat(parsed, agent_type, phase) -> Optional[str]`: the sections matching `all`, `all P`, `T`, `T P`, joined as in [functional §3](cardinstructions_spec_functional.md#3-delivery-to-the-agent), or `None`.
- `help_text(agent_types) -> str`: the description of the `Instructions` card ([§6](#6-the-instructions-card)).

Rules of `parse`:
- line endings are normalized to `\n`;
- fenced code blocks (a line starting with three or more backticks or tildes, up to the matching closing fence) are skipped when looking for headings;
- a line matching `^#{1,6}[ \t]+agent:` (case-insensitive) is an agent heading candidate; its remainder is split on whitespace and lowercased; one or two words are accepted, the first a target in `agent_types ∪ {"all"}`, the second `implementation` or `review`;
- a candidate that does not parse is a problem, never text: that is what turns a misspelling into an error instead of a section nobody gets;
- a duplicate `(target, phase)` is a problem;
- all problems are collected, so one comment names them all.

`all` is reserved: an adapter type named `all` is refused when the adapters are loaded ([initial T§8.7](initial_spec_technical.md#87-adapters)), with `config_error`.

## 3. Check at claim

`workflow.claim_impl` and the review claim call `parse` on the card's description from the board read (B3), before the branch checks and before any ref transition, so a refused card leaves no state:

- **invalid:** `labels.set_label(project, card.id, cfg.label_error, "sections", seat, comment=…)` with `messages.SECTIONS_INVALID` (the problems and the valid targets and phases), op-id `sections-<card id>-<sha1 of the normalized description, 12 hex>`, so each wrong version is commented once and a retry after a timeout finds its comment; then `KankanError(Status.INELIGIBLE, …)`. `choose` adds the card to `skip` and takes the next one, as for a branch conflict.
- **valid:** if an `Error` with cause `sections` is recorded for the card in any seat's state, it is cleared (`labels.clear_label` with cause `sections`) before the claim goes on.

The agent types are `adapter_types(paths)`, the same list the model and effort cards use.

**Clearing after a fix.** On every board read of its cycle (`workflow.tick` after convergence, and `periodic_board_check` while busy), a launcher re-parses the cards on which its own seat state records an `Error` with cause `sections`, and clears the label when the description is valid or the card left the claimable lists (moved to `Done`, archived). The card's later claim also clears it ([above](#3-check-at-claim)), which covers a seat that no longer runs.

## 4. Delivery

`agentcmds.task` for `impl` and `review`:
- parses `card.desc` with the configured agent types;
- valid: `desc` is `parsed.spec`, `card_instructions` is `for_seat(parsed, lease.type, phase)` with phase `implementation` or `review` from the task kind;
- invalid (edited during the task): `desc` is the whole description, `card_instructions` is `None`, `card_instructions_error` is the problems joined; nothing is labelled.

Q&A tasks (`_qa_task_data`) and `hold_wip` tasks are unchanged. The handoff of a Q&A card to implementation needs no change: the implementation claim checks the description like any other.

## 5. Skill

[`SKILL.md`](../kankan/skill/SKILL.md) changes:
- the field table of "Read the task": `desc` becomes "the card's specification (agent sections removed)", plus the rows `card_instructions` ("instructions on how to work for your agent type and phase, or `null`: see Card instructions") and `card_instructions_error` ("present when the description's agent headings are invalid: go on with `desc` and `kankan note` the problem");
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

## 9. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md), by its section numbers. That file is not edited.

**§8.5 Cycle and signals.** Add: on each board read the launcher clears the `Error` labels with cause `sections` it set on cards whose agent headings are now valid ([card instructions T§3](cardinstructions_spec_technical.md#3-check-at-claim)).

**§8.7 Adapters.** Add: the adapter type `all` is reserved and refused with `config_error`.

**§17 Security.** Add after "Text forwarded to the agent comes only from board members …":

> Agent sections of a card's description ([card instructions](cardinstructions_spec_functional.md#2-agent-sections)) are given only to the agents they name, and only through `kankan task --json`.

## 10. Changes to the board settings technical specification

How this feature changes [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md), by its section numbers. That file is not edited.

**§2 Board settings, "Evaluation".** Add: `boardconfig.evaluate` treats the name `Instructions` as reserved: one such card is ignored (no value, no comment, no `Error`); two or more are reported like duplicate setting titles, with `Error` on each and the same removal rules.

## 11. Tests

- Unit (`cardsections`): spec before the first heading; every target and phase combination; case and spacing; other headings kept as text; fenced blocks skipped; unknown target, unknown phase, extra words and duplicates, all reported together; `for_seat` order and filtering; `help_text` deterministic and containing every agent type.
- `workflow`: an invalid card is labelled once per description version and skipped while the next card is claimed; a valid card clears a `sections` `Error` set by another seat; the launcher clears its own after a fix; the review claim behaves the same; no ref is written for a refused card.
- `agentcmds.task`: `desc` and `card_instructions` per agent type and phase; `card_instructions_error` for a description broken mid-task; Q&A and `hold_wip` unchanged.
- `settings_setup` / `doctor` against the fake Trello: creation, rewrite after an agent type is added, no rewrite when unchanged, `--no-fix` report, recreation after deletion, no write with duplicates; `boardconfig.evaluate` ignores one `Instructions` card.
- Adapters: a type named `all` is refused.
- Live agents run: acceptance test 11 of [functional §9](cardinstructions_spec_functional.md#9-acceptance-tests).

## 12. Observed Trello behaviour

Observed on the test board on 2026-11-19 (Trello web editor, descriptions read back through the API):
- A description written through the API comes back byte for byte, headings, extra spaces and fenced blocks included.
- A heading typed in the web editor (`## agent: codex review`, then Enter) is stored as Markdown source, `## agent: codex review`, followed by a blank line.
- Lines copied from a fenced code block in the rendered view keep their `##`; pasted into another description they become a real heading, stored as `## agent: claude review`.
- Saving in the web editor a description written through the API re-serializes it: it adds a blank line after each heading and keeps the heading text as written (`### Agent:  Codex  Implementation`, with its double spaces), the other headings and the fenced block unchanged.

`parse` therefore needs no adaptation: the blank lines the editor adds are part of the section bodies and are stripped with the surrounding whitespace.
