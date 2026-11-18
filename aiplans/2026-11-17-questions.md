# Questions (Q&A cards) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task, according to the execution method chosen by the user. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users talk to a dedicated Q&A agent through the comments of cards in a `Q&A` list, with follow-ups, crash-safe answers, and a handoff into the normal implement → review → integrate workflow.

**Architecture:** A new seat kind `<type>-qa` (no lists, own worktree) runs a `QaLauncher` subclass of the existing launcher. A new card ref phase `qa` holds the active conversation. `kankan/qa.py` owns the conversation policy (record, claim, end, inputs, turns, handoff transition, seat replacement), and `kankan/qa_answer.py` owns posting (chunking, pending operations, `answer`, `handoff`). Everything else is an additive change to existing modules: config, seats, cards, workflow, agent commands, diag, init/doctor and board settings.

**Tech Stack:** Python ≥ 3.10 standard library, Git ≥ 2.38, tmux ≥ 3.2, pytest and pytest-xdist, the existing fake Trello server, fake agent and private tmux servers.

**Spec:** [Questions functional specification](../aispecs/questions_spec_functional.md) (F) and [questions technical specification](../aispecs/questions_spec_technical.md) (T). They change the [initial functional](../aispecs/initial_spec_functional.md) and [initial technical](../aispecs/initial_spec_technical.md) specifications and the [board settings](../aispecs/boardsettings_spec_technical.md) specification as listed in F§6–7 and T§10–11. Do not edit the initial or board settings files. [Kanban requirements](../aispecs/kanban_requirements.md) is revised in the implementation commit (T§8).

**Status:** implemented locally (all 11 tasks, plus the fixes of the final whole-branch reviews by an internal advisor and by the Codex advisor); the live agents run with the Q&A dialogue ([developer manual §3.5](../docs/developer-manual.md#35-live-run-with-real-agents)) remains outstanding. Reviewed before execution by an internal advisor and by the Codex advisor; their findings are incorporated below.

## Global Constraints

- Runtime code uses the standard library only (Python ≥ 3.10), four-space indentation and English throughout. User-facing texts live in [messages.py](../kankan/messages.py).
- Seats:
  - the Q&A seat is named `<type>-qa`, and seat names match `^[a-z]+-([0-9]+|qa)$`;
  - at most one `qa` seat is registered;
  - `SeatConfig.role` is `work` (the default when absent) or `qa`.
- `config.json` gains `qa_list` (an optional list ID) and `qa_poll_seconds` (a positive integer, default 30). A missing or archived `qa_list` is never a `config_error` for other commands or seats.
- Comments:
  - every comment kankan posts is at most 16 384 characters, measured on the exact text sent to Trello (the `n/m` prefix, the body, the last chunk's mention, and `\n\n` plus the marker that `add_comment` appends);
  - long texts split at the last blank line before the limit, else at the last newline, else hard.
- Markers: an answer chunk uses `kankan:qa-<conversation>-<turn>-<i>`, a handoff chunk `kankan:qh-<conversation>-<turn>-<i>`.
- Every Q&A hold of the project lock is limited to at most one card read, one Git transaction, and one move with its verification read (T§3). The project lock is never held while posting answer chunks, downloading attachments or waiting for the agent.
- Lock order: conversation lock (`qa/<conversation>/op.lock`), then project lock, then seat-state lock.
- Error precedence for Q&A agent commands:
  1. no task: `no_work`;
  2. wrong command for the task kind: `usage_error`;
  3. ref missing, another owner, another conversation, or a task marked `ended`: `not_owner`;
  4. stale turn, dirty checkout or moved `HEAD`: `needs_action`.
- Trello dates decide `new` after a conversation restart, never the local clock.
- The Q&A seat never moves the integration branch, never creates a card branch and never holds an `impl` or `review` ref.
- Tests: each task runs its focused modules. The full suite (`setsid -w .venv/bin/python -m pytest -q -n auto`) runs at the milestones after Tasks 4, 7, 10 and 11.
- Commits follow the project rules: one final implementation commit after verification, including the [kanban_requirements.md](../aispecs/kanban_requirements.md) revision, then a push to `feature/questions`. No per-task commits and no PR.

## Review Focus

1. A delayed `answer`, `fail` or no-signal from an older turn arrives after a newer turn has started in the same conversation: it must neither complete nor fail the newer turn (Tasks 6, 8).
2. The user moves, archives or `HOLD`s the active card:
   - between two chunks of a long answer;
   - between `handoff`'s summary and its move;
   - while `task` downloads attachments.

   In each case no later chunk is posted, no move happens, and the agent gets `not_owner` (Tasks 6, 7, 8).
3. Crashes must lose and duplicate nothing:
   - at every point of `answer`: after a chunk, before the `covered` transaction, after it and before the signal;
   - at every point of `handoff`: before or after its transaction, its move or its confirmation;
   - between saving `turn.json` and the pane write.

   No duplicate chunk, no lost input, no double move, and a later move by the user is never replayed (Tasks 4, 6, 9).
4. A user starts `claude-qa` while a stopped or dead `codex-qa` (with or without a seat ref) holds a conversation and another card is on top of `Q&A`, and the old supervisor relaunches meanwhile. The new seat continues that conversation, and the old seat never re-registers (Task 10).
5. Existing configurations (no `qa_list`, seats without `role`) and the work seats keep working unchanged. This covers:
   - `up claude=2` counts;
   - `active_types`;
   - board-settings version checks of work seats;
   - an archived `Q&A` list;
   - a missing `questions` seed

   (Tasks 2, 3, 10).

---

## Existing implementation and file responsibilities

Inspected baseline, by module:

- [refstore.py](../kankan/refstore.py): validates seat names with `^[a-z]+-[0-9]+$`. `Verify(ref, None)` already checks that a ref is absent.
- [seats.py](../kankan/seats.py):
  - `_candidates` and `acquire` parse seat numbers;
  - `_occupy` calls `ensure_created`, which creates three Trello lists;
  - `release` deletes the seat ref;
  - `owned_cards` defaults to `impl`/`review`.
- [config.py](../kankan/config.py): `SeatConfig` requires list IDs, and `check_lists` refuses any closed list in `workflow_lists()`.
- [cards.py](../kankan/cards.py):
  - `converge` holds the project lock across every ref;
  - it moves a card only from its recorded `from_list`, then `confirm_move` drops that field.
- [trello.py](../kankan/trello.py):
  - `move_card` always moves to the top;
  - `card()` reads attachments with `attachment_fields=url`;
  - `add_comment` appends `\n\n` and the marker.
- [agentcmds.py](../kankan/agentcmds.py):
  - `AgentContext.task` raises `no_work` when there is no task;
  - `note` and `_ask_or_fail` check `OWNED_PHASES`;
  - `_ask_or_fail` moves `comment_cursor` and derives its op-id from the task ID;
  - `signal` writes unconditionally.
- [launcher.py](../kankan/launcher.py):
  - `step` dispatches only the known states;
  - `periodic_board_check` and `check_user_move` paste `SUSPEND_REQUEST`, which says "commit WIP";
  - `restart_step` calls `activate`, and through it `workflow._validate_prepared`, which accepts only `impl`/`review`;
  - `flush_assignment` calls `refresh_task`;
  - `failed_start` reads `prepared['ref_oid']`;
  - `take_signal` checks only `task_id`;
  - the inactivity op-id is per task, and `settings_step` compares every role's pair.
- [boardconfig.py](../kankan/boardconfig.py): hard-codes the roles `implementation` and `review`.
- [agents_cmd.py](../kankan/agents_cmd.py): `live_counts` counts seats by type.
- [diag.py](../kankan/diag.py):
  - `seat_remove` archives the seat's lists;
  - `reconcile` reports `done_without_merge` and list mismatches.
- [supervisor.py](../kankan/supervisor.py): passes an explicit `--seat` through unchanged and stops on `FATAL_EXIT`.

| Files | Responsibility |
|---|---|
| Modify [trello.py](../kankan/trello.py), [fake_trello.py](../tests/fakes/fake_trello.py), [kanban_requirements.md](../aispecs/kanban_requirements.md), [test_trello_live.py](../tests/live/test_trello_live.py) | C1 attachment fields, attachment `date`, `pos=bottom` moves, exact comment text, comment length in the fake. |
| Modify [config.py](../kankan/config.py), [refstore.py](../kankan/refstore.py), [seats.py](../kankan/seats.py), [diag.py](../kankan/diag.py), [init.py](../kankan/init.py), [doctor.py](../kankan/doctor.py), [agents_cmd.py](../kankan/agents_cmd.py), [workflow.py](../kankan/workflow.py) | `qa_list`, seat role, seat-name pattern, Q&A seat registration, `Q&A` list creation and restore, work-seat-only counts. |
| Modify [boardconfig.py](../kankan/boardconfig.py), [seeds.py](../kankan/seeds.py), [adapters.py](../kankan/adapters.py), [settings_setup.py](../kankan/settings_setup.py), [boardsettings.py](../kankan/boardsettings.py), [doctor.py](../kankan/doctor.py), [launcher.py](../kankan/launcher.py) | The `questions` role, derived from configuration; each seat checks only its own roles. |
| Modify [cards.py](../kankan/cards.py), [workflow.py](../kankan/workflow.py), [diag.py](../kankan/diag.py) | Phase `qa`, new blob fields, convergence of `pos: bottom`, handoff data carried into implementation claims, `qa.json` exemptions. |
| Create `kankan/qa.py` | Q&A record, conversation directory and lock, worktree reset, claim, end, inputs and `new`, turns, handoff transition, seat replacement. |
| Create `kankan/qa_answer.py` | Chunking, pending operation, `answer`, `handoff`, commit checks, pending completion. |
| Modify [agentcmds.py](../kankan/agentcmds.py), [integrate.py](../kankan/integrate.py), [cli.py](../kankan/cli.py) | `task` for `qa` and handed-off implementation tasks; command wiring and refusals; `fail`/`note`/`yield` in Q&A tasks. |
| Create `kankan/qa_launcher.py`; modify [launcher.py](../kankan/launcher.py) | `QaLauncher` (`qa_waiting`, polling, delivery, ends, restarts) and base-class hooks. |
| Modify [agents_cmd.py](../kankan/agents_cmd.py), [cli.py](../kankan/cli.py), [diag.py](../kankan/diag.py) | `agent --qa`, `up … qa=<type>`, `autostart … qa=<type>`, replacement, `seat remove <type>-qa`, `reconcile`, `status`. |
| Modify [SKILL.md](../kankan/skill/SKILL.md), [messages.py](../kankan/messages.py), [fake_agent.py](../tests/fakes/fake_agent.py), docs | Skill Q&A section, prompts, scripted Q&A agent, manuals. |

## Implementation decisions

- **Data on disk.**
  - `<gcd>/kankan/qa.json` is `{"cards": [card_id, …]}`. It is written atomically under the project lock.
  - `<gcd>/kankan/qa/<conversation>/` holds `op.lock`, `turn.json` and `pending.json`.
  - `turn.json` contains:
    - `token`: the current turn token;
    - `inputs`: `{"comments": [...], "attachments": [...]}`, the items delivered in this turn and marked `new`;
    - `context`: the newest candidate comment ID and all attachment IDs present at claim, on the first turn only;
    - `run`: the launcher run ID.
  - `pending.json` contains `kind`, `turn`, `hash`, the input IDs, the chunk texts and the IDs of the chunks already posted.
- **Ref blob.** `CardMeta` gains:
  - `conversation`;
  - `covered` (`{"comment": id | None, "attachments": [ids]}`);
  - `handoff` (comment IDs);
  - `pos` (only `"bottom"`).

  A `parked` handoff ref keeps `conversation` and `handoff`, and so does the `impl` ref claimed from it.
- **Where conversation directories are deleted.**
  - At the end of a conversation that was not handed off (T§3).
  - By `cards.delete_integrated_ref` for an `integrated` ref naming a conversation.
  - By a new Q&A claim of a card whose `parked` ref names an older conversation.
  - Otherwise by `reconcile --fix`, which removes directories named by no ref. A handed-off card moved straight to `Done` keeps its directory until that happens.
- **Turn coverage.** `covered` after an answer has two parts:
  - its comment is the newest comment ID among the turn's `inputs` and `context` (or the previous `covered.comment` if that is newer);
  - its attachments are the union of the previous ones, the inputs and the context.

  On a re-taken card, the old thread therefore never comes back as a follow-up.
- **Delivery semantics** (T§5).
  - The launcher keeps a per-run set `delivered`.
  - A poll triggers a delivery when it finds a candidate that is neither covered nor in `delivered`.
  - The delivered inputs are then every candidate not covered, so a failed turn's inputs come again with the follow-up.
- **Turn synchronization.**
  - `qa.start_turn` writes `turn.json`, then the seat task's `turn`. Only after that does the caller write the prompt.
  - `answer`, `handoff`, `fail` and the inactivity `Error` carry the turn token, and `signal` payloads include `turn`.
  - `QaLauncher.take_signal` drops any signal whose `task_id` or `turn` differs from the current turn.
  - The `fail` op-id and the inactivity op-id include the turn token, and `inactivity_reported` is reset at every turn.
  - Every writer of `pending.json` holds the conversation lock.
  - The completion step (T§6 step 4) runs under the project lock. It re-validates the seat ref, the ref (phase `qa`, owner, conversation) and the `turn.json` token.
- **Ended task.**
  - `qa.end` deletes the ref and marks the seat task `ended: true`. The `closing` state clears the task.
  - Q&A commands on an ended task give `not_owner`, except `yield`, which succeeds and signals, like the existing idempotent branch of `yield_`.
- **Handoff transition.** Under the project lock, `qa.handoff_transition` does the following in order:
  1. reads the card once and requires that it is in `qa_list`, open and without `HOLD`;
  2. writes the `parked` blob (`cause: handoff`, `target_list: backlog`, `pos: bottom`, `from_list: qa_list`, `handoff`, `conversation`);
  3. runs the transaction, verifying the seat ref and the `qa` ref;
  4. moves the card to the bottom with its verification read;
  5. calls `cards.confirm_move`.

  `qa.eligible` skips `parked` refs that still have `from_list`, so an unfinished handoff is never re-claimed. Convergence completes the move:
  - each work launcher's `converge`;
  - the Q&A launcher's bounded `cards.converge_ref` (one ref per lock hold).
- **Q&A transitions** are made by `qa.py` with `refstore` transactions, not by `cards.transition`. Each one verifies the seat ref (`Verify(seat_ref, oid)`) and the expected ref value.
- **Seat-local task.** The Q&A seat state `task` holds:
  - `id`, `kind: "qa"`, `card`, `short`, `conversation`, `turn`;
  - `role: "questions"`, `model`, `effort`;
  - `base`, `reflog`, `ended`.

  `base` and `reflog` describe this seat's worktree. They are recorded:
  - at a claim;
  - when a seat takes up a conversation whose task holds no base for it (it resets its own worktree first).

  A restart of the same seat keeps them.
- **Seat replacement** (`qa.replace_seat`). Every stage is idempotent and re-run by `agent --qa` and by `QaLauncher.start`:
  1. allowed only when the old seat's liveness is `dead` or `free` and `seats.recover_dead` leaves no survivor (`unavailable` is refused);
  2. registers the new seat (`role: qa`);
  3. rewrites the `qa` ref with the new `owner` in a transaction that verifies the old ref value and the old seat ref (`Verify(seat_ref, None)` when the seat ref is absent);
  4. deletes the old seat ref, if any;
  5. removes the old worktree if clean, the old configuration entry and the old seat files.

  `seats.acquire_qa` refuses a `-qa` seat name that is not registered, with `FATAL_EXIT`, so an old supervisor relaunching after stage 5 stops.
- **Launcher hooks** (base class, with no behaviour change for work seats): `acquire()`, `after_restart_ready()` (replaces the direct `activate()` call in `restart_step`), `refresh_assignment(task)` (replaces the `workflow.refresh_task` call in `flush_assignment`), `release_on_stop(prepared)`, `own_roles()`.
- **`QaLauncher` overrides:** `step`, `start`, `idle`, `working`, `periodic_board_check`, `request_suspend`, `take_signal`, `closing`, `failed_start` (a Q&A task is never released on stop or on a failed start: the conversation is kept).
- **Launcher dispatch.** `launcher.main` builds a `QaLauncher` when the seat name ends in `-qa`. The supervisor already passes an explicit seat through.
- **Prompts** (in `messages`):
  - `QA_PROMPT`: first turn, with the token;
  - `QA_FOLLOWUP`: counts, the names of new attachments, the token;
  - `QA_RESTART`: read the thread with `kankan task --json` and continue;
  - `QA_SUSPEND`: `yield`, without the "commit WIP" text.
- **Roles.** `boardconfig.roles(paths) -> Tuple[str, ...]` returns `("implementation", "review")`, plus `"questions"` when a `qa` seat is registered. Schema, seeds, preflight, evaluation and `status` use it. A launcher compares only its `own_roles()` (`questions` for `QaLauncher`).

## Task 1: Trello capabilities for Q&A

**Files:** Modify [trello.py](../kankan/trello.py), [fake_trello.py](../tests/fakes/fake_trello.py), [test_trello.py](../tests/test_trello.py), [test_trello_live.py](../tests/live/test_trello_live.py), [kanban_requirements.md](../aispecs/kanban_requirements.md).

**Interfaces:** Produces:
- `Attachment.date: str` (`""` when absent), with `ATTACHMENT_FIELDS` adding `date`;
- `Card.attachment_items: List[Attachment]` (filled only by `card()`);
- `card()` requesting `attachment_fields=id,name,fileName,url,bytes,mimeType,idMember,isUpload,date`;
- `move_card(card_id: str, list_id: str, pos: str = "top") -> Card`;
- `trello.comment_text(text: str, op_id: str) -> str` (used by `add_comment`);
- `COMMENT_MAX = 16384`.

- [ ] Extend the fake:
  - attachment `date` on creation;
  - the C1 attachment fields in single-card reads;
  - `pos=bottom` on card moves;
  - `400` for a comment text longer than 16 384 characters.
- [ ] Write these tests:
  - `test_card_read_returns_attachment_fields`: all nine fields, and `Card.attachments` unchanged;
  - `test_attachments_include_date`;
  - `test_move_card_bottom`: the card ends up last, and the verification read is made;
  - `test_move_card_default_top_unchanged`;
  - `test_comment_text_matches_posted_text`;
  - `test_fake_rejects_long_comment`: 16 384 is accepted, 16 385 gives `TRELLO_ERROR`.
- [ ] Run `setsid -w .venv/bin/python -m pytest tests/test_trello.py -q` and confirm the failures. Then implement; `snapshot()` keeps `card_attachment_fields=url`.
- [ ] Revise the kanban requirements: M1, C1, F2, C3.
- [ ] Add a `trello_live` test covering:
  - the length limit;
  - `pos=bottom`;
  - the C1 attachment fields with `date`.
- [ ] Re-run and expect a pass.

## Task 2: Configuration, seat naming, Q&A seat registration and the `Q&A` list

**Files:** Modify [config.py](../kankan/config.py), [refstore.py](../kankan/refstore.py), [seats.py](../kankan/seats.py), [workflow.py](../kankan/workflow.py), [agents_cmd.py](../kankan/agents_cmd.py), [diag.py](../kankan/diag.py), [init.py](../kankan/init.py), [doctor.py](../kankan/doctor.py), [messages.py](../kankan/messages.py). Tests: [test_config.py](../tests/test_config.py), [test_refstore.py](../tests/test_refstore.py), [test_seats.py](../tests/test_seats.py), [test_up.py](../tests/test_up.py), [test_init.py](../tests/test_init.py), [test_doctor.py](../tests/test_doctor.py), [test_reconcile.py](../tests/test_reconcile.py).

**Interfaces:** Produces:
- in `config`:
  - `Config.qa_list: Optional[str] = None` and `Config.qa_poll_seconds: int = 30`;
  - `SeatConfig(type, in_progress="", implemented="", review="", role="work")`;
  - `Config.qa_seat() -> Optional[str]` and `Config.work_seats() -> Dict[str, SeatConfig]`;
  - `config.qa_list_open(cfg, lists) -> bool`;
- in `seats`:
  - `seats.is_qa(seat) -> bool` and `seats.qa_seat_name(agent_type) -> str`;
  - `seats.acquire_qa(project, agent_type, pids) -> Lease`;
  - `seats.ensure_created` registers a `qa` seat without lists.

- [ ] Write the configuration and naming tests:
  - an old `config.json` without `qa_list` or `role` loads;
  - `qa_poll_seconds` rejects 0, negative values and booleans;
  - `seat_lists()`, `check_lists()`, `reconcile`'s `seat_without_lists` and `seat_remove`'s list archiving ignore `qa` seats;
  - `check_lists` never raises for a missing or archived `qa_list`;
  - `validate_seat("claude-qa")` passes, while `"claude-q"` and `"claude-qa1"` fail.
- [ ] Write the seat and count tests, each with a `qa` seat registered:
  - `_candidates`, the next-number computation, the per-type cap (which excludes `qa`) and the `diag.status` sort all work;
  - `acquire(project, "claude")` never returns `claude-qa`;
  - `agents_cmd.live_counts` and `workflow.active_types` count work seats only, so `up claude=2` with a live `claude-qa` starts two work seats.
- [ ] Write the `acquire_qa` tests:
  - it registers `<type>-qa` with `role="qa"` and no Trello lists, and creates its worktree like a seat worktree;
  - it raises `config_error` without an open `qa_list`;
  - it refuses a `-qa` name that is not registered when another `qa` seat is registered.
- [ ] Write the `init` and `doctor` tests:
  - `init` finds, adopts (with the existing question) or creates `Q&A`;
  - `doctor` adds a missing `qa_list` (or reports `MANUAL` with `--no-fix`), unarchives an archived list and recreates a deleted one;
  - the `lists` check of the other workflow lists is unchanged.
- [ ] Run the listed modules and confirm the failures. Then implement:
  - `init` adds `("qa_list", "Q&A")` outside the `config_error` path for archived configured lists;
  - `doctor` gets a `qa-list` check;
  - `status` warns with `messages.QA_LIST_MISSING`.
- [ ] Re-run and expect a pass.

## Task 3: The `questions` role

**Files:** Modify [boardconfig.py](../kankan/boardconfig.py), [seeds.py](../kankan/seeds.py), [adapters.py](../kankan/adapters.py), [settings_setup.py](../kankan/settings_setup.py), [boardsettings.py](../kankan/boardsettings.py), [doctor.py](../kankan/doctor.py), [init.py](../kankan/init.py), [launcher.py](../kankan/launcher.py), [diag.py](../kankan/diag.py). Tests: [test_boardconfig.py](../tests/test_boardconfig.py), [test_seeds.py](../tests/test_seeds.py), [test_settings_setup.py](../tests/test_settings_setup.py), [test_doctor.py](../tests/test_doctor.py), [test_launcher_settings.py](../tests/test_launcher_settings.py).

**Interfaces:** Consumes Task 2. Produces:
- `boardconfig.roles(paths) -> Tuple[str, ...]`, with `schema(adapters, roles)` taking the roles explicitly at every call site;
- `seeds.preflight(paths, types, roles)`;
- `Launcher.own_roles() -> Tuple[str, ...]` (`("implementation", "review")` in the base class).

- [ ] Write these tests:
  - with no `qa` seat, nothing asks for, creates or evaluates `questions`, and every existing settings test passes unchanged;
  - with a `qa` seat registered:
    - `init` and `doctor` ask for the `questions` seed;
    - `doctor` creates the `questions` card with one checklist per agent type;
    - `effective(paths, "questions")` works;
  - an invalid or missing `questions` card never changes the `implementation`/`review` values;
  - a missing `questions` seed never fails `seeds.preflight(paths, types, ("implementation", "review"))`;
  - `preflight(..., ("questions",))` fails with the same manual step as the other roles;
  - a work launcher whose `questions` pair changed starts no version check and no restart (`settings_step` compares only `own_roles()`).
- [ ] Run the listed modules and confirm the failures. Then implement, and re-run.

## Task 4: Card ref phase `qa`, convergence and the handoff data path

**Files:** Modify [cards.py](../kankan/cards.py), [workflow.py](../kankan/workflow.py), [diag.py](../kankan/diag.py), [seats.py](../kankan/seats.py) (`owned_cards` callers). Create `kankan/qa.py`, with only the record and directory functions in this task. Tests: [test_cards.py](../tests/test_cards.py), [test_workflow.py](../tests/test_workflow.py), [test_reconcile.py](../tests/test_reconcile.py), `tests/test_qa_record.py`.

**Interfaces:** Produces:
- `Phase.QA = "qa"`, and `CardMeta` gaining `conversation`, `covered`, `handoff` and `pos`;
- `target_list_for(cfg, "qa", …) -> cfg.qa_list`, and `cause="handoff"` giving `cfg.backlog`;
- `cards.converge_ref(project, lease, ref, value, card) -> Optional[str]`: the single-ref step that `converge` uses in its loop;
- record functions, with writes under the caller's project lock:
  - `qa.record_add(paths, card_id)`;
  - `qa.record_remove(paths, card_id)`;
  - `qa.recorded(paths) -> Set[str]`;
- directory functions:
  - `qa.conversation_dir(paths, conversation) -> Path`;
  - `qa.conversation_lock(paths, conversation, timeout)`;
  - `qa.discard_conversation(paths, conversation)`.

- [ ] Write the convergence and choice tests:
  - `converge` never moves or reports a `qa` ref;
  - a `parked` ref with `pos: "bottom"` whose card is in its `from_list` is moved to the bottom and confirmed;
  - a card the user moved elsewhere meanwhile is not moved;
  - `choose` ignores `qa` refs and `Q&A` cards;
  - `waiting_reasons` reports `Q&A` cards as `hold`, `ineligible_phase` or `no_qa_seat`;
  - the recovery comment (`_comment_recovery`) also covers `qa` refs.
- [ ] Write the claim and cleanup tests:
  - `claim_impl` from a `parked` ref with `cause: "handoff"` copies `handoff` and `conversation`;
  - every implementation claim removes the card's `qa.json` entry right after its transaction;
  - `delete_integrated_ref` deletes `qa/<conversation>`.
- [ ] Write the `reconcile` tests:
  - no `done_without_merge` for cards in `qa.json`, but it is still reported once the card was claimed for implementation;
  - a `qa` ref whose card left `Q&A` while its seat is not alive is reported as `qa_conversation_ended`;
  - `--fix`:
    - deletes such a ref;
    - removes `qa.json` entries of cards holding `impl`/`implemented`/`review`/`integrated` refs, and of deleted cards;
    - removes conversation directories that no ref names.
- [ ] Run the listed modules and confirm the failures. Then implement, and re-run.
- [ ] Milestone: run the full suite and expect a pass.

## Task 5: Conversation policy (`qa.py`)

**Files:** Modify `kankan/qa.py`, [worktrees.py](../kankan/worktrees.py), [messages.py](../kankan/messages.py). Create `tests/test_qa.py`.

**Interfaces:** Consumes Tasks 1–4. Produces:
- `Inputs(comments: List[str], attachments: List[str])`;
- `qa.candidates(project, card: Card, comments: List[Comment]) -> Inputs`:
  - includes only items by `notify.input_members`;
  - excludes comments carrying any `kankan:` marker;
- `qa.to_deliver(cands: Inputs, covered: Optional[dict], delivered: Inputs) -> Optional[Inputs]`: `None` when nothing triggers, otherwise every uncovered candidate;
- `qa.first_turn(project, card, comments) -> Tuple[Inputs, Inputs]` returning `(inputs, context)`:
  - when an earlier conversation answered the card (a `qa-`/`qh-` marker), the inputs are the candidates dated after the latest such comment (by Trello dates);
  - otherwise they are all items;
- `qa.eligible(project, snap: Snapshot, refs) -> Optional[Card]`;
- `qa.claim(project, lease, card: Card, pair) -> Optional[dict]`;
- `qa.take_over(project, lease, value: RefValue, pair) -> dict`;
- `worktrees.reset_detached(worktree, commit) -> Tuple[str, int]` and `worktrees.reflog_length(worktree) -> int`;
- `qa.start_turn(project, lease, task, inputs: Inputs, context: Optional[Inputs] = None) -> str`;
- `qa.card_state(project, card_id) -> Optional[Card]`;
- `qa.active_ended(cfg, card: Optional[Card]) -> bool`;
- `qa.end(project, lease, task, *, handed_off: bool = False) -> bool`;
- `qa.end_admin(project, seat) -> bool` (for `seat remove`).

- [ ] Write the candidate tests:
  - `candidates` excludes kankan's account, non-members and marked comments;
  - `to_deliver`:
    - triggers only on items that are neither covered nor delivered;
    - includes the failed turn's inputs when it triggers;
    - ignores items covered through `context`;
  - `first_turn` uses Trello dates.
- [ ] Write the `eligible` and `claim` tests:
  - `eligible` takes the top open `Q&A` card without `HOLD` whose ref is absent or `parked` with no `from_list`;
  - it ignores dependencies;
  - `claim`:
    - holds the project lock for at most one card read and one transaction;
    - adds the `qa.json` entry before the transaction;
    - from `parked`, drops `branch` from the blob and leaves the branch untouched;
    - refuses a card that left `Q&A` since the board read;
    - deletes the directory of an older conversation;
    - resets the worktree outside the lock, discarding a stray commit and untracked files.
- [ ] Write the `end` and turn tests:
  - after `end`, the seat task is `ended`, the ref is gone, and the attachment and conversation directories are deleted (but not when `handed_off`);
  - `start_turn` writes `turn.json` before the seat state (fault point `qa_after_turn_saved`).
- [ ] Run `setsid -w .venv/bin/python -m pytest tests/test_qa.py -q` and confirm the failures. Then implement, and re-run.

## Task 6: Posting: chunking, pending operations, `answer`, `handoff`

**Files:** Create `kankan/qa_answer.py` and `tests/test_qa_answer.py`. Modify `kankan/qa.py` (`handoff_transition`) and [messages.py](../kankan/messages.py).

**Interfaces:** Consumes Task 5. Produces:
- `qa_answer.chunks(text: str, *, mention: str, op_ids: List[str]) -> List[str]`;
- `qa_answer.post(ctx, text: str, turn: str, kind: str) -> List[str]`;
- `qa_answer.answer(ctx, text, turn) -> dict` and `qa_answer.handoff(ctx, text, turn) -> dict`;
- `qa_answer.complete_pending(project, lease, task) -> Optional[str]`;
- `qa.handoff_transition(project, lease, task, comment_ids) -> None`.

- [ ] Write the chunking tests:
  - every `trello.comment_text(chunk, op_id)` is at most 16 384 characters;
  - a short text gives one comment without a prefix;
  - longer texts get `1/m … m/m`;
  - splits prefer a blank line, then a newline, then a hard cut;
  - only the last chunk carries the mention.
- [ ] Write the pending-operation tests:
  - after a crash following chunk 1 of 3 (fault `qa_after_chunk`), the same `answer` posts chunks 2–3 only;
  - another text for the same turn gives `needs_action`;
  - the pending operation's identity is `(kind, turn, hash)`;
  - two concurrent `answer` processes for one turn post each chunk once (conversation lock);
  - crashes before and after the `covered` transaction (faults `qa_before_covered` and `qa_after_covered`) are completed by `complete_pending` without reposting, and each signals once;
  - the card moved to `Done` between chunks gives `not_owner`, and no further chunk is posted.
- [ ] Write the check tests:
  - a stale `--turn` gives `needs_action`;
  - a dirty checkout gives `needs_action` listing the files;
  - `HEAD` ≠ base gives `needs_action`, with `git checkout --detach <base>` in the message;
  - a commit followed by restoring `HEAD`: the answer is posted, and the card gets `Error` with cause `qa_commit` and `messages.QA_COMMITTED`.
- [ ] Write the `covered` tests:
  - after `answer`, `covered` follows the "Turn coverage" decision;
  - a comment posted during the answer is not covered.
- [ ] Write the handoff tests:
  - the summary is posted with the `qh` marker and a mention;
  - the card moves to the bottom of `Backlog`;
  - the ref is as in the "Handoff transition" decision, and is confirmed without `from_list`;
  - the `qa.json` entry and the directories are kept;
  - a crash after the summary (fault `qa_after_handoff_summary`) is completed by `complete_pending`;
  - when the ref is already `parked` or `impl` with the same `handoff` IDs, `complete_pending` only removes `pending.json` and signals;
  - a crash after the transaction but before the move (fault `qa_after_handoff_tx`) is completed by `converge_ref`;
  - a crash after the move but before `confirm_move` is converged without moving again;
  - after a crash, a card the user dragged back into `Q&A` is not moved by convergence.
- [ ] Run the module and confirm the failures. Then implement, and re-run.

## Task 7: Agent commands and refusals

**Files:** Modify [agentcmds.py](../kankan/agentcmds.py), [integrate.py](../kankan/integrate.py), [cli.py](../kankan/cli.py), [messages.py](../kankan/messages.py). Extend [test_cli.py](../tests/test_cli.py), [test_ask.py](../tests/test_ask.py), [test_attachments.py](../tests/test_attachments.py); create `tests/test_qa_cmds.py`.

**Interfaces:** Consumes Task 6. Produces:
- CLI:
  - `kankan answer --turn <t> (<text> | --file <path>)`;
  - `kankan handoff --turn <t> (<text> | --file <path>)`.
- `agentcmds.task` for `kind == "qa"` returns its own shape:
  - `task_id`, `kind`;
  - `card` (title, desc, url, shortLink), `turn`;
  - `attachments` with `new`;
  - `comments` (`id`, `author`, `date`, `text`, `role`, `new`);
  - `worktree`, `integration`.
- For `impl` tasks, `task` adds `comments` (without `new`) and `handoff` to the existing flat fields.

- [ ] Write the refusal tests:
  - `done`, `ask`, `sync`, `verify`, `integrate` and `block` in a `qa` task give `usage_error`;
  - `answer`/`handoff` in an `impl` task give `usage_error`;
  - after the ref is deleted, every Q&A command gives `not_owner`, `yield` included only before `end` marked the task (after that, `yield` succeeds).
- [ ] Write the `fail`, `note` and `yield` tests:
  - `fail` in a `qa` task:
    - sets `Error` and mentions the user;
    - takes its op-id from the turn token (a second identical `fail` in a later turn comments again);
    - does not change `comment_cursor` or `covered`;
  - `note` works;
  - `yield` ends the conversation; the card stays in `Q&A`.
- [ ] Write the `task` tests:
  - the `new` flags come from `turn.json` and stay equal across repeated calls;
  - roles are `agent` for `qa`/`qh` markers, `kankan` for other kankan comments and `user` for members, and non-members' comments are omitted;
  - a ref deleted during the attachment download gives `not_owner`, and the directory is deleted (`Hold.GONE`);
  - an implementation task of a handed-off card returns the summary from the ref's `handoff` IDs, otherwise the latest `qh`-marked comment, otherwise `null`.
- [ ] Run the listed modules and confirm the failures. Then implement:
  - `_own_ref` gains a `phases` argument for `qa` with the `not_owner` mapping;
  - `_ask_or_fail` skips the cursor for `qa`;
  - the refusals go at the top of each command.
- [ ] Re-run.
- [ ] Milestone: run the full suite and expect a pass.

## Task 8: Q&A launcher: loop, delivery and ends

**Files:** Create `kankan/qa_launcher.py` and `tests/test_qa_launcher.py`. Modify [launcher.py](../kankan/launcher.py) (the hooks listed in the decisions, and the dispatch in `main`), [messages.py](../kankan/messages.py), [fake_agent.py](../tests/fakes/fake_agent.py), [launcher_helpers.py](../tests/launcher_helpers.py).

**Interfaces:** Consumes Tasks 5–7. Produces:
- `QaLauncher(Launcher)` with the overrides listed in the decisions;
- the states `starting`, `idle`, `working`, `qa_waiting`, `limited`, `closing`;
- `QaLauncher.poll_active()`, `QaLauncher.deliver(cls)` and `QaLauncher.end_conversation(reason: str)`.

In a fake agent step's `do` argv, `{turn}` is replaced by the token found in the received prompt, and `{file}` by a temporary file holding the step's `text`.

- [ ] Show that the hooks change nothing for work seats: [test_launcher_loop.py](../tests/test_launcher_loop.py), [test_launcher_start.py](../tests/test_launcher_start.py) and [test_launcher_settings.py](../tests/test_launcher_settings.py) pass after the hook refactor, before any Q&A code.
- [ ] Write the conversation tests (marked `slow`; fake agent and private tmux):
  - the first answer leads to `qa_waiting`, with no labels and the card still in `Q&A`;
  - a follow-up comment is delivered once within one `qa_poll_seconds`, with `FORWARDED` on the card;
  - a comment added during `working` is delivered after the answer;
  - an attachment-only follow-up is named in the prompt;
  - `fail` sets `Error`; a later follow-up removes it and is answered, with the failed turn's inputs included.
- [ ] Write the no-signal and late-signal tests:
  - an agent stopping without a signal gets `Error` (`NO_SIGNAL`), and a follow-up wakes it;
  - a second no-signal in a later turn comments again;
  - a stale signal from an older turn is dropped.
- [ ] Write the end tests:
  - the active card moved to `Done` during `working`:
    - the conversation ends at once, and a late `answer` gets `not_owner`;
    - the clear waits for "ready";
    - the next card starts after `new_conversation`;
  - `HOLD` in `qa_waiting` ends the conversation and the card stays; `HOLD` in `working` pastes `QA_SUSPEND` (never `SUSPEND_REQUEST`) and the agent's `yield` succeeds;
  - cards on `HOLD` are skipped.
- [ ] Write the pane and list tests:
  - the user typing in the pane during `qa_waiting` starts a turn, with a fresh token in `task --json`;
  - without an open `qa_list` the launcher exits with `FATAL_EXIT` at start, and after the current turn when the list is archived mid-run.
- [ ] Run `setsid -w .venv/bin/python -m pytest tests/test_qa_launcher.py -q` and confirm the failures. Then implement:
  - `idle` polls every `qa_poll_seconds`: it runs `converge_ref` on unfinished handoff refs, then `qa.eligible` and `qa.claim`;
  - `working` and `qa_waiting` poll without the project lock: one `qa.card_state` call and `comments(since=cursor)`;
  - delivery follows the base pane write rules, then `FORWARDED`, then clears the `Error` causes `fail` and `inactivity`;
  - `periodic_board_check` is replaced by the active-card poll, with no B3 read during a conversation.
- [ ] Re-run.

## Task 9: Q&A launcher: restarts, settings, stop and limits

**Files:** Modify `kankan/qa_launcher.py`, `tests/test_qa_launcher.py`.

- [ ] Write the restart tests:
  - a launcher restart in `qa_waiting` resumes the ref with a new task ID and sends `QA_RESTART`;
  - a comment added while the previous answer was being written is `new`;
  - a crash between `turn.json` and the pane write (fault `qa_after_turn_saved`) delivers the inputs after the restart;
  - a pending answer or handoff is completed before the restart prompt.
- [ ] Write the settings, stop and limit tests:
  - a changed `questions` pair restarts the agent only in `idle` or `qa_waiting`;
  - a pair change during `working` waits for the turn to end;
  - there is no fallback to another role's pair: an unstartable pair behaves as an agent dying at startup, and the conversation is kept;
  - `stop` in `qa_waiting` exits and keeps the ref;
  - a usage limit sets `Resume`, and follow-ups wait for the resumption.
- [ ] Run and confirm the failures. Then implement (`after_restart_ready` sends `QA_RESTART` when a conversation is active), and re-run.

## Task 10: Commands, replacement, admin and status

**Files:** Modify [agents_cmd.py](../kankan/agents_cmd.py), [cli.py](../kankan/cli.py), [diag.py](../kankan/diag.py), `kankan/qa.py` (`replace_seat`), [messages.py](../kankan/messages.py). Tests: [test_up.py](../tests/test_up.py), [test_cli.py](../tests/test_cli.py), [test_status.py](../tests/test_status.py), [test_admin.py](../tests/test_admin.py), `tests/test_qa.py`.

**Interfaces:**
- `agents_cmd.agent(project, agent_type, seat=None, attach=False, tmux=None, preflight=True, qa=False) -> str`;
- `agents_cmd.parse_wanted(items) -> Tuple[Dict[str, int], Optional[str]]`;
- `agents_cmd.up(project, wanted, tmux=None, qa: Optional[str] = None)`;
- `qa.replace_seat(project, agent_type) -> Optional[str]`;
- `diag.status` adds `qa: {"seat", "liveness", "launcher_state", "card", "short", "turn", "queue", "warning"}`.

- [ ] Write the replacement tests:
  - the old seat refused while `alive` or `unavailable`;
  - replacement of a stopped seat (no seat ref) and of a dead seat (seat ref present);
  - a crash after each stage (faults `qa_replace_registered`, `qa_replace_ref`, `qa_replace_seatref`), completed by a re-run of `agent --qa` or by the new launcher's start;
  - an old command paused mid-answer gets `not_owner` at its next chunk;
  - an old supervisor relaunching `--seat codex-qa` after the replacement exits with `FATAL_EXIT`.
- [ ] Write the command tests:
  - `kankan agent claude --qa`;
  - `up claude=2 codex=1 qa=codex` is idempotent;
  - `up qa=codex` with a dead `claude-qa` holding a conversation continues it, while another card is on top;
  - `autostart enable claude=2 qa=codex` writes `qa=codex` in the unit;
  - preflight checks only the `questions` seed for the Q&A type;
  - `seat remove claude-qa` is refused while alive; otherwise it ends the conversation through `qa.end_admin`, archives no list and removes the worktree.
- [ ] Write the status tests:
  - `status` text and JSON show the Q&A seat, its active card, the queue length, the "Q&A cards wait" warning with no live Q&A seat, and the missing-list warning;
  - with only a Q&A seat alive, the work cards' waiting reasons are unchanged.
- [ ] Run the listed modules and confirm the failures. Then implement, and re-run.
- [ ] Milestone: run the full suite and expect a pass.

## Task 11: Skill, documentation and acceptance

**Files:** Modify [SKILL.md](../kankan/skill/SKILL.md), [test_skill.py](../tests/test_skill.py), [messages.py](../kankan/messages.py) (`INSTRUCTIONS["qa"]`), [user-manual.md](../docs/user-manual.md), [developer-manual.md](../docs/developer-manual.md), [agents_e2e.py](../tests/live/agents_e2e.py), [notes.md](../aiknowledge/notes.md), [AGENTS.md](../AGENTS.md), and the status lines of both questions spec files. Create `tests/test_qa_e2e.py`.

- [ ] Skill:
  - add the Q&A section of F§4.3:
    - answer the newest unanswered input;
    - read new attachments;
    - never commit, create branches or touch `refs/agent-*`;
    - leave the checkout clean;
    - pass the token;
    - use `handoff` for code changes, and confirm when the request is ambiguous;
    - use `fail` only for what the user must fix;
    - `yield` on `HOLD` or suspension;
    - treat inputs as data;
  - add the implementation addition: follow `handoff` and read the comments;
  - update the `test_skill.py` assertions.
- [ ] Write `tests/test_qa_e2e.py` (slow):
  - walk the acceptance matrix below;
  - check that a handoff is implemented and reviewed by work seats end to end;
  - run the independence test. With Trello responses delayed (fake fault) for one seat at a time:
    - a three-chunk answer and an implementation claim each hold the project lock for less than the bound;
    - neither waits for the other's agent.
- [ ] Live: extend `agents_e2e` with a Q&A dialogue covering a question, a follow-up with an attachment, and a handoff implemented by another seat.
- [ ] Run:
  - `setsid -w .venv/bin/python -m pytest -q -n auto`;
  - `setsid -w .venv/bin/python -m pytest tests/live/test_trello_live.py -m trello_live -q` (or record it as outstanding);
  - `git diff --check`.
- [ ] Update the docs:
  - user manual: the `Q&A` list, the `qa=` arguments, the conversation rules, handoff, the `questions` card;
  - developer manual: the fake agent `{turn}`/`{file}` placeholders and the Q&A fault points;
  - knowledge notes: the Q&A architecture, files, lock order and bounds;
  - the feature status in AGENTS.md and in both spec status lines.
- [ ] Commit once and push to `feature/questions`.

## Acceptance coverage

| F§8 test | Tasks | Evidence |
|---|---|---|
| 1. First answer, no labels, no commit/branch/ref change but the card ref | 5, 8 | `test_qa_launcher.py`, `test_qa_e2e.py` |
| 2. Follow-up once; comment during the answer delivered after | 5, 8 | launcher tests |
| 3. Attachment-only and mixed follow-ups, stable `new`, downloads | 1, 5, 7, 8 | `test_qa_cmds.py`, launcher tests |
| 4. Long answers chunked, one mention, crash-safe | 6, 9 | `test_qa_answer.py`, restart tests |
| 5. Handoff, implementer's `task`, crash completion, drag back | 4, 6, 7, 11 | handoff tests, e2e |
| 6. Ends by move/archive/delete, cleared conversation, `reconcile` | 4, 5, 8 | reconcile and launcher tests |
| 7. Late `answer` → `not_owner`; move between chunks | 6, 7, 8 | pending-operation and launcher tests |
| 8. `HOLD` ends, skipped, resumed from the thread | 5, 8 | launcher tests |
| 9. Queue order, seat isolation, independence | 2, 4, 8, 11 | workflow tests, e2e independence test |
| 10. Restart mid-conversation, `new` across restart | 5, 9 | restart tests |
| 11. `needs_action` checks, `Error` for a commit, `usage_error` refusals | 6, 7 | `test_qa_answer.py`, `test_qa_cmds.py` |
| 12. `fail`/no signal set `Error`, removed by a follow-up | 7, 8 | launcher tests |
| 13. Replacement of a dead Q&A seat, refusal for a live one | 10 | replacement tests |
| 14. `Q&A` list archived | 2, 8 | doctor and launcher tests |
| 15. `questions` changes between turns; missing seed harmless | 3, 9 | settings and restart tests |
| 16. `stop` keeps the conversation; limit and resumption | 9 | launcher tests |

Tasks run in order, 1 → 11. Writing this planning document does not by itself authorize any implementation.
