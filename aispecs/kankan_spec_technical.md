# kankan — technical specification

**Status:** specification v1.4 draft (v1.4 draft: board settings, [14.7](#147-board-settings), model and effort, [14.8](#148-model-and-effort))
**Date:** 2026-11-10
**Companion:** [`kankan_spec_functional.md`](kankan_spec_functional.md) — the behaviour this file implements; read it first.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (requirements for backends other than Trello)

**Split rule.** The functional file describes what users and agents observe; this file describes how kankan implements it: modules, local files, refs, locks, processes, protocols and test strategy. Both files share one section numbering; a number missing here lives in the other file.

## 3. Principles

1. **Git decides ownership.** A card belongs to whoever its ref names; every change of ownership is a conditional ref operation.
2. **Trello expresses human intent.** The user communicates through labels and comments. Their changes to the list of an in-progress card are honoured on a *best effort* basis ([14.5](kankan_spec_functional.md#145-precedence-of-human-changes)).
3. **Git first, then Trello.** Every transition first records the new phase in the card's ref, then moves the card. An intermediate state is never eligible for other agents and is completed by convergence ([10.4](#104-convergence)).
4. **Conditional atomic operations.** Creations, releases, phase changes and advancing the integration branch use compare-and-swap; when several refs are involved, a single Git transaction.
5. **Short, serialized critical sections.** Changes to a project's workflow happen under a project lock held for a few seconds.
6. **Stable identifiers.** Cards, lists and labels are recognized by ID, never by name or position. The one exception is the setting cards, keyed by their name inside the `kankan` list, which is recognized by ID ([14.7](#147-board-settings)).
7. **A single write channel.** Agents and the user change workflow state through the kankan CLI.
8. **Fail closed.** An ambiguous state stops work on that card and reports it.
9. **No silent theft.** A seat is recovered automatically only with proof from the kernel that its holder is dead; every recovery is logged.
10. **kankan decides what to do, the agent decides how.**

## 5. Architecture

### 5.2 Local state

| Item | Location |
|---|---|
| Seat refs | `refs/agent-seats/<seat>` |
| Card refs | `refs/agent-cards/<card-id>` |
| Project configuration | `<gcd>/kankan/config.json` |
| Adapters (project override) | `<gcd>/kankan/agents.json` |
| Project lock | `<gcd>/kankan/project.lock` |
| Seat liveness lock | `<gcd>/kankan/seats/<seat>.lock` |
| Seat state | `<gcd>/kankan/seats/<seat>.state.json` (current task, signals, label causes, comment cursor, test verifications, stop request, model and effort of the running agent, planned agent restart) |
| Seat process data | `<gcd>/kankan/seats/<seat>.proc.json` |
| Audit log | `<gcd>/kankan/log.jsonl` |
| Downloaded card attachments | `<gcd>/kankan/attachments/<shortLink>/<attachment-id>/<file name>` ([13.3](kankan_spec_functional.md#133-card-attachments)) |
| Board settings state | `<gcd>/kankan/board-settings.json` ([14.7](#147-board-settings)) |
| Settings construction lock | `<gcd>/kankan/settings-construction.lock` ([14.8](#148-model-and-effort)) |
| Model check results | `~/.cache/kankan/model-checks.json` ([14.8](#148-model-and-effort)) |
| Seed writes lock | `~/.config/kankan/agents.json.lock` ([14.8](#148-model-and-effort)) |
| Seat worktrees | `~/coding/.kankan/<project>/<seat>` (configurable root) |
| Worktrees of blocked cards | `~/coding/.kankan/<project>/blocked-<shortLink>` |
| Adapters (user override) | `~/.config/kankan/agents.json` |
| Credentials | [6.2](kankan_spec_functional.md#62-credentials) |

`<gcd>` is `git rev-parse --git-common-dir`: shared by all worktrees, never committed. The `refs/agent-*` refs are not under `refs/heads` or `refs/tags`, so ordinary push and fetch do not transfer them. Lock files are never deleted or replaced. JSON state files are written to a temporary file and renamed atomically.

The worktree root is a hidden directory: it does not show up in menus listing the folders of `~/coding`, and it does not nest worktrees inside the project (agents would read the main project's instructions as those of a parent directory).

### 5.3 Components

kankan is a Python 3 package that uses **only the standard library** (pytest for development only).

| Module | Responsibility |
|---|---|
| `config` | Project configuration, credentials, `.properties` files, project lock. |
| `trello` | REST client: retries, read-after-write, error classification, rate limits. Isolates Trello from the rest (see [`kanban_requirements.md`](kanban_requirements.md)). |
| `refstore` | The only module that reads and writes kankan refs: conditional operations and multi-ref transactions. |
| `seats` | Seats: creation, occupation, flock, liveness, recovery, removal, process data. |
| `cards` | Card ref phases and transitions, convergence. |
| `workflow` | Task choice, eligibility, dependencies, `HOLD` rules. |
| `integrate` | `sync`, `verify`, `integrate`, worktrees of blocked cards. |
| `adapters` | Per-agent-type adapters and screen classification. |
| `launcher` | Supervisor, launcher and its state machine, tmux, pane writes, comment forwarding. |
| `agentcmds` | Agent commands ([16.2](kankan_spec_functional.md#162-agent-commands)): `task`, `note`, `done`, `ask`, `fail`, `yield`. |
| `notify` | Who is mentioned and whose comments are forwarded ([14.2](kankan_spec_functional.md#142-notifications), [14.3](kankan_spec_functional.md#143-answers-through-comments)). |
| `diag` | `status`, `claims`, `reconcile`, administration commands. |
| `doctor` | Installation and project checks, the preflight of `agent` and `up`. |
| `attachments` | Card attachments as task inputs ([13.3](kankan_spec_functional.md#133-card-attachments)): classification, download, local copies. |
| `boardconfig` | Board settings ([14.7](#147-board-settings)): settings schema, evaluation of the setting cards, effective values. |
| `cli` | Commands, text and JSON output, exit codes. |

The **shared skill** ([`SKILL.md`](../kankan/skill/SKILL.md), for Claude Code and Codex) explains to the agent how to carry out an assigned task. The work cycle belongs to the launcher.

## 6. Setup

### 6.2 Credentials

**Writing the credentials file:** kankan never rewrites the file from scratch. It keeps comments, order and foreign keys, updates its own keys in place and appends missing ones at the end, writes to a temporary file with the same permissions and owner and renames it atomically. It warns if the file is readable by other users. Credentials never appear in logs, comments or output.

## 7. Seats

### 7.3 Occupation

**Invariant:** if a seat's ref exists, its flock is held by the holder or the holder is dead.

The launcher, under the project lock:
1. tries the seat's exclusive **non-blocking** `flock`; if it fails, the seat is alive and it moves on to the next one;
2. if the seat's ref does not exist, creates it;
3. if it exists (flock free: holder dead or dying), applies recovery ([7.5](#75-liveness-and-automatic-recovery)).

Release: conditional deletion of the ref, then release of the flock. The flock descriptor is opened as non-inheritable, so no child process keeps it.

Every agent command (with `KANKAN_SEAT` and `KANKAN_SESSION` in the environment) checks that the seat's ref names its own session; otherwise it answers `not_owner`.

### 7.4 Identity and process data

- The **seat blob** contains only the identity: seat, type, session token, date. It is written at occupation and never changes.
- The **`<seat>.proc.json`** file contains PID and start time of supervisor, launcher and agent, the terminal session ID of the pane and the `boot_id`. The launcher updates it when it starts or restarts the agent. It decides nothing: it serves recovery (which processes to terminate) and diagnostics.

### 7.5 Liveness and automatic recovery

A seat with a free flock is recovered as follows:
1. if the `boot_id` has changed, all recorded processes are dead;
2. otherwise kankan terminates the remaining processes of the recorded terminal session (`SIGTERM`, then `SIGKILL`) and checks that none remains; if any survives, the seat is **unavailable** and is reported;
3. replaces the seat's ref conditionally, records the recovery in the log and with a comment on the cards held by the seat.

The remaining processes are orphans of a dead agent and could still write to the worktree: terminating them is necessary before reusing it. Documented limit: daemons that deliberately detach from the session (e.g. the Gradle daemon) escape this control; they normally do not write to the sources.

Cards belong to the seat, so the new session inherits the work. A seat that is alive but stuck is never recovered automatically: it requires `kankan seat recover <seat> [--kill]` ([15.4](kankan_spec_functional.md#154-administration-commands)). An agent stopped by a usage limit is alive.

## 8. Processes and launcher

### 8.2 Supervisor

Starts the launcher as a child. If the launcher terminates abnormally, it writes a message in the pane, waits with increasing delays and starts a new one in the same pane, up to a maximum number of restarts; beyond that, it stops leaving the error visible. If the launcher terminates normally (agent exited, `stop`), the supervisor terminates too and the tmux session closes. The new launcher recovers the seat and resumes work with convergence.

### 8.3 Launcher startup

1. Checks configuration and the board↔registry binding; warns if the integration branch is checked out in any worktree.
2. Occupies the seat ([7.3](#73-occupation)) and creates the worktree if missing.
3. Renames the tmux session to `ai-<project>-<seat>`: the `ai-` prefix and the agent among the pane's descendants make the session visible to `airesumer.sh`.
4. Starts the agent (the adapter's command, plus the model and effort arguments of [14.8](#148-model-and-effort)) as a child, in the worktree, with `KANKAN_SEAT`, `KANKAN_SESSION`, `KANKAN_PROJECT` in the environment. In the child, before starting the agent: sets `PDEATHSIG` and checks that the parent is still the launcher, otherwise exits immediately.
5. The agent stays in the launcher's session and process group and keeps control of the pane's terminal. The launcher ignores `Ctrl-C` and `Ctrl-Z` and never reads the keyboard.
6. Handles the trust window ([8.6](#86-pane-observation-and-writes)) and waits for the agent to be ready.

When the agent exits, the launcher terminates the remaining processes in the pane session, releases the seat and exits normally. The cards stay with the seat. The exception is a planned restart recorded in the seat state ([14.8](#148-model-and-effort)): the exit is expected and the launcher starts the agent again.

### 8.4 State machine

| State | What it does | Inactivity check |
|---|---|---|
| `starting` | Agent startup, trust window, waiting for the "ready" state | no |
| `idle` | Convergence, task choice, waiting at intervals (default 2 min) | no |
| `working` | Task assigned | yes |
| `waiting_human` | After `ask`/`fail`: comment forwarding | no |
| `limited` | Usage limit: `Resume` label | no |
| `suspending` | Suspension request sent, waiting for `yield` | yes, longer threshold |
| `closing` | Signal received: convergence, waiting for the pane to be idle | no |
| `stopping` | After `kankan stop`: exits at the end of the task | — |

In `idle`, with no work, the launcher does not involve the agent and spends no tokens. After a configurable inactivity time (default: never) it ends the session.

### 8.5 Cycle and signals

1. **Convergence** ([10.4](#104-convergence)) and check of the board↔registry binding.
2. **Task choice** ([chapter 9](kankan_spec_functional.md#9-task-choice)) and preparation before involving the agent: ref transition, verified move, branch and worktree ([chapter 11](kankan_spec_functional.md#11-branches-and-worktrees)).
3. **Assignment:** if the task's role needs another model or effort than the running agent's, the launcher first restarts the agent ([14.8](#148-model-and-effort)). Then it generates a task ID, saves it in the seat state, sends the adapter's new-conversation command (defaults: `/clear` for Claude Code, `/new` for Codex, whose location dialog is a known dialog answered by keeping the seat worktree) and a one-line prompt, for example `Run the current kankan task (kankan skill).` The agent reads the details with `kankan task --json`.
4. **Execution:** the agent closes the task with a signal ([13.2](kankan_spec_functional.md#132-signals)). Agent commands act only on the current task, and the signal written into the seat state carries the task ID; a signal with a different ID is ignored.
5. **Closing:** convergence, waiting for the pane to be idle, back to step 1.

`kankan stop <seat>` records a stop request: the launcher exits after the current task (even if the request arrives in `limited`).

If in `waiting_human` the agent, after stopping ("ready"), goes back to "working" without the launcher having forwarded anything, the user answered from tmux (the "working" right after `ask` is the agent finishing its own turn and does not count): the launcher removes the label set by `ask`/`fail` and comments "resumed".

### 8.6 Pane observation and writes

The launcher periodically reads the screen (`tmux capture-pane`), considers only the **last lines** (default 30) and classifies them with the adapter. Precedence:
1. **known dialogs with an automatic answer** (e.g. Codex's model-switch prompt);
2. **usage limit** → state `limited`, label `Resume YYYY-MM-DD hh:mm` on the current card; the time is interpreted in the time zone stated by the message, otherwise in the server's, and if it has already passed it means the next day;
3. **working** → the agent is working; leaves `limited` and removes the `Resume` label from the card;
4. **ready** → the agent is waiting for input;
5. **unknown** → no writes; if it persists in `working`, `Error` with a comment.

In `working`, if the screen is "ready" with no signal for N minutes (configurable), the launcher sets `Error` with the comment "agent stopped without a signal" and a mention.

**Trust window:** answered automatically only if the real path of the worktree (symlinks resolved) is under the kankan root; otherwise a warning in the log and waiting for the user to answer.

**Pane write rules:**
- the launcher writes only in the "ready" state, or to queue a message (suspension, forwarding), and never `Esc` or other interrupt keys;
- if a tmux client is attached with recent keyboard activity, the write is postponed until N seconds of inactivity have passed;
- every text is sent through a uniquely named tmux buffer (`load-buffer` + `paste-buffer`, buffer deleted after use), so multi-line texts arrive whole and do not interleave.

### 8.7 Adapters

An adapter per agent type defines: start command, the **full-access** arguments that command must contain (agents run without permission prompts or sandbox: they need git, the network and Trello; defaults `claude --dangerously-skip-permissions` and `codex --dangerously-bypass-approvals-and-sandbox`; `agent`, `up` and `doctor` refuse a command without them), an optional login check command (exit 0 = logged in; used by `doctor`), new-conversation command, exit command and its timeout, submit key, the lists of models and efforts with the arguments that select them and the seeds per role ([14.8](#148-model-and-effort)), and groups of indicators — *ready*, *working*, *limit* (with the patterns to derive the resume time), *trust* (with the answer), *known dialogs* (with the answer). Each group is a list of regular expressions applied case-insensitively to the text stripped of colour codes; one match is enough, except for groups marked "all" (e.g. dialogs recognizable only by several texts together).

The defaults ship with kankan and start from the patterns of `airesumer.sh`. They are overridden, only for the parts that change, in `~/.config/kankan/agents.json` (user) and `<gcd>/kankan/agents.json` (project).

`kankan adapter check <seat>` shows the current classification of that seat's screen and which expression fired; it also accepts a file with a saved screen.

## 10. Card refs

### 10.1 Primitives (`refstore`)

| Operation | Mechanism |
|---|---|
| Creation | `update-ref` with a null previous value. If it fails, read back: if the ref points to one's own blob the previous attempt had succeeded, otherwise `already_claimed`. |
| Change / deletion | `update-ref` with the expected value: succeeds only if the ref still points to the expected blob. |
| Transaction | `git update-ref --stdin` with `start`, `verify`, `update`/`create`/`delete`, `commit`: all operations or none. |

Every card transition is a transaction that includes `verify` of the ref of one's own seat, so a command started before a recovery or an administrative action can no longer change anything.

Inputs validated before reaching Git: card ID `^[0-9a-f]{24}$`, shortLink `^[A-Za-z0-9]{8}$`, seat `^[a-z]+-[0-9]+$`.

### 10.2 Metadata blobs

Every ref points to an immutable JSON blob (`git hash-object -w`) containing a random value: its ID is unique and acts as a signature. Example of a card ref:

```json
{"schema":1,"phase":"impl","card":"65a1…e3","short":"Ab12Cd34","owner":"claude-1",
 "session":"9f2c…","implementer":"claude-1","implementer_type":"claude","branch":"26kankan-Ab12Cd34",
 "target_list":"<list id>","created":"2026-11-08T15:42:10Z","nonce":"b71e…"}
```

Phase-specific fields: in `integrated` the merge hash and the tested hash; in `parked` the cause (`yield`, `block`, user move). The `session` field is historical: current ownership is given by `owner` plus the seat's ref. After the ref is deleted the blob becomes unreachable and `git gc` removes it; history lives in `log.jsonl` and in Trello comments.

### 10.3 Phases and transitions

| Transition | Phase | Holder | Destination Trello list |
|---|---|---|---|
| Implementation claim | none/`parked` → `impl` | seat | own `In Progress` (at the top) |
| Implementation `done` | `impl` → `implemented` | none | own `Implemented` |
| Review claim | `implemented`/`parked` → `review` | seat | own `Review` |
| Successful `integrate` | `review` → `integrated` | none | `Done` (after `done` or convergence) |
| Closing | `integrated` → ref deleted | — | `Done` verified |
| `yield` during implementation | `impl` → `parked` | none | `Backlog` (at the top) |
| `yield` during review | `review` → `implemented` | none | implementer's `Implemented` |
| `block` | `impl`/`review` → `parked` | none | `Blocked` |
| User move ([14.6](kankan_spec_functional.md#146-cards-moved-by-the-user-safety-net)) | `impl`/`review` → `parked` | none | list where the user put the card |
| `resolve --done` | `parked` → `implemented` | none | implementer's `Implemented` |

Every transition happens under the project lock: reread of the card, Git transaction (first), Trello move and verification reread (after), comment with an anti-duplicate marker. If the move fails or the reread is ambiguous, the ref stays in the new phase and convergence will complete the move; if the anomaly persists the card gets `Error`.

### 10.4 Convergence

On every cycle, under the project lock, the launcher compares the phase of each card ref with the expected Trello list (`target_list`) and completes the missing moves:
- for phases with a holder (`impl`, `review`) only if the holding seat is its own;
- for phases without a holder (`implemented`, `integrated`, `parked`) any launcher;
- a card already in the right list counts as moved;
- in phase `integrated` with the card verified in `Done`, it deletes the ref.

This way crashes between the Git transition and the Trello move (claim, `done`, `yield`, `block`, `integrate`) resolve themselves. Intermediate states are never eligible for other agents, because eligibility requires agreement between phase and list.

## 14. Communication through Trello

### 14.7 Board settings

Behaviour: [functional §14.7](kankan_spec_functional.md#147-board-settings).

**Schema.** `boardconfig` holds a table of the board settings: key, kind (description value: a fixed set of values or an integer with bounds, with a default; checklist value: a builder of the expected checklists and a validator, with seeds), help text. A board setting never holds a command, a path or free text passed to a shell (§17). The first settings are `implementation` and `review` ([14.8](#148-model-and-effort)); the mechanism's tests also use a description-value setting defined only in the tests.

**List ID.** `config.json` records the `kankan` list's ID (`settings_list`) next to the workflow lists, but it is not part of `workflow_lists()`: its absence never produces `config_error`. Launchers reload the configuration on every cycle, so a list created or replaced by `doctor` is picked up without a restart.

**Reading.** The board read of every cycle (B3 in [`kanban_requirements.md`](kanban_requirements.md)) returns every open card with name, description, labels and `dateLastActivity`, and, with `checklists=all`, every checklist of the board as a top-level array (each with `idCard`) with its items, their `state` (`complete`/`incomplete`) and position; the list read nests the checklists inside each card: the setting cards come at no extra request. Besides, every `settings_poll_seconds` (local configuration, default 30) a launcher reads only the `kankan` list's cards with their labels and checklists (L6). The time of the last such read is kept in `board-settings.json`, checked and updated under the project lock before the read; a launcher skips its read when another one read less than `settings_poll_seconds` ago, so a project makes one such read per interval however many launchers run. A failed read is logged and the next one happens after the interval.

**Quiet period.** Each read keeps the response's `Date` header. A card is judged only when `Date − dateLastActivity ≥ 10 s`; otherwise, or when either timestamp is missing or malformed (logged), it is skipped and not recorded as handled, so it is judged at a later read.

**Evaluation.** A pure function, `boardconfig.evaluate(cards, state, schema)`, takes the cards of the `kankan` list, the local state and the current schema (including the adapters' combinations, [14.8](#148-model-and-effort)) and returns the effective values, the new state and the Trello actions to perform (comment, add or remove `Error`, prune). It runs under the project lock, on the read that triggered it. A read whose `Date` is older than the `Date` of the last read applied (recorded in the state) is discarded, so an overlapping slow read never applies older content over newer.

**Local state, `<gcd>/kankan/board-settings.json`.** Written atomically like the other state files. Per key: last valid value (canonical form), whether the card is currently invalid, setting card ID, the **inputs fingerprint** last handled (a hash of the description, or of the card's checklists in position order with their names, item names and states), the **schema fingerprint** (a hash of the allowed values or of the adapters' combinations), whether kankan set `Error` on the card, a change counter, help comment ID, pending actions. Per card with an unknown or duplicate name: card ID and whether kankan set `Error`. Also: the `Date` of the last read applied, the time of the last fast read, and the cards under construction ([14.8](#148-model-and-effort)).

**When a setting is re-evaluated.** When its inputs fingerprint changes, when the set of cards in the `kankan` list carrying its key changes (a card added, removed, renamed, moved in or out, a duplicate resolved), or when its schema fingerprint changes (an adapter override or a kankan upgrade changes the combinations): an unchanged card is then judged again against the new schema. That is how an edit is handled once. Outcomes are transitions, not only value changes: an invalid card fixed back to the value in force is a valid transition (kankan removes its `Error` and comments "fixed; still using …") even though the value does not change. When a card with an unknown or duplicate name leaves the list or becomes valid, kankan removes the `Error` it set on it.

**Who writes.** The launcher evaluates the settings on every board read of its cycle: in `workflow.tick` after convergence (`idle`), in `periodic_board_check` while busy (`working`, `suspending`, `limited`, `waiting_human`) and on the fast settings read, in every state; while reads succeed, an edit is acknowledged within 10–40 seconds even when every seat is busy. `doctor` does the same and also creates the list and missing setting cards. With several seats, the project lock lets one launcher handle an edit and the others find it recorded. The effective value and the pending actions are recorded first; the Trello actions follow, and each is cleared when it succeeded, so a failed action is retried on the next cycle. Before adding or removing `Error`, kankan uses the labels of the read that triggered the evaluation; a label already in the wanted state is left alone. Each comment carries the marker `kankan:<op-id>`, with an op-id derived from the card ID, the inputs fingerprint and the change counter (so flipping a value back, A→B→A, gets a new comment); after a timeout, the read back of 15.1 finds the comment already posted and no duplicate is written.

**Using a value.** Code that needs a board setting calls `boardconfig.effective(paths, key)`, which reads `board-settings.json` and falls back to the default (description value) or to the seeds (checklist value). The launcher reads the values it needs when it starts the agent and when it assigns a task, and keeps them for that task.

**Pruning.** After posting a comment on a setting card, kankan lists the card's comments (M2), keeps the 10 newest that carry its marker **and** were written by kankan's account (`cfg.kankan_member_id`), plus the help comment, and deletes the older such ones (M6). The help comment carries its own marker, `kankan:help`, so it is recognized even after the state file is lost. A failed deletion is logged, kept as a pending action and retried on the next cycle; it never blocks a cycle.

**Lost or corrupted state.** The file is rebuilt from the board: every card counts as changed, a valid card becomes the value, an invalid one falls back to the default or the seeds (there is no last valid value any more) and is reported again. The change counters restart from zero, so duplicate-free comments after a lost state are best effort: an "applied" comment may be repeated, or skipped when an older comment with the same op-id is found.

**`Error` causes.** The cause of an `Error` on a setting card is recorded in `board-settings.json`, not in a seat state: no seat holds setting cards. Mentions follow `notify.mention` (the board's admins for the cards kankan created, the creator for cards made by people).

**Backends.** Description values need only B3, C6 and M1–M2. Checklist values also need L6, C7 and C8 ([`kanban_requirements.md`](kanban_requirements.md)); on a backend without them, `doctor` reports that the model and effort cannot be set on the board, and the agents run with the seeds.

### 14.8 Model and effort

Behaviour: [functional §14.8](kankan_spec_functional.md#148-model-and-effort).

**Adapter keys.** Each adapter ([8.7](#87-adapters)) adds:
- `models`: ordered from the most to the least powerful, each with the efforts it accepts from the highest down, e.g. `{"gpt-6.1-sol": {"efforts": ["max", "xhigh", "high", "medium", "low"]}}`. The shipped lists follow each provider's documentation (for Codex also its `models_cache.json`, [`docs/adapter-notes.md`](../docs/adapter-notes.md)). Unlike the other adapter keys, an override of `models` **replaces** the shipped value instead of being merged into it, so the user can remove and reorder models.
- `model_args` and `effort_args`: argument templates appended to the start command, e.g. `["--model", "{model}"]` and `["--effort", "{effort}"]` for Claude Code, `["-m", "{model}"]` and `["-c", "model_reasoning_effort={effort}"]` for Codex.
- `seed`: `{"implementation": {"model": …, "effort": …}, "review": {…}}`; never shipped, only set by the user or by the seed question.
- `exit_command` (e.g. `/exit`), `exit_timeout_seconds` (default 20), `start_timeout_seconds` (default 120: the agent must reach "ready" within it).
- `version_command` (e.g. `["claude", "--version"]`), `model_check` (argument template of a one-shot request with the pair, see below), `model_check_home` (optional: an environment variable and the files to link into a temporary directory used as that variable's value) and `model_check_timeout_seconds` (default 90).

They follow the usual override order (shipped defaults, `~/.config/kankan/agents.json`, `<gcd>/kankan/agents.json`). Loading an adapter validates that every model has a non-empty list of efforts, that each seed is one of its combinations, that the names contain no line breaks and that the templates contain exactly their placeholders; otherwise `config_error`. The adapters are reloaded at every evaluation, so a changed override is seen without restarting the launchers.

**Checklist value.** The setting keys are `implementation` and `review`. The expected content is one checklist per adapter type, named after the type, with one item `<model> · <effort>` per combination, models in order and each model's efforts in order. `boardconfig` validates the card's checklists against it: one checklist per type, no checklist for an unknown type, exactly one ticked item per checklist, and that item one of the adapter's combinations (an unknown unticked item is only removed by the next `doctor`). Item names are compared after collapsing whitespace, so `sonnet·high` and `sonnet · high` match. The value is the map type → (model, effort); the comment of an invalid card lists every problem found. A ticked combination that the adapter no longer lists makes the card invalid, and the last valid value (possibly that same combination) stays in force: it is the only way a combination outside the adapter's lists reaches a command line (§17), and only after it passed the model check.

**Model check.** A combination is used to start an agent only after it passed a check, cached in `~/.cache/kankan/model-checks.json` per agent type, CLI version (output of `version_command`), model and effort:
- the check runs `model_check` with the pair as separate arguments, without a shell, in a new empty temporary directory outside any Git repository, so no project instruction file is found; with `model_check_home`, the named variable points to a second temporary directory holding only symlinks to the listed files (Codex: `CODEX_HOME` with `auth.json`), so no user instruction file is found either; both directories are deleted afterwards;
- the shipped templates also disable everything that adds context, as measured in [`docs/adapter-notes.md`](../docs/adapter-notes.md): Claude Code `-p --no-session-persistence --setting-sources "" --settings '{"autoMemoryEnabled":false}' --tools "" --strict-mcp-config --disable-slash-commands --exclude-dynamic-system-prompt-sections`, Codex `exec --ephemeral --skip-git-repo-check`; the prompt is `Reply OK`;
- exit 0 is a pass; any other exit is a failure, recorded with the last lines of the CLI's error output (credentials never included); a timeout is not cached and counts as neither;
- it runs outside the project lock: in the launcher that applied a change, right after applying it; before a start with a pair not yet passed; and in `doctor` for every ticked pair (not with `--offline`).

A failed pair sets `Error` on the setting card with a comment naming the pair and quoting the CLI's message. While the pair in force for a type and role has failed, the launchers of that type do not take tasks of that role (task choice skips them; `status` says why); their other role and the other types continue. Ticking another combination, or a new CLI version, lifts it.

**Seed question.** `init` and `doctor` ask, before taking any lock, for each adapter type and role without a seed after the override merge. The combinations are numbered in adapter order with the keys `1`–`9`, `a`–`z`, `A`–`Z` (61 at most; an adapter with more combinations is a `config_error`). The key is read with the terminal in cbreak mode (`termios`/`tty`, restored in a `finally`), only when stdin and stdout are terminals; otherwise, and always with `--no-fix` or `--offline`, there is no question and the command reports the manual step (`needs_action`). The answer is merged into `~/.config/kankan/agents.json` under a user-level lock (`~/.config/kankan/agents.json.lock`, never held together with a project lock): the file is re-read under the lock, the seed added, other keys kept, and the result written to a temporary file and renamed atomically (created with mode 600 if missing).

**Creating and synchronizing.** `init` and `doctor` create a missing card (C6), its checklists (C7) and items with the value in force ticked (C8); they add new items unticked, delete items no longer listed and move items to the adapter's order (C8); the next evaluation judges the result like any edit. These are many calls, so they do not hold the project lock throughout (principle 5). Instead, a **construction lock** (`<gcd>/kankan/settings-construction.lock`, taken before the project lock when both are needed) makes one `doctor` at a time build cards. Under the project lock, kankan records the card as under construction in `board-settings.json`, and the evaluation skips such a card; the record is cleared when the card is complete. A card's ID is recorded as soon as the creation call returns; after a crash or a timeout before that, the next `doctor` looks for the card **by name** in the `kankan` list and adopts it, and finds checklists and items by name: every step is repeatable and adds only what is missing. A construction record whose lock is free belongs to an interrupted `doctor` and is resumed by the next one. Every creation prints the ticked values and their source (seed file, or last valid value).

**Restart.** At assignment ([8.5](#85-cycle-and-signals)) the launcher compares the (model, effort) of the task's role for its type with the values recorded in the seat state for the running agent. The role of a task is that of the card's phase: resumed work and the WIP-only task of [9.4](kankan_spec_functional.md#94-hold) keep the role of the phase (`impl` or `review`). If the values differ:
1. it records in the seat state a planned restart with the new values, so the agent's exit is not taken for the end of the session ([8.3](#83-launcher-startup));
2. it writes `exit_command` in the pane (the agent is "ready": the write rules of [8.6](#86-pane-observation-and-writes) apply) and waits up to `exit_timeout_seconds` for the agent process to exit; known dialogs are answered as usual;
3. if the agent is still alive: `SIGTERM` to the agent, then `SIGKILL`; never to its process group or the pane session, which it shares with the launcher and the supervisor ([8.3](#83-launcher-startup) step 5);
4. it terminates the agent's leftover descendants: the launcher is a **child subreaper** (`PR_SET_CHILD_SUBREAPER`), so descendants orphaned by the agent's exit become its children and are found among them; it terminates them (`SIGTERM`, then `SIGKILL`) and checks that none remains; if any survives, the seat is unavailable as in [7.5](#75-liveness-and-automatic-recovery), the card being assigned gets `Error` and the launcher does not start a new agent;
5. it starts the agent again with the new arguments as in [8.3](#83-launcher-startup) steps 4–6, updates `<seat>.proc.json` and the recorded values and clears the planned restart;
6. before assigning, it **revalidates** the task as at the start of a cycle: seat ownership, the card's ref and list, `HOLD`, a user move, a stop request and the board binding; when a check fails it follows the usual path (`yield` for `HOLD`, [14.6](kankan_spec_functional.md#146-cards-moved-by-the-user-safety-net) for a user move, exit for a stop request, `config_error` for the binding) instead of assigning.

The seat ref, the lease, the flock and the card refs are untouched. During the restart the launcher is in state `starting` ([8.4](#84-state-machine)). A crash during the restart leaves the planned restart in the seat state; the next launcher clears it and starts as below.

**Every start uses the values in force at that moment.** `agent`, `up`, a launcher started by the supervisor and a restart all read the effective values when they start the agent: the role of the seat's held card (review before implementation, as in [9.1](kankan_spec_functional.md#91-priority)), otherwise `implementation`. A rejected pair is therefore not retried forever: ticking another item on the card is picked up by the next start.

**Failed start.** On every start (first start, supervisor restart, planned restart), an agent that exits, or does not reach "ready" within `start_timeout_seconds`, is an abnormal exit: the launcher ends it as in step 3, exits abnormally and the supervisor's backoff applies. If a card was being assigned, it gets `Error` with a comment naming the model and effort; kankan removes that `Error` when an agent of the seat reaches "ready" with other values or the same values succeed.

## 15. Errors and recovery

### 15.1 Principles

- **Trello reads:** retry with increasing delay on network errors, 5xx and 429; short network timeouts, because calls happen under the project lock.
- **Trello writes:** after a timeout, read back before retrying; comments contain a marker with the operation ID.
- **Intermediate Git/Trello state:** resolved by convergence ([10.4](#104-convergence)).
- **Unexpected Git errors:** fail closed, `Error`, comment.

## 16. CLI

### 16.3 Internal commands

`supervisor`, `launcher`: started by `agent`/`up`, hidden from help.

## 17. Security

- Credentials only in the sources of [6.2](kankan_spec_functional.md#62-credentials), never in configuration, logs, comments, blobs or output.
- Write access to the clone limited to the user running the agents; whoever can write the refs can bypass the protocol.
- Blobs contain hostname, user and paths: the refs must not be published (`pre-push` hook, `purge-remote`).
- The seat file `kankan-agent.json` (seat and session) lives in the worktree's Git directory, writable only by the user; a process left over in the seat worktree could use the current session, so seat recovery kills the recorded processes before the worktree is reused.
- Only `integrate` pushes the integration branch: the `pre-push` hook refuses other pushes to it from the kankan clone.
- Inputs validated before reaching Git or the shell; no Trello text interpolated into shell commands. Model and effort reach the agent's command line only as items of the adapter's own lists, or as the last valid value after the adapter dropped it and only once the model check passed, always as separate arguments without a shell ([14.8](#148-model-and-effort)). The model check runs in empty temporary directories with only symlinks to the agent's credential files; its output is stored without credentials.
- Text forwarded to the agent comes only from board members other than kankan's own account: board membership decides who can instruct the agents. The same rule applies to card attachments ([13.3](kankan_spec_functional.md#133-card-attachments)).
- Attachment downloads send the credential only to the Trello API host, in a header, never on a redirect to another host; downloaded file names are sanitized and stay inside the card's attachment directory.
- Automatic answer to the trust window limited to worktrees under the kankan root (real path).
- Board settings ([14.7](#147-board-settings)) can be changed by anyone who can edit the board: they accept only values from a fixed set or bounded numbers, are never interpolated into a shell command, and never name a command or a path.
- Agents run in full-access mode (no permission prompts, no sandbox): they can do anything the user running them can. Run kankan under a user and on a machine where that is acceptable.

## 18. Tests

### 18.1 Strategy

- **Unit:** `refstore`, `config`, `.properties` files, phases and transitions, convergence, task choice, dependencies, adapter classification, `boardconfig.evaluate` (valid change, invalid value, empty description, missing card, unknown name, duplicates, case-insensitive matching, reported once, A→B→A).
- **Local integration:** fake Trello server (subset of the API used, with injection of timeouts, 5xx, 429, inconsistent responses and successful writes with a late response), real Git in temporary directories, real tmux and a fake agent (a script that simulates ready, working, limit, trust, known dialogs and calls kankan according to a scenario).
- **Fault injection** at every boundary between one effect and the next (simulated crash between Git transaction and Trello move, between advancing the integration branch and `done`, during seat creation, during comment forwarding).
- **On real Trello:** tests marked `trello_live` on a dedicated test board, including creating and deleting a comment (M6).
- **Fake Trello server** for board settings: serves comment deletion; tests cover list and card creation, `Error` added and removed, pruning that keeps the help comment and people's comments, and recovery after failed actions and after a lost state file; checklists in the board read and the list read, card creation with ticked items, item synchronization, the 10-second rule against the response `Date`, the fast poll shared by two launchers.
- **Fake agent** for model and effort: a task needing other values triggers the exit command and a restart with the right arguments; equal values cause no restart; an agent ignoring the exit command is ended with signals; seat and cards survive the restart; the task is revalidated after the restart (`HOLD`, user move, stop request arriving meanwhile); leftover descendants are terminated through the subreaper and a survivor makes the seat unavailable; a start timeout is an abnormal exit on every kind of start; a failed model check blocks the role for that type, a passed one is cached per CLI version. Concurrency tests: two `doctor` runs building cards, a crash after card creation before the ID is recorded, an out-of-order read discarded by its `Date`, two seed writes from different projects, an adapter change re-judging unchanged cards, a card fixed back to the value in force. Unit tests cover checklist evaluation (combinations only, exactly one tick), seed validation and the arguments built from `model_args`/`effort_args`.

### 18.2 Test board

- Configuration outside the code and the repository, in this order: variables `KANKAN_TEST_BOARD_URL` and `KANKAN_TEST_CREDENTIALS` (path of a `.properties` file); file `~/.config/kankan/test.properties` with `kankan.test.board.url`, `kankan.trello.key`, `kankan.trello.token`.
- Without configuration the `trello_live` tests are skipped with an explicit message.
- Before any write the tests check that the board description contains the marker `kankan-test-board`; otherwise they stop.
- Each run uses a unique prefix for lists, cards and labels; at the end it deletes cards and labels and archives the lists. A cleanup option archives the leftovers of interrupted runs.
- A manual run with real agents (`tests/live/agents_e2e.py`) uses the workflow lists and labels with their real names, kept on the test board and adopted by name; it creates and deletes only its own cards (`ke-` prefix) and its own registry block.

## 20. Behaviour not yet observed

The defaults were set from observations of the real agents, recorded with their evidence in [`docs/adapter-notes.md`](../docs/adapter-notes.md). These behaviours have not been observed yet; each has a configurable default or a fallback, and should be checked the first time it happens:
- the real usage-limit screens of Claude Code and Codex (the default patterns come from `airesumer.sh`; check with `kankan adapter check` and override in `agents.json` if needed);
- Codex's model-switch dialog (same origin);
- the confirmation Claude Code may show the first time it starts with `--dangerously-skip-permissions` on an account that never accepted bypass mode;
- whether Trello notifies label additions (the notification is guaranteed anyway by the comment with a mention, [14.2](kankan_spec_functional.md#142-notifications));
- whether ticking a checklist item **from the Trello UI** updates the card's `dateLastActivity` (the 10-second rule of [14.7](#147-board-settings) relies on it; observed on 2026-11-10 for ticks and unticks through the API, which the UI uses too);
- the exit commands of Claude Code and Codex and any confirmation they show, and the model names and efforts of Claude Code (the shipped lists are checked against each CLI's documentation and, for Codex, its `models_cache.json` at implementation time);
- what each CLI prints and returns when the model check is given a combination it rejects (a failure is any non-zero exit, so the exact text matters only for the comment).
