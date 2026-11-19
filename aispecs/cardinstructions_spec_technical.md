# kankan card instructions — technical specification

**Status:** implemented locally ([plan](../aiplans/2026-11-19-cardinstructions.md)); the live agents run with the card-instructions card ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents), F§9 test 11) remains outstanding.
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
- `fingerprint(desc, agent_types) -> str`: the first 12 hex digits of the SHA-1 of the description with line endings normalized to LF, followed by the sorted agent types; it identifies what was checked in op-ids and in the local record ([§3](#3-check-at-claim)), so an adapter change that changes the problems is reported again.
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
- **valid:** if the local record ([below](#3-check-at-claim)) holds the card, its `Error` is removed in the claim itself, before the ref transition ("Clearing at a valid claim").

The agent types are `adapter_types(paths)`, the same list the model and effort cards use.

**Local record, `<gcd>/kankan/card-sections.json`.** No seat holds a refused card, so, as for the setting cards ([boardsettings T§2](boardsettings_spec_technical.md#2-board-settings), "`Error` causes"), the feedback is recorded per project, not in a seat state, and every work launcher sees it. Written atomically under the project lock like the other state files. Per card ID:
- `fingerprint`: the version reported;
- `report`: the UTC time of recording with microseconds (`20261119T141020123456`), unique per recorded version even after the file is lost; it names the comment (op-id `sections-<card id>-<report>`);
- `at`: the same time in `clock.iso` form, for `reconcile`;
- `state`: `reported`, or `clearing` while a removal is pending;
- `owned_error`, `error_write_started`, `error_confirmed`: whether kankan owns the `Error` on the card, as in `board-settings.json`;
- `pending`: the actions still to perform (`comment`, `add_error`, `remove_error`), each with an ID.

The `sections` cause is never recorded in seat states. Q&A launchers neither sweep nor drain the record.

**Feedback on an invalid version.** Under the project lock, with the description and labels of the board read taken in that lock (the claim's fresh read):
- record at this fingerprint, `reported`: nothing happens, so later cycles and other seats make no Trello read or write for a version already reported;
- record at this fingerprint, `clearing` (the user restored a reported version before the removal ran): the pending `remove_error` is dropped and the state goes back to `reported`; any `comment` or `add_error` still pending stays and is performed as usual;
- otherwise (no record, or another fingerprint): kankan records the version with a new `report`, `state: reported`, and the pending actions `comment` and `add_error`, replacing any pending action of the previous version. `owned_error` is set **provisionally** when the read shows no `Error` on the card, or kept when it was already true for an earlier version; it is false when the card already carries an `Error` kankan does not own. `error_write_started` and `error_confirmed` are kept from the previous version.

The comment is `messages.SECTIONS_INVALID`: at most 10 problems, each heading quoted up to 80 characters, then "and N more", the valid targets and phases, and a mention; it stays far below Trello's comment limit.

**Performing pending actions (`drain`).** Like the setting cards' actions (`boardsettings.drain_actions`): one action at a time, each under the project lock, rereading the record and the card (C1) first, so two launchers never perform the same action and a crash leaves it pending:
- `comment`: posted with `add_comment(..., member_id=<kankan's member ID>)` only when the card's comments (M2) hold no comment by kankan's account ending with its marker. A launcher that finds the action pending after another posted it, or after a timeout, does not post it again; a later version, even one equal to an earlier one (A→B→A), has a new `report` and so its own comment.
- `add_error`: when the reread card already has `Error` and neither `error_write_started` nor `error_confirmed` is set, the label appeared before kankan's first write and belongs to someone else: `owned_error` becomes false. When it has `Error` and one of them is set, the label is kankan's: `error_confirmed` is saved without a write. Otherwise kankan performs the POST, so the label is its own: `owned_error` and `error_write_started` are saved, the label is added (T3), then `error_confirmed` is saved. A crash after the POST leaves `owned_error` and `error_write_started` true, so the label is still known as kankan's.
- `remove_error`: performed only when the reread card confirms the clear: it is archived, outside `Backlog` and the seats' `Implemented` lists, or its description is valid at the current agent types. Otherwise the clear is cancelled: the action is dropped and the state goes back to `reported`, and the `comment` and `add_error` still pending, if any, are performed as usual (a new invalid fingerprint is reported at the next claim attempt). A record's pending `remove_error` is decided before its other actions: confirmed, it drops them unperformed, so a fixed card never gets a stale comment; cancelled, they go on. `drain` stops at the first Trello failure, so an outage does not hold the project lock for one retry budget per pending action. When confirmed, the label is removed only when `owned_error` and (`error_write_started` or `error_confirmed`) are set, the card has `Error`, and no seat state records another cause for `Error` on that card ([initial §14.1](initial_spec_functional.md#141-labels)). The record is dropped when a reread shows the label absent, or at once when kankan does not own it; a deleted card (404) drops it without writes.

A failed action stays pending and is retried at the next board read of any work launcher. `sweep` and `drain` never raise: a Trello failure is logged (`sections_action_failed`) and left for the next read, so they never keep a launcher from checking `HOLD` or user moves. The op-id also lets a comment retried after a timeout find the one already posted ([initial T§15.1](initial_spec_technical.md#151-principles)).

**Clearing (`sweep`).** On every board read of its cycle (`workflow.tick` after convergence, and `periodic_board_check` while busy), a work launcher compares each `reported` record with that read, under the project lock, and sets it to `clearing` with a pending `remove_error` when the card is in the read with a valid description or outside the claimable lists, or absent from it (archived or deleted: B3 returns only open cards). The read may be older than another launcher's report: `sweep` only schedules, and `drain` confirms with a fresh C1 read before removing anything, so an older read never clears a newer report.

**Clearing at a valid claim.** The claim's fresh read is current, so, still inside the claim's lock and before the ref transition, kankan removes the `Error` it owns (same conditions as `remove_error`) and drops the record. If the removal fails, the record stays `clearing` with a pending `remove_error` and the claim goes on; the card is then in a seat's list, so the next `drain` confirms and removes it.

When the card reaches `Done`, `clear_kankan_labels` removes kankan's labels as before; the record is then cleared as a card outside the claimable lists. A lost or corrupted record is rebuilt empty: an invalid card is reported again at its next claim attempt with a new `report`, so its comment is not mistaken for the old one; an `Error` kankan set before the loss is no longer known as its own, so the user removes it.

**Accepted limitation.** `labels.set_label` adds a label before recording its cause in the seat state, and some callers run outside the project lock. A seat setting `Error` for another cause on the same card in the instant between kankan's cause check and its removal loses that label, while its cause stays recorded in the seat state. The window is one Trello request; the branch-conflict label is set again at the card's next claim attempt, while one-shot labels (`fail`, inactivity) are not, so `reconcile` reports the recorded cause without its label.

**`kankan status`.** `workflow.waiting_reasons` parses the description of each card in `Backlog` or a seat's `Implemented` list (after the `hold` and phase checks, before `deps` and the reviewer and model checks) and gives reason `invalid_headings` to an invalid one; `diag.status` adds to that waiting row `problems`, the `problems_text` of the parse, and `status_text` prints it after the title. The check reads only the board read `status` already makes and never the local record, so it holds after a lost record or a label removed by hand.

**`reconcile`.** The check of old `Error` and `Question` labels ([initial §15.3](initial_spec_functional.md#153-kankan-reconcile---fix)) also reads `card-sections.json`: a record with `owned_error` older than the threshold is reported as `old_label` with cause `sections`.

## 4. Delivery

`agentcmds.task` for `impl` and `review`:
- parses `card.desc` with the configured agent types;
- valid: `desc` is `parsed.spec`, `card_instructions` is `for_seat(parsed, lease.type, phase)` with phase `implementation` or `review` from the task kind;
- invalid (including a resumed task): `desc` is still `parsed.spec`, `card_instructions` is `None`, `card_instructions_error` lists the line numbers and reasons, without section bodies;
- `card_instructions_error` is always present, `None` on a valid read, like `card_instructions` and `handoff`; the existing field `instructions` is unchanged in both cases; `task` never labels the card or writes the local record.

`hold_wip` tasks get `desc` as `parsed.spec`, valid or not, and none of the new fields. Q&A tasks (`_qa_task_data`) are unchanged. The handoff of a Q&A card to implementation needs no change: the implementation claim checks the description like any other.

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

Everything else uses capabilities already listed: B3 (descriptions on every cycle), C1 (the card before each pending action, and archived cards), L6 and C6 (the `kankan` list and card creation), T3, M1 and M2 (`Error`, its comment and the check for a comment already posted). The new uses of C1 and M2 are added to their rows in the implementation commit that introduces them.

The implementation commit of the parser, the first code that relies on it, adds to [`kanban_requirements.md`](kanban_requirements.md) §11 (requirements outside the API) the editor behaviour of [§12](#12-observed-trello-behaviour): a heading typed or pasted in the description editor is stored as Markdown source (`## agent: …`) in the description the API returns, and lines copied from a rendered code block keep their `##`.

## 9. Changes to the initial technical specification

How this feature changes [`initial_spec_technical.md`](initial_spec_technical.md), by its section numbers. That file is not edited.

**§5.2 Local state.** Add `<gcd>/kankan/card-sections.json`, the feedback record of invalid agent headings ([card instructions T§3](cardinstructions_spec_technical.md#3-check-at-claim)).

**§8.5 Cycle and signals.** Add: on each board read the launcher performs the pending feedback of invalid agent headings and clears the `Error` kankan set for them on cards now valid or no longer claimable, from the project-level record `card-sections.json` ([card instructions T§3](cardinstructions_spec_technical.md#3-check-at-claim)).

**§8.7 Adapters.** Add: the adapter type `all` is reserved and refused with `config_error`.

**§15 Errors and recovery (`reconcile`).** Add: the check of old labels also reads `card-sections.json` ([card instructions T§3](cardinstructions_spec_technical.md#3-check-at-claim)).

**§17 Security.** Add after "Text forwarded to the agent comes only from board members …":

> Agent sections of a card's description ([card instructions](cardinstructions_spec_functional.md#2-agent-sections)) are given only to the agents they name, and only through `kankan task --json`.

## 10. Changes to the board settings technical specification

How this feature changes [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md), by its section numbers. That file is not edited.

**§2 Board settings, "Evaluation".** Add: `boardconfig.evaluate` treats the name `Instructions` as reserved: one such card is ignored (no value, no comment, no `Error`); two or more are reported like duplicate setting titles, with `Error` on each and the same removal rules, under the pseudo-key `instructions` (a key with no value, recorded in `board-settings.json` like a setting's) whose fingerprint is that of the cards' IDs; when only one remains, its `Error` is removed.

## 11. Tests

- Unit (`cardsections`): spec before the first heading; every target and phase combination; case and spacing; other headings kept as text; fenced blocks skipped; unknown target, unknown phase, extra words and duplicates, all reported together; `for_seat` order and filtering; `help_text` deterministic and containing every agent type.
- `workflow`: an invalid card is labelled and commented once per description version, by whichever seat finds it first, and skipped while the next card is claimed; later cycles and other seats make no Trello read or write for that version; after a fix any launcher clears the `Error` kankan set, at its next board read or at the claim; an `Error` already on the card is left in place and only the comment is posted; a failed comment or label write is retried at the next read; two launchers performing pending actions post one comment; a failed removal keeps the record; invalid → another invalid → fixed removes the `Error` kankan added for the first version; a reported version restored while its clear is pending keeps its label; an older board read never clears a newer report; a crash after the label POST keeps the label known as kankan's; an edit between the claim's two board reads clears the label in the claim; a lost record reports again under a new op-id while the old comment is still on the card; a comment with many problems lists 10 and the count of the rest; an archived card's record is cleared after a C1 read; an adapter change reports an unchanged invalid description again; `reconcile` reports an old `sections` `Error`; the review claim behaves the same; no ref is written for a refused card; a lost record is rebuilt empty.
- `agentcmds.task`: `desc` and `card_instructions` per agent type and phase; `card_instructions_error` for a description broken mid-task; `hold_wip` gets the specification only; Q&A unchanged.
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
