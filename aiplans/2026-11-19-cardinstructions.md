# Card instructions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task, according to the execution method chosen by the user. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a card's description carry agent sections (`## agent: <target> [<phase>]`) that reach only the agents they name, keep `desc` the same specification for every agent, refuse cards with misspelt headings with one `Error` and one comment per version, and keep an `Instructions` help card in the `kankan` list.

**Architecture:** A pure parser, `kankan/cardsections.py`, splits a description into specification, sections and problems. A second module, `kankan/cardsections_feedback.py`, owns the project-level record `card-sections.json` and its pending Trello actions, modelled on `boardsettings.drain_actions`. Everything else is an additive change: the claim functions call the parser, `tick` and `periodic_board_check` sweep and drain the record, `agentcmds.task` delivers the fields, `settings_setup` maintains the help card, and `boardconfig.evaluate` reserves its name.

**Tech Stack:** Python ≥ 3.10 standard library, pytest, the existing fake Trello server (`tests/fakes/fake_trello.py`).

**Spec:** [card instructions functional](../aispecs/cardinstructions_spec_functional.md) (F) and [card instructions technical](../aispecs/cardinstructions_spec_technical.md) (T). They change the initial and board settings specifications as listed in F§7–8 and T§9–10; do not edit those files. [Kanban requirements](../aispecs/kanban_requirements.md) is revised in Task 5, in the commit that first calls C9 (T§8).

**Status:** not started.

## Global Constraints

- Runtime code: standard library only, Python ≥ 3.10, four-space indentation, English. User-facing texts live in [messages.py](../kankan/messages.py).
- Agent heading: a line matching `^ {0,3}#{1,6}[ \t]+agent:` (case-insensitive), outside fenced code blocks; remainder after an optional whitespace-separated closing hash run: one word (target) or two (target, phase). Targets: `adapter_types(paths)` plus `all`; phases: `implementation`, `review` (T§2).
- `all` is a reserved adapter type: loading or listing an adapter named `all` raises `config_error` (T§2).
- `fingerprint(desc, agent_types)`: first 12 hex digits of SHA-1 over `desc` with `\r\n` and `\r` replaced by `\n`, then `"\n\0"`, then `",".join(sorted(agent_types))` (T§2).
- Comment op-id: `sections-<card id>-<fingerprint>` (T§3).
- Local record: `<gcd>/kankan/card-sections.json` (`project.paths.kankan_dir / "card-sections.json"`), written with `config.atomic_write_json` under `config.project_lock` (T§3). The `sections` cause is never written to seat states.
- Lock rule: `claim_impl` and `claim_review` run inside the project lock taken by `workflow.tick`, so code they call must not take it again; Trello writes of the record are performed only by `drain`, one action per lock hold (T§3).
- Instructions card title: `Instructions` (F§6). Doctor report: `Instructions card updated (agent types: claude, codex)`.
- `card_instructions_error` is always present in `impl`/`review` task data, `None` when valid (T§4).

## Review Focus

- A description saved by Trello's web editor (blank line after each heading, T§12) yields section bodies without leading blank lines and a `desc` that keeps the editor's blank lines (Task 1, `test_editor_blank_lines`).
- A line indented by four or more spaces (`    ## agent: cluade`) is Markdown code, not a heading, so it is neither a section nor a problem (Task 1, `test_four_space_indent_is_not_a_heading`).
- The user removes `Error` by hand while the description is still invalid: kankan does not add it back for the same version and posts no second comment; the card stays unclaimed until the description changes (Task 3, `test_user_removed_error_not_readded`).
- A description changed A (invalid) → B (valid) → A again gets a second comment and label, because the record was dropped in between (Task 3, `test_invalid_again_after_fix_is_reported_again`).
- A card carrying `Error` for another recorded seat cause (`branches`) keeps it when its headings are fixed (Task 3, `test_fix_keeps_error_of_other_cause`).

---

### Task 1: Parser and the reserved `all` type

**Files:**
- Create: `kankan/cardsections.py`
- Modify: `kankan/adapters.py` (`adapter_types`, `load_adapter`)
- Test: `tests/test_cardsections.py`, `tests/test_adapters.py`

**Interfaces:**
- Produces:
  - `PHASES = ("implementation", "review")`, `RESERVED = "all"`
  - `@dataclass(frozen=True) class Problem: line: int; heading: str; reason: str` (1-based line; `heading` is the stripped heading line; `reason` never contains body text)
  - `@dataclass(frozen=True) class Section: target: str; phase: Optional[str]; heading: str; body: str; line: int`
  - `@dataclass(frozen=True) class Parsed: spec: str; sections: tuple[Section, ...]; problems: tuple[Problem, ...]` with property `valid -> bool`
  - `parse(desc: str, agent_types: Iterable[str]) -> Parsed`
  - `for_seat(parsed: Parsed, agent_type: str, phase: str) -> Optional[str]` (only called on a valid `Parsed`)
  - `fingerprint(desc: str, agent_types: Iterable[str]) -> str`
  - `problems_text(problems) -> str`: `"; ".join(f"line {p.line}: {p.reason}")`, used by `task` (T§4)

- [ ] **Step 1: Write the failing tests** in `tests/test_cardsections.py`, with `TYPES = ("claude", "codex")`:
  - `test_no_heading_spec_is_whole_input`: `parse("a\r\nb  \n", TYPES).spec == "a\r\nb  \n"`, no sections, valid.
  - `test_example_routing`: the F§2 example; `for_seat(p, "claude", "review") == "## agent: claude review\nConsult your advisor before `kankan integrate`."`; `for_seat(p, "codex", "review") is None`; for both types in `implementation` the result is the `all implementation` section; `p.spec == "Implement feature X: … (the specification)\n"`.
  - `test_order_and_specificity_inputs`: sections `all`, `claude review`, `claude`, `all review` are returned for (`claude`, `review`) in description order, joined by `"\n\n"`.
  - `test_case_spacing_closing_hashes`: `"   ### Agent:  Claude  Review ##  "` parses to target `claude`, phase `review`.
  - `test_other_headings_are_text` and `test_fenced_heading_is_text` (backtick and tilde fences, a shorter fence inside a longer one, an unclosed fence hiding a later heading so it stays in `spec`).
  - `test_four_space_indent_is_not_a_heading` (Review Focus).
  - `test_problems_collected`: `## agent:` (no target), `## agent: cluade`, `## agent: claude deploy`, `## agent: claude review now`, and a duplicate `## agent: Claude review` after `## agent: claude review`: five `Problem`s with their line numbers; `p.valid is False`.
  - `test_heading_first_line_empty_spec`: `spec == ""`.
  - `test_empty_body_delivers_heading`: `for_seat` returns the heading line alone.
  - `test_editor_blank_lines` (Review Focus): `"S\n\n## agent: claude\n\nbody\n\n"` gives `spec == "S\n\n"` and `for_seat(...) == "## agent: claude\nbody"`.
  - `test_crlf_sections_rendered_lf`: CRLF input keeps CRLF in `spec`, sections use `\n`.
  - `test_fingerprint`: equal for CRLF and LF variants of the same text, different when the agent types change, length 12.
  - In `tests/test_adapters.py`, `test_adapter_named_all_refused`: an override file defining `"all": {...}` makes `adapter_types(paths)` raise `KankanError` with `Status.CONFIG_ERROR`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_cardsections.py tests/test_adapters.py::test_adapter_named_all_refused`
Expected: FAIL (`ModuleNotFoundError: kankan.cardsections`; no error for `all`).

- [ ] **Step 3: Implement `kankan/cardsections.py`** as a line scanner over `desc.splitlines(keepends=True)` with running offsets: track fence state (opening: up to three spaces and ≥ 3 backticks or tildes; closing: up to three spaces, ≥ as many of the same character, then only horizontal whitespace); `spec` is `desc[:offset of the first candidate]`; section bodies are rendered with LF, leading and trailing blank lines removed, original indentation kept. In `kankan/adapters.py`, `adapter_types` raises `KankanError(Status.CONFIG_ERROR, "the adapter type `all` is reserved")` when the merged keys contain `all`, and `load_adapter(paths, "all")` raises the same.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_cardsections.py tests/test_adapters.py`
Expected: PASS.

- [ ] **Step 5: Commit** `kankan/cardsections.py kankan/adapters.py tests/test_cardsections.py tests/test_adapters.py` with message `Card instructions: section parser and reserved adapter type all`.

### Task 2: Delivery in `kankan task` and the skill

**Files:**
- Modify: `kankan/agentcmds.py` (`task`, around line 246), `kankan/skill/SKILL.md`
- Test: `tests/test_card_instructions.py` (new), `tests/test_skill.py`

**Interfaces:**
- Consumes: `cardsections.parse`, `for_seat`, `problems_text` (Task 1); `adapters.adapter_types(ctx.project.paths)`; `ctx.lease.type`.
- Produces: task data keys `desc`, `card_instructions`, `card_instructions_error` for kinds `impl` and `review`.

- [ ] **Step 1: Write the failing tests** in `tests/test_card_instructions.py`, using `tests.flow.agent_ctx`, `implement` and `review_setup`:
  - `test_task_without_headings`: F§9 test 1 (`desc` whole, both fields `None`, both phases).
  - `test_task_routes_sections`: F§9 test 2 for a Claude and a Codex implementer and reviewer.
  - `test_task_keeps_spec_exact`: F§9 tests 12–14.
  - `test_task_invalid_mid_task`: F§9 tests 7 and 15: an invalid heading inserted above a section for another seat; `desc` is the prefix, `card_instructions is None`, `card_instructions_error` names the line and reason and contains neither section body; after the fix, the fields are back and `card_instructions_error is None`; no `Error` label on the card.
  - `test_qa_and_hold_wip_unchanged`: F§9 test 10 (a `qa` task returns the description whole) and a `hold_wip` task has none of the new keys.
  - In `tests/test_skill.py`, `test_skill_has_card_instructions`: SKILL.md mentions `card_instructions`, `card_instructions_error` and a "Card instructions" section.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_card_instructions.py tests/test_skill.py`
Expected: FAIL (`KeyError: 'card_instructions'`).

- [ ] **Step 3: Implement** in `agentcmds.task`: for `impl` and `review` parse `card.desc`; phase is `implementation` for `impl`, `review` for `review`; set `desc` to `parsed.spec` in both cases, `card_instructions` to `for_seat(...)` when valid else `None`, `card_instructions_error` to `None` when valid else `problems_text(parsed.problems)`. Update SKILL.md as T§5 says: the `desc` row, the two new rows, and a short "Card instructions" section with the rules of F§5 (above the working steps; never above the commands and their order, the statuses or the "Never" section; never change the specification; more specific section prevails: `T P`, `T`, `all P`, `all`; conflicts and impossible instructions go to `kankan note`, never `ask`/`fail`/`block`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_card_instructions.py tests/test_skill.py tests/test_attachments.py tests/test_qa_cmds.py`
Expected: PASS.

- [ ] **Step 5: Commit** with message `Card instructions: deliver desc, card_instructions and card_instructions_error`.

### Task 3: Feedback record and its pending actions

**Files:**
- Create: `kankan/cardsections_feedback.py`
- Modify: `kankan/messages.py`
- Test: `tests/test_cardsections_feedback.py`

**Interfaces:**
- Consumes: `cardsections.fingerprint`, `Problem` (Task 1); `labels.label_cause`, `seatstate.load_state`, `notify.mention`, `notify.kankan_member_id`, `trello.marker`; `config.project_lock`, `config.atomic_write_json`.
- Produces:
  - `path(paths) -> Path`, `load(paths) -> dict`, `save(paths, data) -> None` (shape: `{"cards": {card_id: {"fingerprint", "at", "owned_error", "error_write_started", "error_confirmed", "pending": [{"id", "kind", "text"}]}}}`; a missing or unreadable file loads as `{"cards": {}}`)
  - `record_invalid_locked(project, card, fp: str, problems) -> bool`: caller holds the project lock; returns True when a new version was recorded (pending `comment` with id `sections-<card id>-<fp>` and `add_error`), False when the record is already at `fp`.
  - `schedule_clear_locked(project, card_id: str) -> None`: caller holds the lock; replaces the record's pending actions with one `remove_error`.
  - `sweep(project, snap) -> None`: takes the lock; applies T§3 "Clearing" to every recorded card using `snap` (and C1 for cards absent from it).
  - `drain(project) -> None`: performs pending actions one per lock hold, as T§3 "Performing pending actions" says.
  - `messages.SECTIONS_INVALID = "kankan: this card is not started: {problems} (valid targets: {targets}; phases: implementation, review). Fix the heading; see the `Instructions` card in the `kankan` list."` where each problem reads `` `<heading>` (line <n>): <reason> ``.

- [ ] **Step 1: Write the failing tests** against the fake Trello (`kproject`, `fake_trello` fixtures; take `project_lock` in the test around the `_locked` calls):
  - `test_record_once_per_version`: two `record_invalid_locked` calls with the same `fp` return True then False; after `drain`, the card has one comment ending with the marker of `sections-<id>-<fp>` and the `Error` label; `owned_error` and `error_confirmed` are true.
  - `test_drain_twice_posts_one_comment`: the comment action left pending after a successful post (simulate by re-inserting it) is not posted again.
  - `test_existing_error_only_comments`: a card that already has `Error` gets the comment, `owned_error` false; after `schedule_clear_locked` and `drain` the label is still there.
  - `test_failed_write_stays_pending`: make the fake answer 500 to the label POST once; the action remains; the next `drain` completes it.
  - `test_failed_removal_keeps_record`: same for `remove_error`; the record is dropped only after the successful retry.
  - `test_sweep_valid_and_moved`: a recorded card whose description became valid, and one moved to another list, both get `remove_error`; an archived card (absent from the snapshot, C1 `closed`) too; a deleted card (C1 404) loses its record with no Trello write.
  - `test_fix_keeps_error_of_other_cause` (Review Focus): a seat state records cause `branches` for `Error` on the card; the removal leaves the label and drops the record.
  - `test_user_removed_error_not_readded` (Review Focus): after the user removes the label, `record_invalid_locked` with the same `fp` returns False and `drain` writes nothing.
  - `test_invalid_again_after_fix_is_reported_again` (Review Focus): A invalid, cleared after B valid, then A again: a second comment is posted (the marker is the same op-id; the check looks only at comments since the record was created, by filtering on the record's `at`).
  - `test_lost_record_rebuilt_empty`: a corrupted file loads as empty.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_cardsections_feedback.py`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** following `boardsettings.drain_actions` (lines 56–118): reread record and card (C1) inside each lock hold; `comment` posted only when no comment by `kankan_member_id` dated at or after the record's `at` ends with the marker; `add_error` with the `error_write_started` / `error_confirmed` / `owned_error` rules of T§3; `remove_error` only when `owned_error` and a write was started or confirmed and no seat state's `label_causes` has another cause for `cfg.label_error` on that card; drop the record after a successful removal or when there is nothing to remove. A `KankanError` from Trello leaves the action pending and is logged (`project.log("sections_action_failed", ...)`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_cardsections_feedback.py`
Expected: PASS.

- [ ] **Step 5: Commit** with message `Card instructions: project-level feedback record for invalid headings`.

### Task 4: Claim check, cycle integration and `reconcile`

**Files:**
- Modify: `kankan/workflow.py` (`claim_impl` line 125, `claim_review` line 196, `tick` line 238), `kankan/launcher.py` (`periodic_board_check` line 790), `kankan/diag.py` (old-label check around line 316)
- Test: `tests/test_card_instructions.py`, `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `cardsections.parse`, `fingerprint` (Task 1); `record_invalid_locked`, `schedule_clear_locked`, `load`, `sweep`, `drain` (Task 3).
- Produces: `workflow._check_sections(project, card) -> None`, called first in `claim_impl` and `claim_review` (inside the lock held by `tick`); raises `KankanError(Status.INELIGIBLE, "card <short> has invalid agent headings")` for an invalid card.

- [ ] **Step 1: Write the failing tests**, in the style of `tests/test_workflow.py::test_branch_conflict_sets_error_and_skips`:
  - `test_invalid_card_skipped_next_claimed`: F§9 test 4: `## agent: cluade review` on the top Backlog card; `workflow.tick` claims the second card; after the next `tick`, the first card has `Error` and one comment; no card ref for it; after fixing the description, the next `tick` removes `Error` and claims it.
  - `test_reported_once_across_cycles_and_seats`: F§9 tests 5 and 16: two seats tick several times on the same invalid description: one comment; a different invalid edit gives a second; the reporting seat is stopped, another seat's tick clears the label after the fix; an `Error` the user set before the check stays.
  - `test_review_claim_refused`: F§9 test 6, with `review_setup`; the card stays in `Implemented`.
  - `test_adapter_change_reports_again`: changing the agent types (an override adding a type) re-reports an unchanged invalid description.
  - `test_periodic_board_check_clears`: a busy launcher (`tests/launcher_helpers.py`) clears a fixed card's `Error` at `periodic_board_check`.
  - In `tests/test_reconcile.py`, `test_old_sections_error_reported`: a record with `owned_error` and `at` older than `LABEL_AGE_HOURS` gives an `old_label` issue with cause `sections`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_card_instructions.py tests/test_reconcile.py::test_old_sections_error_reported`
Expected: FAIL (the invalid card is claimed).

- [ ] **Step 3: Implement**:
  - `_check_sections`: parse with `adapter_types(project.paths)`; invalid → `record_invalid_locked(project, card, fingerprint(...), parsed.problems)` then raise; valid and the card is in `load(paths)["cards"]` → `schedule_clear_locked`.
  - `tick`: after `boardsettings.drain_actions(project)`, call `cardsections_feedback.sweep(project, snap)` and `drain(project)`; in the `except KankanError` branch for `INELIGIBLE`, call `drain(project)` (outside the lock) so the comment is posted in the same cycle.
  - `periodic_board_check`: after `boardsettings.drain_actions`, the same `sweep` and `drain`.
  - `diag.reconcile`: after the seat-state loop, report each record with `owned_error` older than the threshold as `Issue("old_label", card_id, f"sections since {at}")`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_card_instructions.py tests/test_workflow.py tests/test_review_claim.py tests/test_launcher_loop.py tests/test_reconcile.py`
Expected: PASS.

- [ ] **Step 5: Commit** with message `Card instructions: refuse invalid headings at claim, clear after a fix`.

### Task 5: The `Instructions` card

**Files:**
- Modify: `kankan/cardsections.py` (`help_text`), `kankan/messages.py`, `kankan/trello.py` (new `set_card_desc`), `tests/fakes/fake_trello.py` (PUT `desc`), `kankan/settings_setup.py`, `kankan/doctor.py` (around line 452), `kankan/boardconfig.py` (`evaluate`), `aispecs/kanban_requirements.md`
- Test: `tests/test_instructions_card.py` (new), `tests/test_boardconfig.py`

**Interfaces:**
- Consumes: `adapters.adapter_types`; `boardconfig` duplicate handling (`card_error` with `messages.SETTINGS_DUPLICATE`).
- Produces:
  - `cardsections.INSTRUCTIONS_TITLE = "Instructions"`, `help_text(agent_types: Iterable[str]) -> str`, `normalize_help(text: str) -> str` (LF, trailing horizontal whitespace per line, trailing blank lines removed)
  - `TrelloClient.set_card_desc(card_id: str, desc: str) -> Card` (`PUT /1/cards/{id}` with `desc`, C9)
  - `settings_setup.ensure_instructions_card(project, agent_types, *, write: bool) -> Optional[str]`: returns `"Instructions card created"`, `messages.INSTRUCTIONS_CARD_UPDATED.format(types=", ".join(sorted(agent_types)))`, `"Instructions card missing"`, `"Instructions card outdated"` (the last two when `write` is false), or `None` when nothing to do or when two or more such cards exist.

- [ ] **Step 1: Write the failing tests**:
  - `test_help_text_deterministic_and_complete`: `help_text(["codex", "claude"]) == help_text(["claude", "codex"])`; it contains, inside fenced blocks, `## agent: claude`, `## agent: claude implementation`, `## agent: claude review`, the three `codex` headings and the three `all` headings, the precedence sentence, the misspelt-heading sentence and the closing line `This card is maintained by `kankan doctor`: edits to it are overwritten.`; `parse(help_text(...), types)` reports no problems (all headings are inside fences).
  - `test_init_creates_card`, `test_doctor_rewrites_after_new_type_and_user_edit`, `test_doctor_no_fix_reports`, `test_deleted_card_recreated`, `test_no_rewrite_when_unchanged` (no PUT when only trailing whitespace differs): F§9 test 8.
  - `test_duplicates_not_written`: F§9 test 17: two cards get the duplicate-title `Error`; `ensure_instructions_card` writes nothing; after deleting one, the next doctor updates the survivor.
  - In `tests/test_boardconfig.py`, `test_instructions_card_is_not_unknown`: F§9 test 9.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_instructions_card.py tests/test_boardconfig.py::test_instructions_card_is_not_unknown`
Expected: FAIL.

- [ ] **Step 3: Implement**: the help text from constants in `messages.py` following F§6 (paragraphs 1–5, one fenced block per sorted type and one for `all`, each heading followed by `Instructions for <target> <phase> go here.`), dropping placeholder lines if the text would exceed 16,384 characters; `set_card_desc` and the fake's `desc` field in its card PUT handler; `ensure_instructions_card` reads the `kankan` list with `settings_cards`, creates through `_write_once` like the setting cards, compares with `normalize_help`; `settings_setup.ensure` calls it with `write=True` after the setting cards and adds its report; `doctor.run` calls it with `write=False` when `fix` is false and online, as a `WARN` check `instructions-card`; offline it is skipped. In `boardconfig.evaluate`, cards named `INSTRUCTIONS_TITLE` are kept out of `unknown`; two or more go through `card_error` with `SETTINGS_DUPLICATE` and keep the same removal rules. Add C9 to §5 of `kanban_requirements.md` and the editor behaviour line to its §11 (T§8), bump its status to v1.4.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_instructions_card.py tests/test_boardconfig.py tests/test_settings_setup.py tests/test_doctor.py tests/test_init.py tests/test_trello.py`
Expected: PASS.

- [ ] **Step 5: Commit** with message `Card instructions: Instructions help card in the kankan list (C9)`.

### Task 6: Documentation, live check and status

**Files:**
- Modify: `docs/user-manual.md` (new §6.3 after "Questions: `Q&A` cards"), `aiknowledge/notes.md`, `tests/live/test_trello_live.py`, `aispecs/cardinstructions_spec_functional.md` and `_technical.md` (status lines), `AGENTS.md` (feature list), this plan's status line
- Test: the whole suite

- [ ] **Step 1: Add the live round trip** `test_card_desc_update_round_trip` to `tests/live/test_trello_live.py`: `set_card_desc` on a `kt-` card and a C1 read give back the same text (C9 on real Trello).
- [ ] **Step 2: Write user manual §6.3 "Card instructions"**: the two parts of a description, the heading forms with one example, who gets what, what happens on a misspelt heading, the `Instructions` card; no references to specs or plans.
- [ ] **Step 3: Add to `aiknowledge/notes.md`** the durable facts: `cardsections.py` parses, `cardsections_feedback.py` owns `card-sections.json` (no seat state), the `Instructions` card is kankan-owned and reserved in `boardconfig.evaluate`.
- [ ] **Step 4: Run the full suite**

Run: `setsid -w .venv/bin/python -m pytest -q -n auto`
Expected: all pass (live tests skipped without a test board, or passing with one).

- [ ] **Step 5: Update the status lines** of both spec files and of `AGENTS.md` to "implemented locally; the live agents run (F§9 test 11) remains outstanding", and this plan's status; commit with message `Card instructions: documentation and status`.
