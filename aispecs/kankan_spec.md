# kankan — coordinating AI agents on a Kanban board

**Status:** specification v1.1 (English translation)
**Date:** 2026-11-08
**Related documents:** `kanban_requirements.md` (requirements for backends other than Trello)
**Language:** this specification, the implementation plan, the code and code comments are in English; the user manual is in Italian. This document is a translation of the original Italian v1.1.

## 1. Purpose

kankan lets several AI coding agents (Claude Code and Codex CLI) work in parallel on the same project, taking tasks from a Trello board and handing work to each other: one agent implements a card, an agent of a different type reviews it, integrates it into the main branch and closes it.

The user creates and orders the cards on Trello, starts the agents and steps in only when an agent asks or when they decide to stop a card. kankan guarantees that two agents never work on the same card and that only reviewed and tested code enters the main branch.

kankan is meant to become an open source project: it contains no hard-wired references to a specific user's boards, paths or credentials.

## 2. Scope and assumptions

- All agents run **on the same Linux server**, in the same process namespace.
- A **project** is a Git clone in `~/coding/<project>` bound to **one** Trello board. All agents of a project work on worktrees of the same clone and therefore share the same Git ref store. Several projects can coexist on the same server, each with its own state.
- The main branch is called `main` (configurable).
- Agents run in **interactive** mode inside **tmux** sessions, so the user can attach and watch them, and the external tool `airesumer.sh` can resume them after a usage limit.
- tmux sessions survive SSH disconnection. Requirements: `KillUserProcesses=no` in logind (Ubuntu default) and, for automatic start, lingering enabled (`loginctl enable-linger`).
- Trello is reachable via REST with a user API key and token. Trello does **not** offer conditional updates.
- Participants cooperate: they change in-progress cards, claims and seats only through kankan.
- Minimum versions (to be confirmed during planning): Python 3.10, Git 2.38, tmux 3.2.

## 3. Principles

1. **Git decides ownership.** A card belongs to whoever its ref names; every change of ownership is a conditional ref operation.
2. **Trello expresses human intent.** The user communicates through labels and comments. Their changes to the list of an in-progress card are honoured on a *best effort* basis (14.5).
3. **Git first, then Trello.** Every transition first records the new phase in the card's ref, then moves the card. An intermediate state is never eligible for other agents and is completed by convergence (10.4).
4. **Conditional atomic operations.** Creations, releases, phase changes and advancing `main` use compare-and-swap; when several refs are involved, a single Git transaction.
5. **Short, serialized critical sections.** Changes to a project's workflow happen under a project lock held for a few seconds.
6. **Stable identifiers.** Cards, lists and labels are recognized by ID, never by name or position.
7. **A single write channel.** Agents and the user change workflow state through the kankan CLI.
8. **Fail closed.** An ambiguous state stops work on that card and reports it.
9. **No silent theft.** A seat is recovered automatically only with proof from the kernel that its holder is dead; every recovery is logged.
10. **kankan decides what to do, the agent decides how.**

## 4. Glossary

| Term | Meaning |
|---|---|
| **Type** | Agent type: `claude` or `codex`. Determines the adapter (8.7) and the cross-review rule. |
| **Seat** | Stable identity `<type>-<n>` (e.g. `claude-1`) with three Trello lists and a worktree. |
| **Session** | One run of a launcher on a seat, identified by a random token. |
| **Card ref** | `refs/agent-cards/<card-id>`: records a card's phase and holder from the first claim until `Done`. |
| **Phase** | State of the card in its ref: `impl`, `implemented`, `review`, `integrated`, `parked`. |
| **Supervisor** | Minimal process in the tmux pane that starts the launcher and restarts it if it dies. |
| **Launcher** | Process that holds the seat, starts the agent and drives its cycle. |
| **Pane** | The tmux terminal pane where the agent runs; attaching to the session shows the pane. |
| **Task** | Unit of work assigned to the agent, with an ID: implement, review, or suspend a card. |
| **Convergence** | Launcher step that completes Trello moves lagging behind the phase recorded in the refs. |

## 5. Architecture

### 5.1 Trello board

- **Common lists:** `Backlog`, `Done`, `Blocked`.
- **Lists of each seat:** `<seat> · In Progress`, `<seat> · Implemented`, `<seat> · Review`.
- **Fixed labels:** `HOLD` (set by the user), `Question`, `Error` (set by kankan).
- **Dynamic labels:** `Resume YYYY-MM-DD hh:mm`, created and deleted by kankan.
- **Board description:** contains a delimited block `--- kankan-registry ---` with `registry-id`, hostname and clone path (6.3). The rest of the description is free.

kankan recognizes lists and labels **only by ID**. The user may create other lists and labels and rename or reorder the workflow ones; kankan ignores what it does not know. An archived or deleted workflow list produces `config_error`.

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
| Seat worktrees | `~/coding/.kankan/<project>/<seat>` (configurable root) |
| Worktrees of blocked cards | `~/coding/.kankan/<project>/blocked-<shortLink>` |
| Adapters (user override) | `~/.config/kankan/agents.json` |
| Credentials | 6.2 |

`<gcd>` is `git rev-parse --git-common-dir`: shared by all worktrees, never committed. The `refs/agent-*` refs are not under `refs/heads` or `refs/tags`, so ordinary push and fetch do not transfer them. Lock files are never deleted or replaced. JSON state files are written to a temporary file and renamed atomically.

The worktree root is a hidden directory: it does not show up in menus listing the folders of `~/coding`, and it does not nest worktrees inside the project (agents would read the main project's instructions as those of a parent directory).

### 5.3 Components

kankan is a Python 3 package that uses **only the standard library** (pytest for development only).

| Module | Responsibility |
|---|---|
| `config` | Project configuration, credentials, `.properties` files, project lock. |
| `trello` | REST client: retries, read-after-write, error classification, rate limits. Isolates Trello from the rest (see `kanban_requirements.md`). |
| `refstore` | The only module that reads and writes kankan refs: conditional operations and multi-ref transactions. |
| `seats` | Seats: creation, occupation, flock, liveness, recovery, removal, process data. |
| `cards` | Card ref phases and transitions, convergence. |
| `workflow` | Task choice, eligibility, dependencies, `HOLD` rules. |
| `integrate` | `sync`, `verify`, `integrate`, worktrees of blocked cards. |
| `adapters` | Per-agent-type adapters and screen classification. |
| `launcher` | Supervisor, launcher and its state machine, tmux, pane writes, comment forwarding. |
| `cli` | Commands, text and JSON output, exit codes. |

The **shared skill** (`SKILL.md`, for Claude Code and Codex) explains to the agent how to carry out an assigned task. The work cycle belongs to the launcher.

## 6. Setup

### 6.1 `kankan init <board-url>`

Run once in the project clone (e.g. URL `https://trello.com/b/s5C4vWbG/test1`; kankan extracts the shortLink and derives the board ID). Under the project lock:
1. checks or acquires the credentials (6.2);
2. writes the `kankan-registry` block into the board description, waits a few seconds and reads it back (6.3);
3. finds or creates the lists `Backlog`, `Done`, `Blocked`; if lists with those names exist, asks whether to adopt them;
4. creates the labels `HOLD`, `Question`, `Error`;
5. determines the Trello user to notify (`GET /1/members/me`), overridable with `--notify <user>`;
6. asks for the project's `test_command` (may be left empty; see 12.2);
7. writes `config.json`;
8. installs a `pre-push` hook that refuses pushing `refs/agent-*`; if a hook already exists, reports it without overwriting it;
9. adds the necessary exclusions to `<gcd>/info/exclude`;
10. warns if `main` is checked out in any worktree (12.5).

`init` is idempotent. `init --reauth` repeats only the credential acquisition.

### 6.2 Credentials

Lookup order:
1. environment variables `KANKAN_TRELLO_KEY` and `KANKAN_TRELLO_TOKEN`;
2. the `.properties` file given with `init --credentials <path>` (e.g. the project secrets file in `/srv/<acroenv>/bin/`), keys `kankan.trello.key` and `kankan.trello.token` (configurable names). The path is saved in the configuration; kankan must not know about the acroenv;
3. `~/.config/kankan/credentials` (permissions 600).

If they are missing or invalid, `init` guides the user: asks for the API key (from `trello.com/power-ups/admin`), prints the authorization URL (scope `read,write`, expiration `never`), asks for the token, verifies it with `GET /1/members/me` and saves it.

**Writing the credentials file:** kankan never rewrites the file from scratch. It keeps comments, order and foreign keys, updates its own keys in place and appends missing ones at the end, writes to a temporary file with the same permissions and owner and renames it atomically. It warns if the file is readable by other users. Credentials never appear in logs, comments or output.

Trello's rate limit applies per token: projects using the same token share it.

### 6.3 Board↔registry binding

Two independent registries on the same board would let two agents take the same card.
- `init` generates a random `registry-id`, writes it into the board description and into `config.json`, waits and reads back: if it finds a different `registry-id` it stops and reports the conflict.
- `init` on a board with a different `registry-id` refuses, unless `--takeover` is given. The manual prescribes using `--takeover` only after stopping the other clone's agents.
- The board read the launcher performs on every cycle, and the one performed by state-changing commands, includes the description (a single Trello call returns board, lists, cards, attachments and labels). If the `registry-id` does not match, commands answer `config_error` and the launcher stops assigning tasks, comments on the current card and stops.

Exclusion is not absolute without an external authority; these checks cover the practical cases (a single user running `init`).

## 7. Seats

### 7.1 Creation and removal

Seats are created only by `kankan agent` / `kankan up`, under the project lock:
- without `--seat`: a free seat of the type is used (7.2); if none is free, the next `<type>-<n+1>` is created;
- with `--seat <type>-<n>`: that seat is used, created if it does not exist.

Creating a seat means: finding or creating by name its three lists, registering them in `config.json`, creating the worktree. The operations are repeatable, so an interrupted creation completes on the next attempt. A configurable **per-type cap** (default 4) prevents creation beyond the limit with a clear error. Because dead seats are reused, the number of seats equals the maximum number of agents of that type run at the same time.

`kankan seat remove <seat>`, under the project lock, is allowed only if the seat is free, holds no cards and its lists are empty: it archives the lists, removes the entry from the configuration and the worktree if clean.

### 7.2 Seat choice

Among the free seats of the type, kankan prefers those holding cards (ref in phase `impl` or `review`), then the others in name order.

### 7.3 Occupation

**Invariant:** if a seat's ref exists, its flock is held by the holder or the holder is dead.

The launcher, under the project lock:
1. tries the seat's exclusive **non-blocking** `flock`; if it fails, the seat is alive and it moves on to the next one;
2. if the seat's ref does not exist, creates it;
3. if it exists (flock free: holder dead or dying), applies recovery (7.5).

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

Cards belong to the seat, so the new session inherits the work. A seat that is alive but stuck is never recovered automatically: it requires `kankan seat recover <seat> [--kill]` (15.4). An agent stopped by a usage limit is alive.

## 8. Processes and launcher

### 8.1 Process chain

```
kankan agent claude / kankan up ...   → creates the tmux session and exits
tmux pane └─ kankan supervisor        → starts the launcher and restarts it if it dies
            └─ kankan launcher        → seat, tasks, pane observation
                └─ claude             → the interactive agent
```

`kankan supervisor` and `kankan launcher` are internal commands, hidden from help.

- `kankan agent <type> [--seat <s>] [--attach]` creates a tmux session running the supervisor and, with `--attach`, attaches the user to it.
- `kankan up claude=2 codex=1` is idempotent: it makes sure those agents are running for the project, starting only the missing ones.
- `kankan autostart enable claude=2 codex=1` installs a systemd user service (`kankan-up@<project>.service`) that runs `kankan up` for the project at boot; `kankan autostart disable` removes it. Restarted agents recover their seats (different `boot_id`).

### 8.2 Supervisor

Starts the launcher as a child. If the launcher terminates abnormally, it writes a message in the pane, waits with increasing delays and starts a new one in the same pane, up to a maximum number of restarts; beyond that, it stops leaving the error visible. If the launcher terminates normally (agent exited, `stop`), the supervisor terminates too and the tmux session closes. The new launcher recovers the seat and resumes work with convergence.

### 8.3 Launcher startup

1. Checks configuration and the board↔registry binding; warns if `main` is checked out in any worktree.
2. Occupies the seat (7.3) and creates the worktree if missing.
3. Renames the tmux session to `ai-<project>-<seat>`: the `ai-` prefix and the agent among the pane's descendants make the session visible to `airesumer.sh`.
4. Starts the agent (the adapter's command) as a child, in the worktree, with `KANKAN_SEAT`, `KANKAN_SESSION`, `KANKAN_PROJECT` in the environment. In the child, before starting the agent: sets `PDEATHSIG` and checks that the parent is still the launcher, otherwise exits immediately.
5. The agent stays in the launcher's session and process group and keeps control of the pane's terminal. The launcher ignores `Ctrl-C` and `Ctrl-Z` and never reads the keyboard.
6. Handles the trust window (8.6) and waits for the agent to be ready.

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

1. **Convergence** (10.4) and check of the board↔registry binding.
2. **Task choice** (chapter 9) and preparation before involving the agent: ref transition, verified move, branch and worktree (chapter 11).
3. **Assignment:** the launcher generates a task ID, saves it in the seat state, sends the adapter's new-conversation command (default `/new` for both types) and a one-line prompt, for example `Run the current kankan task (kankan skill).` The agent reads the details with `kankan task --json`.
4. **Execution:** the agent closes the task with a signal (13.2). Agent commands act only on the current task, and the signal written into the seat state carries the task ID; a signal with a different ID is ignored.
5. **Closing:** convergence, waiting for the pane to be idle, back to step 1.

`kankan stop <seat>` records a stop request: the launcher exits after the current task (even if the request arrives in `limited`).

If in `waiting_human` the agent goes back to "working" without the launcher having forwarded anything, the user answered from tmux: the launcher removes the label set by `ask`/`fail` and comments "resumed".

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

An adapter per agent type defines: start command, new-conversation command, submit key, and groups of indicators — *ready*, *working*, *limit* (with the patterns to derive the resume time), *trust* (with the answer), *known dialogs* (with the answer). Each group is a list of regular expressions applied case-insensitively to the text stripped of colour codes; one match is enough, except for groups marked "all" (e.g. dialogs recognizable only by several texts together).

The defaults ship with kankan and start from the patterns of `airesumer.sh`. They are overridden, only for the parts that change, in `~/.config/kankan/agents.json` (user) and `<gcd>/kankan/agents.json` (project).

`kankan adapter check <seat>` shows the current classification of that seat's screen and which expression fired; it also accepts a file with a saved screen.

## 9. Task choice

### 9.1 Priority

1. **Own work:** cards held by the seat (phase `review`, then `impl`).
2. **Review:** eligible cards in the `Implemented` lists.
3. **Implementation:** eligible cards in the `Backlog`, from the top.

### 9.2 Eligibility

Board read in one call (lists, cards, attachments, labels, description). A card is eligible only if its Trello list and the phase of its ref agree:
- **implementation:** card in the `Backlog`, without `HOLD`, with dependencies satisfied, with no ref or with a ref in phase `parked`;
- **review:** card in a seat's `Implemented`, without `HOLD`, with a ref in phase `implemented` (or `parked`) whose implementer is of a **different type** from the requester.

The implementer is read from the card's ref, not from the list. If only one agent type is active, cards stay in `Implemented`; `kankan status` shows why.

### 9.3 Dependencies

A card depends on the Trello cards attached to it ("Add > Attachment" with a card). The attachment exists only on the card that holds it. Attachments whose URL is `https://trello.com/c/<shortLink>`, followed or not by the rest of the address, count as attached cards; other attachments are ignored. Attached cards not present in the board read (archived or on other boards) are read individually by shortLink.

A dependency is satisfied only if the attached card is in the `Done` list of the same board. An attached card that is deleted, unreadable, on another board or archived outside `Done`, or a cycle, make the card ineligible and are reported by `status` and `reconcile`.

### 9.4 `HOLD`

| Moment | Behaviour |
|---|---|
| Choosing a new card | It is not chosen; the card is rechecked after the ref transition. |
| Resuming own work | Not assigned as work: if the worktree is clean kankan runs `yield` itself; if it is dirty it assigns the agent only the task "commit WIP and `yield`". |
| During work | The launcher queues for the agent the request to commit WIP and `yield`, never interrupting it. |
| During `limited` | The request is queued and read on resumption. |
| `integrate` | Rechecks the labels under the project lock; with `HOLD` answers `on_hold` and does not integrate. |
| After `main` has advanced | Irreversible: the card goes to `Done` with the comment "HOLD arrived after integration; to undo: `git revert -m 1 <merge>`". The label stays. |

When the user removes `HOLD`, the card becomes eligible again and whoever picks it up restarts from the existing branch.

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
| User move (14.6) | `impl`/`review` → `parked` | none | list where the user put the card |
| `resolve --done` | `parked` → `implemented` | none | implementer's `Implemented` |

Every transition happens under the project lock: reread of the card, Git transaction (first), Trello move and verification reread (after), comment with an anti-duplicate marker. If the move fails or the reread is ambiguous, the ref stays in the new phase and convergence will complete the move; if the anomaly persists the card gets `Error`.

### 10.4 Convergence

On every cycle, under the project lock, the launcher compares the phase of each card ref with the expected Trello list (`target_list`) and completes the missing moves:
- for phases with a holder (`impl`, `review`) only if the holding seat is its own;
- for phases without a holder (`implemented`, `integrated`, `parked`) any launcher;
- a card already in the right list counts as moved;
- in phase `integrated` with the card verified in `Done`, it deletes the ref.

This way crashes between the Git transition and the Trello move (claim, `done`, `yield`, `block`, `integrate`) resolve themselves. Intermediate states are never eligible for other agents, because eligibility requires agreement between phase and list.

## 11. Branches and worktrees

- **Branch name:** `YYkankan-<shortLink>`, where `YY` is the two-digit year the branch was created and `<shortLink>` is the card's 8-character code present in its URL (`trello.com/c/<shortLink>/…`). Example: `26kankan-Ab12Cd34`. The shortLink is unique across all of Trello, does not change if the card moves to another board and allows finding the card through the API. Trello's interface shows only the title: the comment kankan writes at claim time reports the branch name, and merge commits report the card's title and URL.
- **Start of implementation:** kankan looks for branches `??kankan-<shortLink>`; if one exists it reuses it (resumption after `HOLD`, `block` or a change of year), if more than one `Error`, otherwise it creates `YYkankan-<shortLink>` from the current `main`. Checkout in the seat's worktree, which must be clean.
- **Start of review:** checks with `git worktree list` where the branch is checked out. If it is in a clean `blocked-…` worktree with no merge in progress, it removes that worktree; if the merge is not completed it does not assign the card and comments "merge not completed in `<path>`"; if it is checked out elsewhere, `Error`.
- **Clean worktree:** no modified tracked files and no untracked files not ignored by `.gitignore`. `done`, `yield` and `block` require it (with the exception of an in-progress merge for `block`) and, if it is not, answer `needs_action` listing the files.
- **Leaving every task:** `done`, `yield` and `block` leave the worktree at a detached HEAD, so the branch is free.
- **Resuming with a dirty worktree** (previous session died): `kankan task` reports it and the agent evaluates the changes.
- Branches are not deleted by kankan (deletion after `Done` available as an option, disabled by default).

## 12. Integration into `main`

### 12.1 Who and when

The reviewing agent integrates, before `Done`, without human approval. The reviewer fixes the problems it finds itself, with separate commits on the branch, and summarizes them in a card comment.

### 12.2 Steps

1. **`kankan sync`:** merge of the current `main` into the branch. Conflicts are resolved on the branch and committed; they stay saved for later attempts.
2. **Tests** on the tip of the branch, whose hash the agent notes.
3. **`kankan verify --tested <hash>`** (required if `test_command` is configured): runs `test_command` in the worktree at that hash, with a configurable timeout, and records the outcome in the seat state. It is separate from `integrate` because it may last longer than the agents' shell command timeout; the agent runs it like any long test command.
4. **`kankan integrate --tested <hash>`**, under the project lock:
   - rechecks `HOLD` (9.4) → `on_hold`;
   - if the card's ref is already `integrated`, returns the recorded merge and exits (idempotency);
   - checks that the branch tip is exactly `<hash>`, that the worktree is clean and, if `test_command` is configured, that a successful verification exists for `<hash>` → otherwise `needs_action`;
   - reads `M` = `main`; if `M` is not an ancestor of `<hash>` → `main_moved`;
   - creates with `git commit-tree` the commit `R` with the tree of `<hash>`, first parent `M`, second parent `<hash>`, a message with the card's title and URL and the trailers `Kankan-Card: <shortLink>` and `Kankan-Tested: <hash>`;
   - runs a single transaction: `verify` of the seat's ref, update of the card's ref from `review` to `integrated` (blob with `R` and `<hash>`), update of `main` from `M` to `R`. If it fails because `main` changed → `main_moved`;
   - if configured, `git push origin main`; a failure does not undo the local integration but is commented and reported by `reconcile`.
5. On `main_moved` the agent repeats from step 1; beyond a configurable number of attempts the card gets `Error`.
6. **`kankan done`:** card to `Done`, verification, deletion of the ref (or convergence after a crash).

### 12.3 Guarantees

- Only a version that contains the current `main` and is exactly the one declared as tested enters `main`.
- With `test_command`: kankan saw the tests pass on that version. Without `test_command`: the agent declares it ran them (weaker guarantee).
- If `main` has advanced, the card is certainly `integrated` and vice versa: a new `integrate` does not create a second merge.
- The first-parent line of `main` contains one merge per card and `git revert -m 1 <merge>` undoes exactly one card (no *foxtrot merge*).

### 12.4 Unresolvable conflicts and `block`

If the agent cannot resolve a conflict safely it calls `block "<reason>"`. With a merge in progress kankan:
1. notes the hash of `main` used in the merge and copies the files the agent was working on;
2. aborts the merge in the seat's worktree, which becomes free again;
3. creates the worktree `~/coding/.kankan/<project>/blocked-<shortLink>` on the branch, redoes the same merge with the same hash, puts back the copied files and marks as resolved those the agent had already resolved: the state is identical to the one where the agent stopped;
4. brings the card's ref to `parked` with destination `Blocked` and comments: reason, conflicting files and `To resolve: kankan resolve <shortLink>`.

Without a merge in progress, `block` requires a clean worktree and moves the card to `Blocked`. The card's committed work and `main` are never touched.

**`kankan resolve <shortLink>`** prints the files still in conflict, the steps to follow (edit, `git add`, `git commit`) and how to finish, then opens a tmux session `kankan-resolve-<project>-<shortLink>` (without the `ai-` prefix) in the blocked worktree, where the same instructions are repeated. **`kankan resolve <shortLink> --done`** checks that the merge is complete and the worktree clean (otherwise it repeats the instructions), removes the worktree and moves the card to the implementer's `Implemented`. The reviewer's next `sync` brings in only the changes to `main` that arrived in the meantime; tests and `integrate` guarantee as always that only the updated and tested version enters `main`.

### 12.5 Main worktree

No agent checks out `main`. If the user has `main` checked out, their files fall behind when `main` advances: while the agents work, the main worktree should hold a user branch or be at a detached HEAD. `init` and `agent` report it.

## 13. Interaction with the agent

### 13.1 Skill

The `SKILL.md` skill instructs the agent to:
1. read the task with `kankan task --json`;
2. implementation: work on the prepared branch, commit, run the tests, close with `done`;
3. review: read the differences from `main`, fix with separate commits, then `sync`, tests, `verify`, `integrate` (repeating on `main_moved`), finally `done`;
4. publish progress with `kankan note` when useful;
5. use `ask` before stopping for a question, `fail` for an error that needs the user, `block` for an obstacle that prevents completing the card;
6. on a suspension request or on `on_hold`, make a WIP commit and call `yield`;
7. never move cards on Trello and never touch `refs/agent-*`;
8. always use `--json` and stop on `needs_reconciliation`, `not_owner` or unexpected errors.

### 13.2 Signals

| Command | Effect | Launcher |
|---|---|---|
| `done` | Transition of 10.3 (implementation → `implemented`; review → closing) | next task |
| `ask "<question>"` | `Question` + comment with question and mention | `waiting_human` |
| `fail "<reason>"` | `Error` + comment with reason and mention | `waiting_human` |
| `block "<reason>"` | 12.4 | next task |
| `yield` | Transition of 10.3 back to the origin | next task |

`done` verifies the Git side: clean worktree; for implementation at least one commit beyond `main`; for review a ref in phase `integrated`. If the verification fails it answers `needs_action` and the agent fixes it.

## 14. Communication through Trello

### 14.1 Labels

| Label | Set by | Meaning |
|---|---|---|
| `HOLD` | user | Do not assign; if in progress, suspend (9.4). |
| `Question` | kankan (`ask`) | The agent is waiting for an answer. |
| `Error` | kankan (`fail`, protocol, inactivity, unknown screen) | Intervention needed. |
| `Resume YYYY-MM-DD hh:mm` | launcher | Agent stopped by a usage limit until that date and time. |

The fixed labels are created by `init` and recognized by ID. kankan adds and removes labels **one at a time** and never rewrites the whole list, so it does not delete a `HOLD` the user just set. The seat state records the cause of every label set: a user answer removes only the label created by `ask`/`fail` on that card. kankan removes its own labels when the cause ends and when the card reaches `Done`.

`Resume` labels are removed from the card when the agent resumes. They are deleted from the board, under the project lock, when no card uses them or when their date passed more than an hour ago.

### 14.2 Notifications

Every `Question`, `Error` and `Blocked` comes with a comment mentioning the configured user, which generates a Trello notification.

### 14.3 Answers through comments

In `waiting_human`, the launcher reads the new comments of the configured members posted after the question and forwards them to the agent in the pane (8.6). The cursor of the last forwarded comment is saved **before** forwarding: a comment is never forwarded twice, but a crash can lose one. After forwarding the launcher removes the label and replies "forwarded to the agent"; if this confirmation is missing, the user repeats the comment. Alternatively the user answers by attaching to the tmux session.

### 14.4 What the user does

Golden rule (to be stated in the manual): **never move cards that are in the seats' lists; to stop a card use `HOLD` and wait for kankan to bring it back.** The user may freely:
- create cards in the `Backlog` and reorder them;
- add and remove `HOLD`, add dependency attachments, write comments;
- unblock a card from `Blocked` with `kankan resolve … --done`, or by moving it to the `Backlog` to have it reimplemented (safe: the card has no holder).

### 14.5 Precedence of human changes

Trello has no conditional updates: if the user moves a card exactly while kankan moves it, one of the two changes may be overwritten. With the golden rule this case does not occur (labels and list are independent fields); precedence of human changes to the list is therefore *best effort*, with a window of a few seconds, and residual inconsistencies are reported by `reconcile`.

### 14.6 Cards moved by the user (safety net)

If the user moves out of the seat's lists, archives or deletes a card in progress, their intent prevails: the launcher queues for the agent the request to commit WIP and `yield`; the ref goes to `parked` with the current list as destination (no move) and the card stays where the user put it.

## 15. Errors and recovery

### 15.1 Principles

- **Trello reads:** retry with increasing delay on network errors, 5xx and 429; short network timeouts, because calls happen under the project lock.
- **Trello writes:** after a timeout, read back before retrying; comments contain a marker with the operation ID.
- **Intermediate Git/Trello state:** resolved by convergence (10.4).
- **Unexpected Git errors:** fail closed, `Error`, comment.

### 15.2 Situations and behaviour

| Situation | Behaviour |
|---|---|
| Another agent takes the card first | Next card. |
| Crash between Git transition and Trello move | Convergence on the next cycle. |
| Move that keeps failing or inconsistent reread | `Error`; no task assigned on that card. |
| Crash after `main` has advanced | Ref already `integrated`; convergence brings the card to `Done`. |
| The agent dies | The launcher terminates the remaining processes and releases the seat; the cards stay with the seat. |
| The launcher dies | The agent dies via `PDEATHSIG`; the supervisor starts a new launcher that recovers the seat. |
| The supervisor dies | Dead seat, recovered at the next `kankan agent`/`up`. |
| Server reboot | Different `boot_id`; `autostart` (if enabled) runs `kankan up`; automatic recovery. |
| Command started before a recovery or an administrative action | The transaction fails on the seat `verify` or on the card ref → `not_owner`. |
| Agent alive but stuck | No automatic recovery; `Error` for inactivity; manual recovery. |
| Usage limit | `limited`, `Resume`; resumed by `airesumer.sh`. |
| `main_moved` beyond the limit | `Error`. |
| Unresolvable conflict | `block` (12.4). |
| Different `registry-id` | Commands `config_error`; the launcher stops. |
| Workflow list archived or deleted | `config_error`, startup refused. |
| Ref pointing to an unexpected object or malformed blob | `needs_reconciliation`, no automatic action. |

### 15.3 `kankan reconcile [--fix]`

By default report only:
- card refs whose phase has not matched the list for more than one cycle;
- cards in seat lists without a ref; refs of deleted cards; malformed blobs;
- cards in `Done` without a matching merge in `main` (`Kankan-Card` trailer);
- registered dead seats; seats without their lists; seats unavailable because of surviving processes;
- unsatisfiable dependencies; more than one `??kankan-<shortLink>` branch for the same card; `blocked-…` worktrees of cards no longer in `Blocked`;
- `Error` or `Question` labels older than a threshold; failed push of `main`;
- `main` checked out in a worktree; invalid board↔registry binding.

With `--fix`, under the project lock and skipping cards whose holder is alive, it repairs only the certain cases: deletes `integrated` refs of cards already in `Done`, deletes unused or expired `Resume` labels, removes kankan labels from cards in `Done`.

### 15.4 Administration commands

Under the project lock, conditional on the value read, recorded in the log:
- `kankan release <card> --force`: brings the card's ref to `parked` (the card stays where it is);
- `kankan seat recover <seat> [--kill]`: recovers a seat that is alive but stuck; with `--kill` it first terminates the processes of the recorded session;
- `kankan purge-remote`: deletes from the remote any `refs/agent-*` published by mistake.

## 16. CLI

### 16.1 User commands

| Command | Description |
|---|---|
| `kankan init <url> [--credentials <file>] [--notify <user>] [--takeover] [--reauth]` | Project setup (6.1). |
| `kankan agent <type> [--seat <s>] [--attach]` | Starts an agent in tmux. |
| `kankan up <type>=<n> …` | Ensures the requested number of agents, starting the missing ones. |
| `kankan autostart enable <type>=<n> …` / `autostart disable` | Automatic start at boot via systemd user service. |
| `kankan stop <seat>` | Exits after the current task. |
| `kankan status` | Seats (alive, dead, free, unavailable), launcher state, current task, waiting cards and reason. |
| `kankan claims` | Card refs with phase, holder, age and branch. |
| `kankan resolve <shortLink> [--done]` | Manual resolution of a blocked card (12.4). |
| `kankan reconcile [--fix]` | 15.3. |
| `kankan adapter check <seat>\|<file>` | Screen classification (8.7). |
| `kankan seat remove <seat>` | 7.1. |
| `kankan release <card> --force`, `kankan seat recover <s> [--kill]`, `kankan purge-remote` | 15.4. |

### 16.2 Agent commands

They require `KANKAN_SEAT` and `KANKAN_SESSION`, verify ownership and act on the current task: `task`, `note`, `sync`, `verify --tested <hash>`, `integrate --tested <hash>`, `done`, `ask`, `fail`, `block`, `yield`.

### 16.3 Internal commands

`supervisor`, `launcher`: started by `agent`/`up`, hidden from help.

### 16.4 Responses

Text output by default; with `--json` a fixed envelope with the fields `ok`, `status`, `message`, `data`.

| Exit | `status` | Meaning |
|---|---|---|
| 0 | `success` | Done. |
| 10 | `no_work` | No task available. |
| 11 | `already_claimed` | Taken by someone else. |
| 12 | `ineligible` | Card not (or no longer) eligible. |
| 13 | `main_moved` | Repeat `sync`, tests, `verify`, `integrate`. |
| 14 | `needs_action` | Failed check the agent can fix; the message explains what to do. |
| 15 | `on_hold` | The card is on `HOLD`: commit WIP and `yield`. |
| 20 | `network_error` | Temporary, retryable. |
| 21 | `trello_error` | Unexpected Trello response. |
| 30 | `needs_reconciliation` | Ambiguous state: stop. |
| 31 | `not_owner` | Card or seat not one's own: stop. |
| 40 | `config_error` | Missing or invalid configuration. |
| 41 | `usage_error` | Wrong arguments. |
| 50 | `internal_error` | kankan error. |

## 17. Security

- Credentials only in the sources of 6.2, never in configuration, logs, comments, blobs or output.
- Write access to the clone limited to the user running the agents; whoever can write the refs can bypass the protocol.
- Blobs contain hostname, user and paths: the refs must not be published (`pre-push` hook, `purge-remote`).
- Inputs validated before reaching Git or the shell; no Trello text interpolated into shell commands.
- Text forwarded to the agent comes only from the configured members.
- Automatic answer to the trust window limited to worktrees under the kankan root (real path).

## 18. Tests

### 18.1 Strategy

- **Unit:** `refstore`, `config`, `.properties` files, phases and transitions, convergence, task choice, dependencies, adapter classification.
- **Local integration:** fake Trello server (subset of the API used, with injection of timeouts, 5xx, 429, inconsistent responses and successful writes with a late response), real Git in temporary directories, real tmux and a fake agent (a script that simulates ready, working, limit, trust, known dialogs and calls kankan according to a scenario).
- **Fault injection** at every boundary between one effect and the next (simulated crash between Git transaction and Trello move, between advancing `main` and `done`, during seat creation, during comment forwarding).
- **On real Trello:** tests marked `trello_live` on a dedicated test board.

### 18.2 Test board

- Configuration outside the code and the repository, in this order: variables `KANKAN_TEST_BOARD_URL` and `KANKAN_TEST_CREDENTIALS` (path of a `.properties` file); file `~/.config/kankan/test.properties` with `kankan.test.board.url`, `kankan.trello.key`, `kankan.trello.token`.
- Without configuration the `trello_live` tests are skipped with an explicit message.
- Before any write the tests check that the board description contains the marker `kankan-test-board`; otherwise they stop.
- Each run uses a unique prefix for lists, cards and labels; at the end it deletes cards and labels and archives the lists. A cleanup option archives the leftovers of interrupted runs.

### 18.3 Acceptance tests

**Ownership and concurrency**
1. 20 parallel attempts on the same card: exactly one succeeds.
2. Two seats on different cards proceed in parallel.
3. Two agents start from a stale board read: only one gets the card.
4. Repeating a creation after an ambiguous failure recognizes one's own ref.
5. A conditional change does not modify a ref replaced in the meantime.
6. An `integrate` started before `seat recover` or `release --force` does not advance `main`.

**Seats and processes**

7. 10 parallel starts of the same type with no seats: no duplicates, no duplicate lists; beyond the cap, a clear error.
8. `kill -9` of the launcher: the agent dies, the supervisor starts a new launcher that resumes the card.
9. `kill -9` of supervisor and launcher: at the next `kankan up` the seat is recovered and the card resumed.
10. A surviving child process of the agent is terminated before the worktree is reused.
11. A seat that is alive but stuck is never recovered automatically.
12. A zombie session gets `not_owner`.
13. Simulated reboot (different `boot_id`): automatic recovery.
14. `kankan up` starts only the missing agents; `autostart enable` generates a service that runs it.
15. Two projects on the same server do not interfere (seats with the same name, distinct tmux sessions).

**Workflow**

16. Order: own work, then review, then `Backlog` by position.
17. Review only by a type different from the implementer recorded in the ref, even if the card is moved by hand into another `Implemented`.
18. `HOLD` matrix of 9.4, including `on_hold` from `integrate` and `HOLD` after integration.
19. Dependencies through attachments: waiting until `Done`; missing attachment, attachment from another board or cycle reported.
20. Foreign lists ignored; renamed workflow lists working; archived workflow list → `config_error`.
21. Branch `YYkankan-<shortLink>`: reuse after `HOLD` and after a change of year; two branches for the same card → `Error`.
22. Crash at every boundary of claim, `done`, `yield`, `block`: convergence always leads to a consistent state and no card is taken by two seats.

**Integration**

23. Two reviewers integrate together: both cards enter `main`, one gets `main_moved`; one merge per card along the first parent; `revert -m 1` undoes the right card.
24. The merge commit has the same tree as the tested version; `integrate` refuses a tip different from `--tested` and, with `test_command`, an unverified version.
25. Crash after `main` has advanced: no second merge; the card reaches `Done`.
26. `block` with a merge in progress: `blocked-…` worktree with identical state, seat free, `main` untouched; `resolve --done` brings the card to `Implemented`; the next reviewer integrates after a `sync` without conflicts.

**Trello errors**

27. Timeout on the move after the transition: no task assigned, later convergence, no duplicate comment.
28. Card moved by the user during work: `yield`, card where the user put it.

**Pane and communication**

29. `HOLD` during work: message queued, WIP commit, `yield`, card back to its origin; never `Esc`.
30. `ask`: label and mention; no inactivity `Error` in `waiting_human`; the user's comment is forwarded only once and the label removed; answer from tmux detected.
31. Usage limit: `Resume YYYY-MM-DD hh:mm`, removed on resumption; expired labels deleted from the board; an old limit message outside the last lines is not considered.
32. Pane "ready" without signals in `working`: `Error`. Unknown screen: no writes.
33. User attached and typing: the launcher's writes are postponed.
34. Signal with an old task ID: ignored.
35. Trust window answered automatically only in kankan worktrees; known dialogs answered with the configured answer.
36. `adapter check` recognizes the states from saved screens; an override in `agents.json` changes the classification.

**Setup and security**

37. `init` with an existing `.properties` file keeps all its content.
38. A second `init` on the same board refused; with `--takeover` accepted; the other registry's launchers stop at the next cycle.
39. The `pre-push` hook blocks `refs/agent-*`.
40. `airesumer.sh` (`discover_panes`) finds the `ai-…` session and the agent; `kankan-resolve-…` sessions are ignored.

## 19. Implementation phases

All phases are designed in this specification; the order makes each step verifiable on its own.

| Phase | Content | Tests |
|---|---|---|
| 1. Foundations | `config`, `.properties`, project lock; `init` with board↔registry binding, lists, labels, hook; Trello client and fake server; `refstore` with transactions; seats (creation, occupation, flock, recovery, process data); card refs with phases and convergence; claim from the `Backlog`; branches; `task` and `done` for implementation; JSON and exit codes | 1–7, 11–13, 20–22, 27, 37–39 |
| 2. Processes and launcher | `agent`, `up`, `autostart`, supervisor, launcher and state machine, adapters and `adapter check`, pane observation and writes, trust window, `Resume`, `SKILL.md`, fake agent | 8–10, 14, 15, 31–36, 40 |
| 3. Review and integration | review claim, `sync`, `verify`, `integrate` with transaction and receipt, `block` with dedicated worktree, `resolve` | 16, 17, 23–26 |
| 4. Communication and policies | `HOLD` and `yield`, `ask`/`fail` with comment forwarding, dependencies, cards moved by the user | 18, 19, 28–30 |
| 5. Diagnostics and documentation | `status`, `claims`, `reconcile [--fix]`, `release --force`, `seat recover`, `seat remove`, `purge-remote`, user manual in Italian | cross-cutting |

## 20. Out of scope

- Agents on different servers or in different process namespaces; remote registries.
- Several repositories per project or several boards per repository.
- Human approval before integration.
- A web interface of its own: the UI is Trello.
- Absolute guarantee of precedence of human changes to a card's list (14.5).
- Backends other than Trello: the architecture makes them possible (`kanban_requirements.md`), but the first version supports only Trello.

## 21. To be verified during planning

Items with an already defined fallback behaviour, to be confirmed on the installed versions:
- the `/new` command in Codex and in Claude Code (configurable per type);
- whether Codex queues messages typed while it works, as Claude Code does; if not, the launcher waits for the "ready" state before sending the suspension request;
- the texts of the trust window, the limit messages and the known dialogs of both agents (configurable);
- whether Trello notifies label additions (the notification is guaranteed anyway by the comment with a mention);
- the minimum versions of Python, Git and tmux with respect to the features used;
- the behaviour of `PDEATHSIG` and of terminal control with the interactive interfaces of Claude Code and Codex.
