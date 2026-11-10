# kankan — technical specification

**Status:** specification v1.4 draft (v1.4 draft: board settings, [14.7](#147-board-settings))
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
| Seat state | `<gcd>/kankan/seats/<seat>.state.json` (current task, signals, label causes, comment cursor, test verifications, stop request) |
| Seat process data | `<gcd>/kankan/seats/<seat>.proc.json` |
| Audit log | `<gcd>/kankan/log.jsonl` |
| Downloaded card attachments | `<gcd>/kankan/attachments/<shortLink>/<attachment-id>/<file name>` ([13.3](kankan_spec_functional.md#133-card-attachments)) |
| Board settings state | `<gcd>/kankan/board-settings.json` ([14.7](#147-board-settings)) |
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
4. Starts the agent (the adapter's command) as a child, in the worktree, with `KANKAN_SEAT`, `KANKAN_SESSION`, `KANKAN_PROJECT` in the environment. In the child, before starting the agent: sets `PDEATHSIG` and checks that the parent is still the launcher, otherwise exits immediately.
5. The agent stays in the launcher's session and process group and keeps control of the pane's terminal. The launcher ignores `Ctrl-C` and `Ctrl-Z` and never reads the keyboard.
6. Handles the trust window ([8.6](#86-pane-observation-and-writes)) and waits for the agent to be ready.

When the agent exits, the launcher terminates the remaining processes in the pane session, releases the seat and exits normally. The cards stay with the seat.

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
3. **Assignment:** the launcher generates a task ID, saves it in the seat state, sends the adapter's new-conversation command (defaults: `/clear` for Claude Code, `/new` for Codex, whose location dialog is a known dialog answered by keeping the seat worktree) and a one-line prompt, for example `Run the current kankan task (kankan skill).` The agent reads the details with `kankan task --json`.
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

An adapter per agent type defines: start command, the **full-access** arguments that command must contain (agents run without permission prompts or sandbox: they need git, the network and Trello; defaults `claude --dangerously-skip-permissions` and `codex --dangerously-bypass-approvals-and-sandbox`; `agent`, `up` and `doctor` refuse a command without them), an optional login check command (exit 0 = logged in; used by `doctor`), new-conversation command, submit key, and groups of indicators — *ready*, *working*, *limit* (with the patterns to derive the resume time), *trust* (with the answer), *known dialogs* (with the answer). Each group is a list of regular expressions applied case-insensitively to the text stripped of colour codes; one match is enough, except for groups marked "all" (e.g. dialogs recognizable only by several texts together).

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

**Schema.** `boardconfig` holds a table of the board settings: key, kind (a fixed set of values, or an integer with bounds), default, help text. A board setting never holds a command, a path or free text passed to a shell (§17). The table may be empty: the mechanism ships before the settings that use it, and its tests use a setting defined only in the tests.

**List ID.** `config.json` records the `kankan` list's ID (`settings_list`) next to the workflow lists, but it is not part of `workflow_lists()`: its absence never produces `config_error`.

**Reading.** The board read of every cycle (B3 in [`kanban_requirements.md`](kanban_requirements.md)) already returns every open card with name and description, the setting cards included: reading the settings costs no request. A pure function, `boardconfig.evaluate(cards, state, schema)`, takes the cards of the `kankan` list and the local state and returns the effective values, the new state and the Trello actions to perform (comment, add or remove `Error`, prune).

**Local state, `<gcd>/kankan/board-settings.json`.** Written atomically like the other state files. Per key: last valid value (canonical form), setting card ID, last description handled, whether kankan set `Error` on the card, help comment ID. Per card with an unknown or duplicate name: card ID and whether kankan set `Error`. kankan re-evaluates a setting only when its inputs differ from those recorded: the description of its card, or the set of cards in the `kankan` list carrying its key (a card added, removed, renamed, moved in or out, a duplicate resolved). That is how an edit is handled once. When a card with an unknown or duplicate name leaves the list or becomes valid, kankan removes the `Error` it set on it.

**Who writes.** The launcher evaluates the settings under the project lock on every board read of its cycle: in `workflow.tick` after convergence (`idle`) and in `periodic_board_check` while busy (`working`, `suspending`, `limited`, `waiting_human`), so an edit is acknowledged within one poll interval even when every seat is busy; `doctor` does the same and also creates the list, missing setting cards and their help comments. With several seats, the project lock lets one launcher handle an edit and the others find it recorded. The effective value is recorded first; the Trello actions follow, and the description is recorded as handled only when they all succeeded, so a failed action is retried on the next cycle. Each comment carries the marker `kankan:<op-id>` with an op-id derived from the card ID, the description and a per-card change counter kept in the state (so flipping a value back, A→B→A, gets a new comment): after a timeout or a lost state file, the read back of 15.1 finds the comment already posted and no duplicate is written.

**Using a value.** Code that needs a board setting calls `boardconfig.effective(paths, key)`, which reads `board-settings.json` and falls back to the default. The launcher reads the values it needs when it assigns a task and keeps them for that task.

**Pruning.** After posting a comment on a setting card, kankan lists the card's comments (M2), keeps the 10 newest that carry its marker plus the help comment, and deletes the older marked ones (M6). The help comment carries its own marker, `kankan:help`, so it is recognized even after the state file is lost. A failed deletion is logged, flagged in the state and retried on the next cycle; it never blocks a cycle.

**Lost or corrupted state.** The file is rebuilt from the board: every card counts as changed, a valid description becomes the value, an invalid one falls back to the default (there is no last valid value any more) and is reported again; the op-id markers prevent duplicate "applied" comments.

**`Error` causes.** The cause of an `Error` on a setting card is recorded in `board-settings.json`, not in a seat state: no seat holds setting cards. Mentions follow `notify.mention` (the board's admins for the cards kankan created, the creator for cards made by people).

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
- Inputs validated before reaching Git or the shell; no Trello text interpolated into shell commands.
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
- **Fake Trello server** for board settings: serves comment deletion; tests cover list and card creation, `Error` added and removed, pruning that keeps the help comment and people's comments, and recovery after failed actions and after a lost state file.

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
- whether Trello notifies label additions (the notification is guaranteed anyway by the comment with a mention, [14.2](kankan_spec_functional.md#142-notifications)).
