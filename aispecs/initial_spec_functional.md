# kankan — functional specification

**Status:** specification v1.3 (v1.3: card attachments as task inputs, [13.3](#133-card-attachments))
**Date:** 2026-11-10
**Companion:** [`initial_spec_technical.md`](initial_spec_technical.md) — how kankan implements this behaviour.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (requirements for backends other than Trello)
**Feature specifications:** the kankan specification is the set of specification files in this folder: this initial pair and one pair per feature, `<feature>_spec_functional.md` and `<feature>_spec_technical.md`, each with its own numbering and a chapter stating how the feature changes the initial behaviour. Features: [board settings](boardsettings_spec_functional.md) (draft, not implemented).
**Language:** everything is in English: this specification, the implementation plan, the code, code comments and the user manual.

**Split rule.** This file describes what users and agents observe: the board, the CLI, the workflow, labels and comments, branches, guarantees and acceptance tests. The technical file describes how: modules, local files, refs, locks, processes, protocols and test strategy. Both files share one section numbering; a number missing here lives in the other file.

## 1. Purpose

kankan lets several AI coding agents (Claude Code and Codex CLI) work in parallel on the same project, taking tasks from a Trello board and handing work to each other: one agent implements a card, an agent of a different type reviews it, integrates it into the integration branch and closes it.

The user creates and orders the cards on Trello, starts the agents and steps in only when an agent asks or when they decide to stop a card. kankan guarantees that two agents never work on the same card and that only reviewed and tested code enters the integration branch, which the user merges into their own branches when they choose.

kankan is meant to become an open source project: it contains no hard-wired references to a specific user's boards, paths or credentials.

## 2. Scope and assumptions

- All agents run **on the same Linux server**, in the same process namespace.
- A **project** is a Git clone in `~/coding/<project>` bound to **one** Trello board. All agents of a project work on worktrees of the same clone and therefore share the same Git ref store. Several projects can coexist on the same server, each with its own state.
- The agents integrate into a dedicated **integration branch**, `kankan-agents` by default (`integration_branch` in the configuration); only kankan moves it and nobody checks it out. Every other branch belongs to the user, except the card branches `??kankan-<shortLink>` ([12.5](#125-integration-branch-and-user-branches)). The user's own main branch is called the *user branch* (`user_branch`, default `main`); kankan never changes it. Configurations written before v1.2 with `main_branch` keep integrating into that branch. Some names keep `main` for compatibility although they refer to the integration branch: the status `main_moved`, the configuration key `push_main` and the field `main` of `kankan task`.
- Agents run in **interactive** mode inside **tmux** sessions, so the user can attach and watch them, and the external tool `airesumer.sh` can resume them after a usage limit.
- tmux sessions survive SSH disconnection. Requirements: `KillUserProcesses=no` in logind (Ubuntu default) and, for automatic start, lingering enabled (`loginctl enable-linger`).
- Trello is reachable via REST with a user API key and token. Trello does **not** offer conditional updates.
- Participants cooperate: they change in-progress cards, claims and seats only through kankan.
- Minimum versions: Python 3.10, Git 2.38, tmux 3.2 (`kankan doctor` checks Git and tmux, [15.5](#155-kankan-doctor---no-fix---offline)).

## 4. Glossary

| Term | Meaning |
|---|---|
| **Type** | Agent type: `claude` or `codex`. Determines the adapter ([8.7](initial_spec_technical.md#87-adapters)) and the cross-review rule. |
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
- **Board description:** contains a delimited block `--- kankan-registry ---` with `registry-id`, hostname and clone path ([6.3](#63-boardregistry-binding)). The rest of the description is free.

kankan recognizes lists and labels **only by ID**. The user may create other lists and labels and rename or reorder the workflow ones; kankan ignores what it does not know. An archived or deleted workflow list produces `config_error`.

## 6. Setup

### 6.1 `kankan init <board-url>`

Run once in the project clone (e.g. URL `https://trello.com/b/s5C4vWbG/test1`; kankan extracts the shortLink and derives the board ID). Under the project lock:
1. checks or acquires the credentials ([6.2](#62-credentials));
2. writes the `kankan-registry` block into the board description, waits a few seconds and reads it back ([6.3](#63-boardregistry-binding));
3. finds or creates the lists `Backlog`, `Done`, `Blocked`; if lists with those names exist, asks whether to adopt them;
4. creates the labels `HOLD`, `Question`, `Error`;
5. records kankan's own Trello account, the token's owner (`GET /1/members/me`); warns when that account is an admin of the board, the sign of a person's own token: kankan needs its own account, invited to the board (14.2);
6. asks for the project's `test_command` (may be left empty; see [12.2](#122-steps));
7. asks for the integration branch (default `kankan-agents`) and records the user branch (`main` if it exists, otherwise the checked-out branch); if the integration branch does not exist locally it adopts `origin/<name>` when present, otherwise creates it from the user branch, never resetting or force-pushing; changing the integration branch of a configured clone is refused while a seat is alive or a card ref exists;
8. writes `config.json` and `git config kankan.integrationBranch`;
9. installs a `pre-push` hook that refuses pushing `refs/agent-*` and pushing to the integration branch except from `integrate` (`KANKAN_PUSH=1`); if a foreign hook already exists, reports it without overwriting it;
10. adds the necessary exclusions to `<gcd>/info/exclude`;
11. warns if the integration branch is checked out in any worktree ([12.5](#125-integration-branch-and-user-branches)).

`init` is idempotent. `init --reauth` repeats only the credential acquisition.

### 6.2 Credentials

Lookup order:
1. environment variables `KANKAN_TRELLO_KEY` and `KANKAN_TRELLO_TOKEN`;
2. the `.properties` file given with `init --credentials <path>` (e.g. the project secrets file in `/srv/<acroenv>/bin/`), keys `kankan.trello.key` and `kankan.trello.token` (configurable names). The path is saved in the configuration; kankan must not know about the acroenv;
3. `~/.config/kankan/credentials` (permissions 600).

If they are missing or invalid, `init` guides the user: asks for the API key (from `trello.com/apps/admin`, logged in as kankan's own Trello account: app, key and token belong to a dedicated bot account invited to the user's boards, so that kankan's mentions notify the user), prints the authorization URL (scope `read,write`, expiration `never`), asks for the token, verifies it with `GET /1/members/me` and saves it.

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
- without `--seat`: a free seat of the type is used ([7.2](#72-seat-choice)); if none is free, the next `<type>-<n+1>` is created;
- with `--seat <type>-<n>`: that seat is used, created if it does not exist.

Creating a seat means: finding or creating by name its three lists, registering them in `config.json`, creating the worktree. The operations are repeatable, so an interrupted creation completes on the next attempt. A configurable **per-type cap** (default 4) prevents creation beyond the limit with a clear error. Because dead seats are reused, the number of seats equals the maximum number of agents of that type run at the same time.

`kankan seat remove <seat>`, under the project lock, is allowed only if the seat is free, holds no cards and its lists are empty: it archives the lists, removes the entry from the configuration and the worktree if clean.

### 7.2 Seat choice

Among the free seats of the type, kankan prefers those holding cards (ref in phase `impl` or `review`), then the others in name order.

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

A card depends on the Trello cards attached to it ("Add > Attachment" with a card). The attachment exists only on the card that holds it. Attachments whose URL is `https://trello.com/c/<shortLink>`, followed or not by the rest of the address, count as attached cards; other attachments are not dependencies but task inputs for the agent ([13.3](#133-card-attachments)). Attached cards not present in the board read (archived or on other boards) are read individually by shortLink.

A dependency is satisfied only if the attached card is in the `Done` list of the same board. An attached card that is deleted, unreadable, on another board or archived outside `Done`, or a cycle, make the card ineligible and are reported by `status` and `reconcile`.

### 9.4 `HOLD`

| Moment | Behaviour |
|---|---|
| Choosing a new card | It is not chosen; the card is rechecked after the ref transition. |
| Resuming own work | Not assigned as work: if the worktree is clean kankan runs `yield` itself; if it is dirty it assigns the agent only the task "commit WIP and `yield`". |
| During work | The launcher queues for the agent the request to commit WIP and `yield`, never interrupting it. |
| During `limited` | The request is queued and read on resumption. |
| `integrate` | Rechecks the labels under the project lock; with `HOLD` answers `on_hold` and does not integrate. |
| After the integration branch has advanced | Irreversible: the card goes to `Done` with the comment "HOLD arrived after integration; to undo: `git revert -m 1 <merge>`". The label stays. |

When the user removes `HOLD`, the card becomes eligible again and whoever picks it up restarts from the existing branch.

## 11. Branches and worktrees

- **Branch name:** `YYkankan-<shortLink>`, where `YY` is the two-digit year the branch was created and `<shortLink>` is the card's 8-character code present in its URL (`trello.com/c/<shortLink>/…`). Example: `26kankan-Ab12Cd34`. The shortLink is unique across all of Trello, does not change if the card moves to another board and allows finding the card through the API. Trello's interface shows only the title: the comment kankan writes at claim time reports the branch name, and merge commits report the card's title and URL.
- **Start of implementation:** kankan looks for branches `??kankan-<shortLink>`; if one exists it reuses it (resumption after `HOLD`, `block` or a change of year), if more than one `Error`, otherwise it creates `YYkankan-<shortLink>` from the current integration branch. Checkout in the seat's worktree, which must be clean.
- **Start of review:** checks with `git worktree list` where the branch is checked out. If it is in a clean `blocked-…` worktree with no merge in progress, it removes that worktree; if the merge is not completed it does not assign the card and comments "merge not completed in `<path>`"; if it is checked out elsewhere, `Error`.
- **Clean worktree:** no modified tracked files and no untracked files not ignored by `.gitignore`. `done`, `yield` and `block` require it (with the exception of an in-progress merge for `block`) and, if it is not, answer `needs_action` listing the files.
- **Leaving every task:** `done`, `yield` and `block` leave the worktree at a detached HEAD, so the branch is free.
- **Resuming with a dirty worktree** (previous session died): `kankan task` reports it and the agent evaluates the changes.
- Branches are not deleted by kankan (deletion after `Done` available as an option, disabled by default).

## 12. Integration

### 12.1 Who and when

The reviewing agent integrates, before `Done`, without human approval. The reviewer fixes the problems it finds itself, with separate commits on the branch, and summarizes them in a card comment.

### 12.2 Steps

1. **`kankan sync`:** merge of the current integration branch into the card branch. Conflicts are resolved on the branch and committed; they stay saved for later attempts.
2. **Tests** on the tip of the branch, whose hash the agent notes.
3. **`kankan verify --tested <hash>`** (required if `test_command` is configured): runs `test_command` in the worktree at that hash, with a configurable timeout, and records the outcome in the seat state. It is separate from `integrate` because it may last longer than the agents' shell command timeout; the agent runs it like any long test command.
4. **`kankan integrate --tested <hash>`**, under the project lock:
   - rechecks `HOLD` ([9.4](#94-hold)) → `on_hold`;
   - if the card's ref is already `integrated`, returns the recorded merge and exits (idempotency);
   - checks that the branch tip is exactly `<hash>`, that the worktree is clean and, if `test_command` is configured, that a successful verification exists for `<hash>` → otherwise `needs_action`;
   - reads `M` = the integration branch; if `M` is not an ancestor of `<hash>` → `main_moved`;
   - creates with `git commit-tree` the commit `R` with the tree of `<hash>`, first parent `M`, second parent `<hash>`, a message with the card's title and URL and the trailers `Kankan-Card: <shortLink>` and `Kankan-Tested: <hash>`;
   - runs a single transaction: `verify` of the seat's ref, update of the card's ref from `review` to `integrated` (blob with `R` and `<hash>`), update of the integration branch from `M` to `R`. If it fails because the integration branch changed → `main_moved`;
   - if `push_main` is set, push of `R` to the integration branch on `origin`; a failure does not undo the local integration but is commented and reported by `reconcile`.
5. On `main_moved` the agent repeats from step 1; beyond a configurable number of attempts the card gets `Error`.
6. **`kankan done`:** card to `Done`, verification, deletion of the ref (or convergence after a crash).

### 12.3 Guarantees

- Only a version that contains the current integration branch and is exactly the one declared as tested enters the integration branch.
- With `test_command`: kankan saw the tests pass on that version. Without `test_command`: the agent declares it ran them (weaker guarantee).
- If the integration branch has advanced, the card is certainly `integrated` and vice versa: a new `integrate` does not create a second merge.
- The first-parent line of the integration branch contains one merge per card and `git revert -m 1 <merge>` undoes exactly one card (no *foxtrot merge*).

### 12.4 Unresolvable conflicts and `block`

If the agent cannot resolve a conflict safely it calls `block "<reason>"`. With a merge in progress kankan:
1. notes the hash being merged (`MERGE_HEAD`: the integration branch for a `sync`, the requested commit for a merge-request card) and copies the files the agent was working on;
2. aborts the merge in the seat's worktree, which becomes free again;
3. creates the worktree `~/coding/.kankan/<project>/blocked-<shortLink>` on the branch, redoes the same merge with the same hash, puts back the copied files and marks as resolved those the agent had already resolved: the state is identical to the one where the agent stopped;
4. brings the card's ref to `parked` with destination `Blocked` and comments: reason, conflicting files and `To resolve: kankan resolve <shortLink>`.

Without a merge in progress, `block` requires a clean worktree and moves the card to `Blocked`. The card's committed work and the integration branch are never touched.

**`kankan resolve <shortLink>`** prints the files still in conflict, the steps to follow (edit, `git add`, `git commit`) and how to finish, then opens a tmux session `kankan-resolve-<project>-<shortLink>` (without the `ai-` prefix) in the blocked worktree, where the same instructions are repeated. **`kankan resolve <shortLink> --done`** checks that the merge is complete and the worktree clean (otherwise it repeats the instructions), removes the worktree and moves the card to the implementer's `Implemented`. The reviewer's next `sync` brings in only the changes to the integration branch that arrived in the meantime; tests and `integrate` guarantee as always that only the updated and tested version enters it.

### 12.5 Integration branch and user branches

**One writer per branch.** Only kankan moves the integration branch (through `integrate`'s transaction) and nobody checks it out; `init`, `agent` and `doctor` warn if it is checked out. The user's branches are never changed by kankan or the agents, so the user can check them out and commit freely. Cross-merges only read the other side:

- **Agents → user:** `git merge kankan-agents` on any user branch (or `git pull origin kankan-agents` from another clone, when `push_main` publishes it).
- **User → agents:** `kankan merge-request [<branch>] [--remote] [--bottom]` creates a card at the top of `Backlog` titled `Merge branch <branch> into <integration>`. The branch must be a plain branch name that is not reserved (the integration branch, `??kankan-<shortLink>`); it is resolved to a commit at request time (local branch, or with `--remote` or when only the remote has it, a fetch into `refs/remotes/origin/<branch>` that never touches user branches) and the card names that SHA. A commit already contained in the integration branch is refused ("nothing to merge"). The card goes through the normal workflow: the implementer merges the SHA and resolves conflicts, the reviewer checks the resolutions, `sync`, `verify` and `integrate` apply as for any card. If there is nothing new to merge when the card runs, the implementer makes an empty commit so that `done` succeeds.
- **Divergence:** `kankan status` and `kankan doctor` report how many commits the integration branch is ahead of and behind the user branch, and `doctor` warns when an integration has been waiting more than 7 days.

The `pre-push` hook enforces the single writer only for pushes from the kankan clone; a push to `origin/<integration>` from another clone is not prevented.

## 13. Interaction with the agent

### 13.1 Skill

The [`SKILL.md`](../kankan/skill/SKILL.md) skill instructs the agent to:
1. read the task with `kankan task --json`, including the card's attachments ([13.3](#133-card-attachments)), which are part of the card's specification;
2. implementation: work on the prepared branch, commit, run the tests, close with `done`;
3. review: read the differences from the integration branch, fix with separate commits, then `sync`, tests, `verify`, `integrate` (repeating on `main_moved`), finally `done`;
4. publish progress with `kankan note` when useful;
5. use `ask` before stopping for a question, `fail` for an error that needs the user, `block` for an obstacle that prevents completing the card;
6. on a suspension request or on `on_hold`, make a WIP commit and call `yield`;
7. never move cards on Trello and never touch `refs/agent-*`;
8. always use `--json` and stop on `needs_reconciliation`, `not_owner` or unexpected errors.

### 13.2 Signals

| Command | Effect | Launcher |
|---|---|---|
| `done` | Transition of [10.3](initial_spec_technical.md#103-phases-and-transitions) (implementation → `implemented`; review → closing) | next task |
| `ask "<question>"` | `Question` + comment with question and mention | `waiting_human` |
| `fail "<reason>"` | `Error` + comment with reason and mention | `waiting_human` |
| `block "<reason>"` | [12.4](#124-unresolvable-conflicts-and-block) | next task |
| `yield` | Transition of [10.3](initial_spec_technical.md#103-phases-and-transitions) back to the origin | next task |

`done` verifies the Git side: clean worktree; for implementation at least one commit beyond the integration branch; for review a ref in phase `integrated`. If the verification fails it answers `needs_action` and the agent fixes it.

### 13.3 Card attachments

The user can attach files and links to a card (screenshots, mock-ups, logs, documents, reference pages); kankan hands them to the agent working on the card, for implementation and for review alike. A Trello card has three kinds of attachments:

| Kind | Recognized by | Handling |
|---|---|---|
| **Card link** | URL `https://trello.com/c/<shortLink>…` | Dependency ([9.3](#93-dependencies)); not a task input. |
| **File** | uploaded to Trello (`isUpload`) | Downloaded by kankan for the agent. |
| **Link** | any other URL | Passed to the agent as a URL; kankan does not fetch it. |

**Who can supply inputs.** Only attachments added by board members other than kankan's own account are task inputs, the same rule as for forwarded comments ([14.3](#143-answers-through-comments)). Other attachments are listed with status `not_member`, without URL and without download.

**Download.** `kankan task` reads the card's attachments (ID, name, file name, URL, size, MIME type, uploader, upload flag) and the board's members, and downloads every file that is not already present:
- into `<gcd>/kankan/attachments/<shortLink>/<attachment-id>/<file name>`, outside every worktree: the files never dirty a worktree, are never committed by accident and are shared by the implementer's and the reviewer's seats; the agent reads them by path and, when a file belongs in the repository (e.g. an image asset), copies it there and commits it;
- under the file name Trello records for the upload (renaming the attachment does not change it), sanitized (path separators, `..`, leading dots, control characters and characters the file system cannot encode replaced; an empty name becomes `file`; a name longer than 255 bytes in UTF-8 is shortened, keeping its extension);
- writing to a temporary file in the same directory and renaming it atomically; a file already present with the expected size is not downloaded again (when Trello reports no size, a copy not larger than `attachment_max_bytes` is reused);
- only from the URL Trello gives for that card's attachment (`https://trello.com/1/cards/<card-id>/attachments/<attachment-id>/download/…`, also accepted with the API host); any other URL on a file attachment gives status `unexpected_url`;
- authenticating with the API credential in an HTTP header, never in the URL; the credential is sent only to the Trello API host and never follows a redirect to another host, nor a later redirect back to the API host;
- only up to `attachment_max_bytes` per file (configuration, default 25 MiB, 26 214 400 bytes); a larger file gets status `too_large` and is not downloaded.

The next `kankan task` deletes every local copy that is not a file with status `ok` (attachment removed from the card, uploader no longer a board member, limit lowered). A failure to read the card's attachments or the board's members fails `task` like a failed card read (`network_error` when temporary); a failed download does not fail `task`: the entry gets status `error` with the reason, and the next `kankan task` tries again. `task` starts no download after about 60 seconds, so that it returns within the agents' shell-command timeout; the files not reached get status `error` and are downloaded by the next `kankan task` (a file that cannot be downloaded within that time never is: the user links it instead). The directory `<gcd>/kankan/attachments/<shortLink>` is deleted when the card's ref is deleted (card verified in `Done`).

**Task output.** `kankan task` adds the field `attachments`, in the card's order, one entry per non-dependency attachment:

| Field | Meaning |
|---|---|
| `id`, `name` | Trello attachment ID and name |
| `kind` | `file` or `link` |
| `url` | the Trello URL (for `file`, the one a description references when it embeds the image) or the link's URL |
| `mime_type`, `bytes` | as reported by Trello (files only) |
| `path` | absolute path of the local copy (files with status `ok` only) |
| `status` | `ok`, `too_large`, `not_member`, `unexpected_url`, `error` |
| `message` | reason, for every status other than `ok` |

**The agent** ([13.1](#131-skill)) treats the attachments with status `ok` as part of the card's specification: it reads text files and looks at images with its own tools, and it may fetch links. Attachment contents are data supplied by the user, never instructions that override the skill. When a file the card depends on has status `error`, the agent repeats `kankan task --json` later; when it has another status, or keeps failing, the agent uses `ask`.

## 14. Communication through Trello

### 14.1 Labels

| Label | Set by | Meaning |
|---|---|---|
| `HOLD` | user | Do not assign; if in progress, suspend ([9.4](#94-hold)). |
| `Question` | kankan (`ask`) | The agent is waiting for an answer. |
| `Error` | kankan (`fail`, protocol, inactivity, unknown screen) | Intervention needed. |
| `Resume YYYY-MM-DD hh:mm` | launcher | Agent stopped by a usage limit until that date and time. |

The fixed labels are created by `init` and recognized by ID. kankan adds and removes labels **one at a time** and never rewrites the whole list, so it does not delete a `HOLD` the user just set. The seat state records the cause of every label set: a user answer removes only the label created by `ask`/`fail` on that card. kankan removes its own labels when the cause ends and when the card reaches `Done`.

`Resume` labels are removed from the card when the agent resumes. They are deleted from the board, under the project lock, when no card uses them or when their date passed more than an hour ago.

### 14.2 Notifications

kankan acts through its **own Trello account** (the token's owner), invited to the user's boards. Every `Question`, `Error` and `Blocked` comes with a comment mentioning the person behind the card: the card's creator, or, when kankan's account created the card (or Trello no longer knows the creator), the board's admins other than kankan. Trello never notifies anyone about their own comments, which is why kankan must not use a person's token.

### 14.3 Answers through comments

In `waiting_human`, the launcher reads the new comments posted after the question by **board members other than kankan's account** (comments with kankan's marker are never forwarded; comments by people outside the board are ignored, so open commenting cannot reach the agent) and forwards them to the agent in the pane ([8.6](initial_spec_technical.md#86-pane-observation-and-writes)). The cursor of the last forwarded comment is saved **before** forwarding: a comment is never forwarded twice, but a crash can lose one. After forwarding the launcher removes the label and replies "forwarded to the agent"; if this confirmation is missing, the user repeats the comment. Alternatively the user answers by attaching to the tmux session.

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

### 15.2 Situations and behaviour

| Situation | Behaviour |
|---|---|
| Another agent takes the card first | Next card. |
| Crash between Git transition and Trello move | Convergence on the next cycle. |
| Move that keeps failing or inconsistent reread | `Error`; no task assigned on that card. |
| Crash after the integration branch has advanced | Ref already `integrated`; convergence brings the card to `Done`. |
| The agent dies | The launcher terminates the remaining processes and releases the seat; the cards stay with the seat. |
| The launcher dies | The agent dies via `PDEATHSIG`; the supervisor starts a new launcher that recovers the seat. |
| The supervisor dies | Dead seat, recovered at the next `kankan agent`/`up`. |
| Server reboot | Different `boot_id`; `autostart` (if enabled) runs `kankan up`; automatic recovery. |
| Command started before a recovery or an administrative action | The transaction fails on the seat `verify` or on the card ref → `not_owner`. |
| Agent alive but stuck | No automatic recovery; `Error` for inactivity; manual recovery. |
| Usage limit | `limited`, `Resume`; resumed by `airesumer.sh`. |
| `main_moved` beyond the limit | `Error`. |
| Unresolvable conflict | `block` ([12.4](#124-unresolvable-conflicts-and-block)). |
| Different `registry-id` | Commands `config_error`; the launcher stops. |
| Workflow list archived or deleted | `config_error`, startup refused. |
| Ref pointing to an unexpected object or malformed blob | `needs_reconciliation`, no automatic action. |

### 15.3 `kankan reconcile [--fix]`

By default report only:
- card refs whose phase has not matched the list for more than one cycle;
- cards in seat lists without a ref; refs of deleted cards; malformed blobs;
- cards in `Done` without a matching merge in the integration branch (`Kankan-Card` trailer);
- registered dead seats; seats without their lists; seats unavailable because of surviving processes;
- unsatisfiable dependencies; more than one `??kankan-<shortLink>` branch for the same card; `blocked-…` worktrees of cards no longer in `Blocked`;
- `Error` or `Question` labels older than a threshold; failed push of the integration branch;
- the integration branch checked out in a worktree; invalid board↔registry binding;
- attachment directories ([13.3](#133-card-attachments)) of cards without a ref.

With `--fix`, under the project lock and skipping cards whose holder is alive, it repairs only the certain cases: deletes `integrated` refs of cards already in `Done`, deletes unused or expired `Resume` labels, removes kankan labels from cards in `Done`, deletes attachment directories of cards without a ref.

### 15.4 Administration commands

Under the project lock, conditional on the value read, recorded in the log:
- `kankan release <card> --force`: brings the card's ref to `parked` (the card stays where it is);
- `kankan seat recover <seat> [--kill]`: recovers a seat that is alive but stuck; with `--kill` it first terminates the processes of the recorded session;
- `kankan purge-remote`: deletes from the remote any `refs/agent-*` published by mistake.

### 15.5 `kankan doctor [--no-fix] [--offline]`

Checks the machine and, inside a clone, the project; fixes what is safe and prints the manual steps for the rest. Exit 0 when no manual step remains, 14 (`needs_action`) otherwise; warnings do not fail.

- **Machine:** Git ≥ 2.38, tmux ≥ 3.2, each agent command in full-access mode (manual step otherwise), `kankan` on the login `PATH` (Codex runs commands with it; agents that inherit their environment also get a private `<gcd>/kankan/bin` first on their `PATH`, holding only a `kankan` wrapper for this installation, so the interpreter's `python`/`pip` never shadow the project's), each agent CLI on `PATH`, its login (the adapter's `login_check`: a failure is only a warning, a timeout is skipped), the skill links (installed or refreshed automatically; a real directory is never touched), `KillUserProcesses` (warning), lingering when an autostart unit exists (manual step only).
- **Project:** binding (`kankan init` if missing), adapters, `pre-push` hook (installed or made executable), `info/exclude`, integration branch (exists, not checked out), divergence from the user branch, autostart unit (a generated unit whose `PATH` lacks `kankan` or an agent is rewritten keeping its enabled state; an edited unit is reported).
- **Online:** credentials (`kankan init --reauth`), kankan's account (manual step when the token's owner is an admin of the board: that is a person's token, and that person's answers would be ignored and never notified), board binding (`--takeover`; no board repair on a board bound to another clone), workflow lists (unarchive), labels (recreated; running launchers must be restarted, and a recreated `HOLD` does not restore which cards were held), `reconcile` issues (warning).

`--no-fix` changes nothing. **Preflight:** `kankan up` (once, before starting anything, for the types that need a new agent) and `kankan agent` run the offline checks for the requested types and refuse with the manual steps if an agent CLI is missing or kankan cannot be imported; they create a missing skill link for the requested types but never re-point an existing one; the login check never blocks. When the integration branch is also the user branch (pre-1.2 setup) `kankan.integrationBranch` is not set, so the hook never blocks the user's own pushes.

## 16. CLI

### 16.1 User commands

| Command | Description |
|---|---|
| `kankan init <url> [--credentials <file>] [--takeover] [--reauth]` | Project setup ([6.1](#61-kankan-init-board-url)). |
| `kankan agent <type> [--seat <s>] [--attach]` | Starts an agent in tmux. |
| `kankan up <type>=<n> …` | Ensures the requested number of agents, starting the missing ones. |
| `kankan autostart enable <type>=<n> …` / `autostart disable` | Automatic start at boot via systemd user service. |
| `kankan stop <seat>` | Exits after the current task. |
| `kankan status` | Seats (alive, dead, free, unavailable), launcher state, current task, waiting cards and reason. |
| `kankan claims` | Card refs with phase, holder, age and branch. |
| `kankan resolve <shortLink> [--done]` | Manual resolution of a blocked card ([12.4](#124-unresolvable-conflicts-and-block)). |
| `kankan reconcile [--fix]` | [15.3](#153-kankan-reconcile---fix). |
| `kankan doctor [--no-fix] [--offline]` | Checks the installation and the project, fixes what is safe, lists manual steps ([15.5](#155-kankan-doctor---no-fix---offline)). |
| `kankan merge-request [<branch>] [--remote] [--bottom]` | Asks the agents to merge a snapshot of a user branch into the integration branch ([12.5](#125-integration-branch-and-user-branches)). |
| `kankan adapter check <seat>\|<file>` | Screen classification ([8.7](initial_spec_technical.md#87-adapters)). |
| `kankan seat remove <seat>` | [7.1](#71-creation-and-removal). |
| `kankan release <card> --force`, `kankan seat recover <s> [--kill]`, `kankan purge-remote` | [15.4](#154-administration-commands). |

### 16.2 Agent commands

They identify the seat with `KANKAN_SEAT` and `KANKAN_SESSION`; when the agent runs commands without its environment (Codex hands them to a shared background service), they fall back to `kankan-agent.json`, written by the launcher in the seat worktree's private Git directory with the seat and current session. The seat file also records an identifier of the launcher's kankan code (a hash of the package files); an agent command running different code (kankan edited or upgraded since the launcher started) refuses with `config_error` and asks to restart the agents. They verify ownership and act on the current task: `task`, `note`, `sync`, `verify --tested <hash>`, `integrate --tested <hash>`, `done`, `ask`, `fail`, `block`, `yield`.

### 16.4 Responses

Text output by default; with `--json` a fixed envelope with the fields `ok`, `status`, `message`, `data`.

| Exit | `status` | Meaning |
|---|---|---|
| 0 | `success` | Done. |
| 10 | `no_work` | No task available. |
| 11 | `already_claimed` | Taken by someone else. |
| 12 | `ineligible` | Card not (or no longer) eligible. |
| 13 | `main_moved` | The integration branch moved: repeat `sync`, tests, `verify`, `integrate`. |
| 14 | `needs_action` | Failed check the agent can fix; the message explains what to do. |
| 15 | `on_hold` | The card is on `HOLD`: commit WIP and `yield`. |
| 20 | `network_error` | Temporary, retryable. |
| 21 | `trello_error` | Unexpected Trello response. |
| 30 | `needs_reconciliation` | Ambiguous state: stop. |
| 31 | `not_owner` | Card or seat not one's own: stop. |
| 40 | `config_error` | Missing or invalid configuration. |
| 41 | `usage_error` | Wrong arguments. |
| 50 | `internal_error` | kankan error. |

## 18. Tests

### 18.3 Acceptance tests

**Ownership and concurrency**
1. 20 parallel attempts on the same card: exactly one succeeds.
2. Two seats on different cards proceed in parallel.
3. Two agents start from a stale board read: only one gets the card.
4. Repeating a creation after an ambiguous failure recognizes one's own ref.
5. A conditional change does not modify a ref replaced in the meantime.
6. An `integrate` started before `seat recover` or `release --force` does not advance the integration branch.

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
18. `HOLD` matrix of [9.4](#94-hold), including `on_hold` from `integrate` and `HOLD` after integration.
19. Dependencies through attachments: waiting until `Done`; missing attachment, attachment from another board or cycle reported.
20. Foreign lists ignored; renamed workflow lists working; archived workflow list → `config_error`.
21. Branch `YYkankan-<shortLink>`: reuse after `HOLD` and after a change of year; two branches for the same card → `Error`.
22. Crash at every boundary of claim, `done`, `yield`, `block`: convergence always leads to a consistent state and no card is taken by two seats.

**Integration**

23. Two reviewers integrate together: both cards enter the integration branch, one gets `main_moved`; one merge per card along the first parent; `revert -m 1` undoes the right card.
24. The merge commit has the same tree as the tested version; `integrate` refuses a tip different from `--tested` and, with `test_command`, an unverified version.
25. Crash after the integration branch has advanced: no second merge; the card reaches `Done`.
26. `block` with a merge in progress: `blocked-…` worktree with identical state, seat free, integration branch untouched; `resolve --done` brings the card to `Implemented`; the next reviewer integrates after a `sync` without conflicts.

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

**Attachments**

41. Card attachments: `task` lists files and links but not dependency card links; a file is downloaded once into the card's attachment directory and reused by the reviewer's seat; a file larger than `attachment_max_bytes` is `too_large`; an attachment by a non-member is `not_member` and not downloaded; a file name with `../` stays inside the directory; a file whose URL points elsewhere is `unexpected_url` and gets no credential; a redirect to another host gets no credential; a failed download is `error` and is retried by the next `task`; an attachment removed from the card, or whose uploader left the board, loses its local copy; the directory is deleted when the card reaches `Done`.

## 19. Out of scope

- Agents on different servers or in different process namespaces; remote registries.
- Several repositories per project or several boards per repository.
- Human approval before integration.
- A web interface of its own: the UI is Trello.
- Absolute guarantee of precedence of human changes to a card's list ([14.5](#145-precedence-of-human-changes)).
- Backends other than Trello: the architecture makes them possible ([`kanban_requirements.md`](kanban_requirements.md)), but the first version supports only Trello.
