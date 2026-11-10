# kankan board settings — technical specification

**Status:** draft, not implemented.
**Date:** 2026-11-10
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3; this feature changes it as listed in [§4](#4-changes-to-the-initial-technical-specification).
**Companion:** [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md) — the behaviour this file implements; read it first.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (the capabilities marked "planned").

**Split rule.** The functional file describes what users and agents observe; this file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

Implementation of [functional §2](boardsettings_spec_functional.md#2-board-settings) (the `kankan` list and its setting cards) and [functional §3](boardsettings_spec_functional.md#3-model-and-effort) (model and effort per role).

## 2. Board settings

Behaviour: [functional §2](boardsettings_spec_functional.md#2-board-settings).

**Schema.** `boardconfig` holds a table of the board settings: key, kind (description value: a fixed set of values or an integer with bounds, with a default; checklist value: a builder of the expected checklists and a validator, with seeds), help text. A board setting never holds a command, a path or free text passed to a shell ([initial §17](initial_spec_technical.md#17-security)). The first settings are `implementation` and `review` ([§3](#3-model-and-effort)); the mechanism's tests also use a description-value setting defined only in the tests.

**List ID.** `config.json` records the `kankan` list's ID (`settings_list`) next to the workflow lists, but it is not part of `workflow_lists()`: its absence never produces `config_error`. Launchers reload the configuration on every cycle, so a list created or replaced by `doctor` is picked up without a restart.

**Reading.** The board read of every cycle (B3 in [`kanban_requirements.md`](kanban_requirements.md)) returns every open card with name, description, labels and `dateLastActivity`, and, with `checklists=all`, every checklist of the board as a top-level array (each with `idCard`) with its items, their `state` (`complete`/`incomplete`) and position; the list read nests the checklists inside each card: the setting cards come at no extra request. Besides, every `settings_poll_seconds` (local configuration, default 30) a launcher reads only the `kankan` list's cards with their labels and checklists (L6). The time of the last such read is kept in `board-settings.json`, checked and updated under the project lock before the read; a launcher skips its read when another one read less than `settings_poll_seconds` ago, so a project makes one such read per interval however many launchers run. A failed read is logged and the next one happens after the interval.

**Quiet period.** Each read keeps the response's `Date` header. A card is judged only when `Date − dateLastActivity ≥ 10 s`; otherwise, or when either timestamp is missing or malformed (logged), it is skipped and not recorded as handled, so it is judged at a later read.

**Evaluation.** A pure function, `boardconfig.evaluate(cards, state, schema)`, takes the cards of the `kankan` list, the local state and the current schema (including the adapters' combinations, [§3](#3-model-and-effort)) and returns the effective values, the new state and the Trello actions to perform (comment, add or remove `Error`, prune). It runs under the project lock, on the read that triggered it. A read whose `Date` is older than the `Date` of the last read applied (recorded in the state) is discarded, so an overlapping slow read never applies older content over newer.

**Local state, `<gcd>/kankan/board-settings.json`.** Written atomically like the other state files. Per key: last valid value (canonical form), whether the card is currently invalid, setting card ID, the **inputs fingerprint** last handled (a hash of the description, or of the card's checklists in position order with their names, item names and states), the **schema fingerprint** (a hash of the allowed values or of the adapters' combinations), whether kankan set `Error` on the card, a change counter, help comment ID, pending actions. Per card with an unknown or duplicate name: card ID and whether kankan set `Error`. Also: the `Date` of the last read applied, the time of the last fast read, and the cards under construction ([§3](#3-model-and-effort)).

**When a setting is re-evaluated.** When its inputs fingerprint changes, when the set of cards in the `kankan` list carrying its key changes (a card added, removed, renamed, moved in or out, a duplicate resolved), or when its schema fingerprint changes (an adapter override or a kankan upgrade changes the combinations): an unchanged card is then judged again against the new schema. That is how an edit is handled once. Outcomes are transitions, not only value changes: an invalid card fixed back to the value in force is a valid transition (kankan removes its `Error` and comments "fixed; still using …") even though the value does not change. When a card with an unknown or duplicate name leaves the list or becomes valid, kankan removes the `Error` it set on it.

**Who writes.** The launcher evaluates the settings on every board read of its cycle: in `workflow.tick` after convergence (`idle`), in `periodic_board_check` while busy (`working`, `suspending`, `limited`, `waiting_human`) and on the fast settings read, in every state; while reads succeed, an edit is acknowledged within 10–40 seconds even when every seat is busy. `doctor` does the same and also creates the list and missing setting cards. With several seats, the project lock lets one launcher handle an edit and the others find it recorded. The effective value and the pending actions are recorded first; the Trello actions follow, and each is cleared when it succeeded, so a failed action is retried on the next cycle. Before adding or removing `Error`, kankan uses the labels of the read that triggered the evaluation; a label already in the wanted state is left alone. Each comment carries the marker `kankan:<op-id>`, with an op-id derived from the card ID, the inputs fingerprint and the change counter (so flipping a value back, A→B→A, gets a new comment); after a timeout, the read back of [initial §15.1](initial_spec_technical.md#151-principles) finds the comment already posted and no duplicate is written.

**Using a value.** Code that needs a board setting calls `boardconfig.effective(paths, key)`, which reads `board-settings.json` and falls back to the default (description value) or to the seeds (checklist value). The launcher reads the values it needs when it starts the agent and when it assigns a task, and keeps them for that task.

**Pruning.** After posting a comment on a setting card, kankan lists the card's comments (M2), keeps the 10 newest that carry its marker **and** were written by kankan's account (`cfg.kankan_member_id`), plus the help comment, and deletes the older such ones (M6). The help comment carries its own marker, `kankan:help`, so it is recognized even after the state file is lost. A failed deletion is logged, kept as a pending action and retried on the next cycle; it never blocks a cycle.

**Lost or corrupted state.** The file is rebuilt from the board: every card counts as changed, a valid card becomes the value, an invalid one falls back to the default or the seeds (there is no last valid value any more) and is reported again. The change counters restart from zero, so duplicate-free comments after a lost state are best effort: an "applied" comment may be repeated, or skipped when an older comment with the same op-id is found.

**`Error` causes.** The cause of an `Error` on a setting card is recorded in `board-settings.json`, not in a seat state: no seat holds setting cards. Mentions follow `notify.mention` (the board's admins for the cards kankan created, the creator for cards made by people).

**Backends.** Description values need only B3, C6 and M1–M2. Checklist values also need L6, C7 and C8 ([`kanban_requirements.md`](kanban_requirements.md)); on a backend without them, `doctor` reports that the model and effort cannot be set on the board, and the agents run with the seeds.

## 3. Model and effort

Behaviour: [functional §3](boardsettings_spec_functional.md#3-model-and-effort).

**Adapter keys.** Each adapter ([initial §8.7](initial_spec_technical.md#87-adapters)) adds:
- `models`: ordered from the most to the least powerful, each with the efforts it accepts from the highest down, e.g. `{"gpt-6.1-sol": {"efforts": ["max", "xhigh", "high", "medium", "low"]}}`. The shipped lists follow each provider's documentation; for Codex the list can come from the CLI itself (see **Model lists from the agent** below). Unlike the other adapter keys, an override of `models` **replaces** the shipped value instead of being merged into it, so the user can remove and reorder models.
- `model_args` and `effort_args`: argument templates appended to the start command, e.g. `["--model", "{model}"]` and `["--effort", "{effort}"]` for Claude Code, `["-m", "{model}"]` and `["-c", "model_reasoning_effort={effort}"]` for Codex.
- `seed`: `{"implementation": {"model": …, "effort": …}, "review": {…}}`; never shipped, only set by the user or by the seed question.
- `exit_command` (e.g. `/exit`), `exit_timeout_seconds` (default 20), `start_timeout_seconds` (default 120: the agent must reach "ready" within it).
- `models_from` (optional): where to read the models from the agent's own data instead of `models`, see below;
- `version_command` (e.g. `["claude", "--version"]`), `model_check` (argument template of a one-shot request with the pair, see below), `model_check_home` (optional: an environment variable and the files to link into a temporary directory used as that variable's value) and `model_check_timeout_seconds` (default 90).

They follow the usual override order (shipped defaults, `~/.config/kankan/agents.json`, `<gcd>/kankan/agents.json`). Loading an adapter validates that every model has a non-empty list of efforts, that each seed is one of its combinations, that the names contain no line breaks and that the templates contain exactly their placeholders; otherwise `config_error`. The adapters are reloaded at every evaluation, so a changed override is seen without restarting the launchers.

**Model lists from the agent.** Codex knows the models available to the logged-in account and their efforts: `codex debug models` prints its model catalog as JSON, refreshed by the CLI, and `$CODEX_HOME/models_cache.json` (`~/.codex` by default) holds the same entries ([`docs/adapter-notes.md`](../docs/adapter-notes.md)). The shipped Codex adapter sets `"models_from": {"command": ["codex", "debug", "models"], "file": "{CODEX_HOME}/models_cache.json"}`, so its combinations follow the account and the CLI version without a kankan release:
- **when the command runs:** in `doctor` (not with `--offline`), and when an adapter is loaded and `~/.cache/kankan/codex-models.json` was produced by another CLI version (output of `version_command`) or is missing. The result is stored there with that version; it is never run during an evaluation, which only reads the stored result. It runs outside every lock, with the model-check timeout;
- **parsing:** only four fields of each entry of the `models` array are read, strictly: `slug` (a string), `visibility`, `priority` (a number) and `supported_reasoning_levels` (a non-empty list of strings or of objects with `effort`); any other field is ignored, so added or removed fields do not matter. The models are the entries with `visibility` `list` (models hidden from Codex's own picker are left out), in ascending `priority`, taken as most powerful first; each model's efforts are its levels reversed, highest first;
- **fallback:** if the command fails or its output does not parse, kankan reads the file with the same rules; if that fails too, it uses the shipped `models` list. `doctor` prints the source used and warns when it was not the command;
- **precedence:** a `models` key in `~/.config/kankan/agents.json` or `<gcd>/kankan/agents.json` (which replaces everything, as above), then the agent's catalog, then the shipped list;
- **changes:** a new stored result with different models or efforts (new CLI version, account gaining or losing a model) changes the schema fingerprint ([§2](#2-board-settings)), so the cards are judged again and the next `doctor` adds, removes and reorders the items; a model that disappears while ticked follows the rule for combinations the adapter no longer lists (the card is invalid and the last valid value stays in force while the model check passes);
- **format changes:** for each verified Codex version a sample of the command's output is kept as a test fixture and noted in the adapter notes, so a change of format shows up as a failing test when a new version is verified.

Claude Code has no equivalent catalog; its list is the shipped `models` or the user's override.

**Checklist value.** The setting keys are `implementation` and `review`. The expected content is one checklist per adapter type, named after the type, with one item `<model> · <effort>` per combination, models in order and each model's efforts in order. `boardconfig` validates the card's checklists against it: one checklist per type, no checklist for an unknown type, exactly one ticked item per checklist, and that item one of the adapter's combinations (an unknown unticked item is only removed by the next `doctor`). Item names are compared after collapsing whitespace, so `sonnet·high` and `sonnet · high` match. The value is the map type → (model, effort); the comment of an invalid card lists every problem found. A ticked combination that the adapter no longer lists makes the card invalid, and the last valid value (possibly that same combination) stays in force: it is the only way a combination outside the adapter's lists reaches a command line ([initial §17](initial_spec_technical.md#17-security)), and only after it passed the model check.

**Model check.** A combination is used to start an agent only after it passed a check, cached in `~/.cache/kankan/model-checks.json` per agent type, CLI version (output of `version_command`), model and effort:
- the check runs `model_check` with the pair as separate arguments, without a shell, in a new empty temporary directory outside any Git repository, so no project instruction file is found; with `model_check_home`, the named variable points to a second temporary directory holding only symlinks to the listed files (Codex: `CODEX_HOME` with `auth.json`), so no user instruction file is found either; both directories are deleted afterwards;
- the shipped templates also disable everything that adds context, as measured in [`docs/adapter-notes.md`](../docs/adapter-notes.md): Claude Code `-p --no-session-persistence --setting-sources "" --settings '{"autoMemoryEnabled":false}' --tools "" --strict-mcp-config --disable-slash-commands --exclude-dynamic-system-prompt-sections`, Codex `exec --ephemeral --skip-git-repo-check`; the prompt is `Reply OK`;
- exit 0 is a pass; any other exit is a failure, recorded with the last lines of the CLI's error output (credentials never included); a timeout is not cached and counts as neither;
- it runs outside the project lock: in the launcher that applied a change, right after applying it; before a start with a pair not yet passed; and in `doctor` for every ticked pair (not with `--offline`).

A failed pair sets `Error` on the setting card with a comment naming the pair and quoting the CLI's message. While the pair in force for a type and role has failed, the launchers of that type do not take tasks of that role (task choice skips them; `status` says why); their other role and the other types continue. Ticking another combination, or a new CLI version, lifts it.

**Seed question.** `init` and `doctor` ask, before taking any lock, for each adapter type and role without a seed after the override merge. The combinations are numbered in adapter order with the keys `1`–`9`, `a`–`z`, `A`–`Z` (61 at most; an adapter with more combinations is a `config_error`). The key is read with the terminal in cbreak mode (`termios`/`tty`, restored in a `finally`), only when stdin and stdout are terminals; otherwise, and always with `--no-fix` or `--offline`, there is no question and the command reports the manual step (`needs_action`). The answer is merged into `~/.config/kankan/agents.json` under a user-level lock (`~/.config/kankan/agents.json.lock`, never held together with a project lock): the file is re-read under the lock, the seed added, other keys kept, and the result written to a temporary file and renamed atomically (created with mode 600 if missing).

**Creating and synchronizing.** `init` and `doctor` create a missing card (C6), its checklists (C7) and items with the value in force ticked (C8); they add new items unticked, delete items no longer listed and move items to the adapter's order (C8); the next evaluation judges the result like any edit. These are many calls, so they do not hold the project lock throughout (principle 5). Instead, a **construction lock** (`<gcd>/kankan/settings-construction.lock`, taken before the project lock when both are needed) makes one `doctor` at a time build cards. Under the project lock, kankan records the card as under construction in `board-settings.json`, and the evaluation skips such a card; the record is cleared when the card is complete. A card's ID is recorded as soon as the creation call returns; after a crash or a timeout before that, the next `doctor` looks for the card **by name** in the `kankan` list and adopts it, and finds checklists and items by name: every step is repeatable and adds only what is missing. A construction record whose lock is free belongs to an interrupted `doctor` and is resumed by the next one. Every creation prints the ticked values and their source (seed file, or last valid value).

**Restart.** At assignment ([initial §8.5](initial_spec_technical.md#85-cycle-and-signals)) the launcher compares the (model, effort) of the task's role for its type with the values recorded in the seat state for the running agent. The role of a task is that of the card's phase: resumed work and the WIP-only task of [initial §9.4](initial_spec_functional.md#94-hold) keep the role of the phase (`impl` or `review`). If the values differ:
1. it records in the seat state a planned restart with the new values, so the agent's exit is not taken for the end of the session ([initial §8.3](initial_spec_technical.md#83-launcher-startup));
2. it writes `exit_command` in the pane (the agent is "ready": the write rules of [initial §8.6](initial_spec_technical.md#86-pane-observation-and-writes) apply) and waits up to `exit_timeout_seconds` for the agent process to exit; known dialogs are answered as usual;
3. if the agent is still alive: `SIGTERM` to the agent, then `SIGKILL`; never to its process group or the pane session, which it shares with the launcher and the supervisor ([initial §8.3](initial_spec_technical.md#83-launcher-startup) step 5);
4. it terminates the agent's leftover descendants: the launcher is a **child subreaper** (`PR_SET_CHILD_SUBREAPER`), so descendants orphaned by the agent's exit become its children and are found among them; it terminates them (`SIGTERM`, then `SIGKILL`) and checks that none remains; if any survives, the seat is unavailable as in [initial §7.5](initial_spec_technical.md#75-liveness-and-automatic-recovery), the card being assigned gets `Error` and the launcher does not start a new agent;
5. it starts the agent again with the new arguments as in [initial §8.3](initial_spec_technical.md#83-launcher-startup) steps 4–6, updates `<seat>.proc.json` and the recorded values and clears the planned restart;
6. before assigning, it **revalidates** the task as at the start of a cycle: seat ownership, the card's ref and list, `HOLD`, a user move, a stop request and the board binding; when a check fails it follows the usual path (`yield` for `HOLD`, [initial §14.6](initial_spec_functional.md#146-cards-moved-by-the-user-safety-net) for a user move, exit for a stop request, `config_error` for the binding) instead of assigning.

The seat ref, the lease, the flock and the card refs are untouched. During the restart the launcher is in state `starting` ([initial §8.4](initial_spec_technical.md#84-state-machine)). A crash during the restart leaves the planned restart in the seat state; the next launcher clears it and starts as below.

**Every start uses the values in force at that moment.** `agent`, `up`, a launcher started by the supervisor and a restart all read the effective values when they start the agent: the role of the seat's held card (review before implementation, as in [initial §9.1](initial_spec_functional.md#91-priority)), otherwise `implementation`. A rejected pair is therefore not retried forever: ticking another item on the card is picked up by the next start.

**Failed start.** On every start (first start, supervisor restart, planned restart), an agent that exits, or does not reach "ready" within `start_timeout_seconds`, is an abnormal exit: the launcher ends it as in step 3, exits abnormally and the supervisor's backoff applies. If a card was being assigned, it gets `Error` with a comment naming the model and effort; kankan removes that `Error` when an agent of the seat reaches "ready" with other values or the same values succeed.

## 4. Changes to the initial technical specification

How this feature changes the initial behaviour described in [`initial_spec_technical.md`](initial_spec_technical.md), by initial section number. The initial file is not edited: with the feature, these statements add to or replace the ones quoted.

**§3. Principles.** Replace the text starting “6. **Stable identifiers.** Cards, lists and labels are recog…” with:

6. **Stable identifiers.** Cards, lists and labels are recognized by ID, never by name or position. The one exception is the setting cards, keyed by their name inside the `kankan` list, which is recognized by ID ([§2](#2-board-settings)).

**§5.2 Local state.** Replace the text starting “| Seat state | `<gcd>/kankan/seats/<seat>.state.json` (curre…” with:

| Seat state | `<gcd>/kankan/seats/<seat>.state.json` (current task, signals, label causes, comment cursor, test verifications, stop request, model and effort of the running agent, planned agent restart) |

**§5.2 Local state.** Add:

| Board settings state | `<gcd>/kankan/board-settings.json` ([§2](#2-board-settings)) |
| Settings construction lock | `<gcd>/kankan/settings-construction.lock` ([§3](#3-model-and-effort)) |
| Model check results | `~/.cache/kankan/model-checks.json` ([§3](#3-model-and-effort)) |
| Codex model catalog (per CLI version) | `~/.cache/kankan/codex-models.json` ([§3](#3-model-and-effort)) |
| Seed writes lock | `~/.config/kankan/agents.json.lock` ([§3](#3-model-and-effort)) |

**§5.3 Components.** Add:

| `boardconfig` | Board settings ([§2](#2-board-settings)): settings schema, evaluation of the setting cards, effective values. |

**§8.3 Launcher startup.** Replace the text starting “4. Starts the agent (the adapter's command) as a child, in t…” with:

4. Starts the agent (the adapter's command, plus the model and effort arguments of [§3](#3-model-and-effort)) as a child, in the worktree, with `KANKAN_SEAT`, `KANKAN_SESSION`, `KANKAN_PROJECT` in the environment. In the child, before starting the agent: sets `PDEATHSIG` and checks that the parent is still the launcher, otherwise exits immediately.

**§8.3 Launcher startup.** Replace the text starting “When the agent exits, the launcher terminates the remaining …” with:

> When the agent exits, the launcher terminates the remaining processes in the pane session, releases the seat and exits normally. The cards stay with the seat. The exception is a planned restart recorded in the seat state ([§3](#3-model-and-effort)): the exit is expected and the launcher starts the agent again.

**§8.5 Cycle and signals.** Replace the text starting “3. **Assignment:** the launcher generates a task ID, saves i…” with:

3. **Assignment:** if the task's role needs another model or effort than the running agent's, the launcher first restarts the agent ([§3](#3-model-and-effort)). Then it generates a task ID, saves it in the seat state, sends the adapter's new-conversation command (defaults: `/clear` for Claude Code, `/new` for Codex, whose location dialog is a known dialog answered by keeping the seat worktree) and a one-line prompt, for example `Run the current kankan task (kankan skill).` The agent reads the details with `kankan task --json`.

**§8.7 Adapters.** Replace the text starting “An adapter per agent type defines: start command, the **full…” with:

> An adapter per agent type defines: start command, the **full-access** arguments that command must contain (agents run without permission prompts or sandbox: they need git, the network and Trello; defaults `claude --dangerously-skip-permissions` and `codex --dangerously-bypass-approvals-and-sandbox`; `agent`, `up` and `doctor` refuse a command without them), an optional login check command (exit 0 = logged in; used by `doctor`), new-conversation command, exit command and its timeout, submit key, the lists of models and efforts with the arguments that select them and the seeds per role ([§3](#3-model-and-effort)), and groups of indicators — *ready*, *working*, *limit* (with the patterns to derive the resume time), *trust* (with the answer), *known dialogs* (with the answer). Each group is a list of regular expressions applied case-insensitively to the text stripped of colour codes; one match is enough, except for groups marked "all" (e.g. dialogs recognizable only by several texts together).

**§17. Security.** Replace the text starting “- Inputs validated before reaching Git or the shell; no Trel…” with:

- Inputs validated before reaching Git or the shell; no Trello text interpolated into shell commands. Model and effort reach the agent's command line only as items of the adapter's own lists, or as the last valid value after the adapter dropped it and only once the model check passed, always as separate arguments without a shell ([§3](#3-model-and-effort)). The model check runs in empty temporary directories with only symlinks to the agent's credential files; its output is stored without credentials.

**§17. Security.** Add:

- Board settings ([§2](#2-board-settings)) can be changed by anyone who can edit the board: they accept only values from a fixed set or bounded numbers, are never interpolated into a shell command, and never name a command or a path.

**§18.1 Strategy.** Replace the text starting “- **Unit:** `refstore`, `config`, `.properties` files, phase…” with:

- **Unit:** `refstore`, `config`, `.properties` files, phases and transitions, convergence, task choice, dependencies, adapter classification, `boardconfig.evaluate` (valid change, invalid value, empty description, missing card, unknown name, duplicates, case-insensitive matching, reported once, A→B→A).

**§18.1 Strategy.** Replace the text starting “- **On real Trello:** tests marked `trello_live` on a dedica…” with:

- **On real Trello:** tests marked `trello_live` on a dedicated test board, including creating and deleting a comment (M6).
- **Fake Trello server** for board settings: serves comment deletion; tests cover list and card creation, `Error` added and removed, pruning that keeps the help comment and people's comments, and recovery after failed actions and after a lost state file; checklists in the board read and the list read, card creation with ticked items, item synchronization, the 10-second rule against the response `Date`, the fast poll shared by two launchers.
- **Fake agent** for model and effort: a task needing other values triggers the exit command and a restart with the right arguments; equal values cause no restart; an agent ignoring the exit command is ended with signals; seat and cards survive the restart; the task is revalidated after the restart (`HOLD`, user move, stop request arriving meanwhile); leftover descendants are terminated through the subreaper and a survivor makes the seat unavailable; a start timeout is an abnormal exit on every kind of start; a failed model check blocks the role for that type, a passed one is cached per CLI version. Concurrency tests: two `doctor` runs building cards, a crash after card creation before the ID is recorded, an out-of-order read discarded by its `Date`, two seed writes from different projects, an adapter change re-judging unchanged cards, a card fixed back to the value in force. Unit tests cover checklist evaluation (combinations only, exactly one tick), reading the Codex catalog (visibility, `priority` order, reversed levels, the recorded fixtures, fallback from the command to the file to the shipped list, refresh only on a new CLI version, precedence of a `models` override), seed validation and the arguments built from `model_args`/`effort_args`.

**§20. Behaviour not yet observed.** Replace the text starting “- whether Trello notifies label additions (the notification …” with:

- whether Trello notifies label additions (the notification is guaranteed anyway by the comment with a mention, [initial §14.2](initial_spec_functional.md#142-notifications));
- whether ticking a checklist item **from the Trello UI** updates the card's `dateLastActivity` (the 10-second rule of [§2](#2-board-settings) relies on it; observed on 2026-11-10 for ticks and unticks through the API, which the UI uses too);
- the exit commands of Claude Code and Codex and any confirmation they show, and the model names and efforts of Claude Code (the shipped lists are checked against each CLI's documentation and, for Codex, its `models_cache.json` at implementation time);
- how stable the format of `codex debug models` and `models_cache.json` is across CLI versions (both undocumented; the fixtures of [§3](#3-model-and-effort) detect a change);
- what each CLI prints and returns when the model check is given a combination it rejects (a failure is any non-zero exit, so the exact text matters only for the comment).
