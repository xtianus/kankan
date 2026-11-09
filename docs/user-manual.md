# kankan user manual

kankan lets several AI coding agents (Claude Code and Codex CLI) work in parallel on one Git repository, taking tasks from a Trello board. One agent implements a card; an agent of the **other** type reviews it, integrates it into the agents' branch `kankan-agents` and closes it. Your `main` and your other branches stay yours ([section 4.1](#41-working-alongside-the-agents)). You create and order the cards, start the agents and step in only when an agent asks or when you want to stop a card.

The design is in [`aispecs/kankan_spec.md`](../aispecs/kankan_spec.md); links marked "spec §" point to its sections, links marked "section" to this manual's.

## 1. Requirements

- Linux, Python ≥ 3.10, Git ≥ 2.38, tmux ≥ 3.2; Claude Code and/or Codex CLI installed and logged in.
- A Trello account, an API key and a token (see [section 3](#3-binding-a-project-to-a-board)).
- tmux sessions survive SSH disconnection with `KillUserProcesses=no` in logind (the Ubuntu default). For agents started at boot, lingering must be enabled: `loginctl enable-linger`.

## 2. Installation

```bash
pipx install /path/to/kankan          # puts kankan in ~/.local/bin; then `pipx ensurepath` if needed
kankan skill install                   # links the shared skill into ~/.claude/skills and ~/.codex/skills
kankan doctor                          # checks git, tmux, the agents and their login, the skill, PATH; fixes what it can
```

`pipx install` builds a **copy** of kankan: later changes to `/path/to/kankan` have no effect until you reinstall it (`pipx install --force /path/to/kankan`). Restart the agents after reinstalling: an agent's kankan commands refuse to run when kankan changed since its launcher started. Editable installs (`pip install -e`) are only for developing kankan itself.

kankan uses only the Python standard library. `kankan` must be on your **login** `PATH` (for example `~/.local/bin`): Codex runs its shell commands through a background service that sees only that `PATH`, not the environment kankan gives the agent. `kankan doctor` is the first thing to run when something does not start: it fixes what is safe (skill links, the `pre-push` hook, missing labels, the PATH of a generated autostart unit) and prints numbered manual steps for the rest (exit 14 while any remain). `kankan up` and `kankan agent` refuse to start an agent type whose CLI is missing; they create a missing skill link but never change an existing one; a failed login check is only a warning. Agents always run the same kankan as their launcher, through a small wrapper in `<git dir>/kankan/bin`.

## 3. Binding a project to a board

kankan works on Trello with its own Trello account ([section 3.1](#31-a-trello-account-for-kankan)); then `kankan init` binds the project clone to the board ([section 3.2](#32-kankan-init)).

### 3.1 A Trello account for kankan

Trello never notifies you about your own actions. If kankan uses **your** token, its questions, errors and mentions of you are, for Trello, comments you wrote yourself: you get no notification. Give kankan its own Trello account:

1. Create a second Trello account for kankan. It needs its own email address; a plus-address such as `you+kankan@example.com` works with most providers. Trello gives it a workspace of its own.
   - Give it an **uncommon username**, for example `kankan-<your username>-<a few random letters>` (`kankan-xtian-q7wz`), not a generic `kankanbot`. Board invitations go by username or email: a distinctive name keeps other people from inviting your bot to their boards by mistake, and keeps you from inviting someone else's bot to yours.
2. From your account, invite the bot to the project's board as a normal member, checking the username before you confirm. The boards stay yours.
3. Log in **as the bot** (a private browser window is simplest) and create the Trello app that gives kankan its API key:
   1. Open `https://trello.com/apps/admin` and press **New** under *Your Apps*.
   2. Fill the form: *App specifications*: **My app doesn't use Power-up capabilities** (kankan adds nothing to the Trello UI; this cannot be changed later); *App name* (for example "kankan"); *Workspace*: the bot's own workspace; *Email*, *Support contact*, *Author*. Press **Create**. The page opens on the *OAuth 2.0* tab, which kankan does not use.
   3. Open the **Trello Auth** tab, press **Generate a new Trello Auth API key for this app** and confirm with **Generate API key**. Copy the **API key** (kankan does not need the *Secret*).
4. Still logged in as the bot, get the token: on the same **Trello Auth** tab click the **Token** link next to the API key, check that the page names your app, press **Allow** and copy the token. Key, app and token all belong to the bot; your own account only owns the boards.
   - This token also has the `account` scope (it could change the bot's own profile), which kankan does not need; for a dedicated bot account that is harmless. For the minimal scope, open instead the URL that `kankan init` prints when it asks for the token (`scope=read,write`).
5. Keep the key and the token at hand: `kankan init` asks for them ([section 3.2](#32-kankan-init)), or put them in a `.properties` file and pass it with `--credentials`. Run `init` with `--notify <your Trello username>`: by default kankan mentions the token's owner, which is the bot.

If a clone already uses your own token, switch it to the bot with `kankan init --reauth`, then `kankan init --notify <your Trello username>`.

Trello does not let you reset an API key, so keep the key and the token private; the token stays valid until it is revoked from the bot's account settings.

Your answers keep working: kankan forwards to the agent only comments written by the notify user (you) without kankan's marker, and ignores the bot's own comments. `kankan init` and `kankan doctor` warn while the notify user is the token owner.

### 3.2 `kankan init`

Run once in the project clone (for example `~/coding/myproject`):

```bash
kankan init https://trello.com/b/<shortLink>/<name> --notify <your Trello username> [--credentials <file.properties>]
```

`init` ([spec §6.1](../aispecs/kankan_spec.md#61-kankan-init-board-url)):

- checks the Trello credentials, and if they are missing asks for the bot's API key and token ([section 3.1](#31-a-trello-account-for-kankan)); for the token it also prints an authorization URL with the minimal scope. Credentials are looked up in this order: `KANKAN_TRELLO_KEY`/`KANKAN_TRELLO_TOKEN`; the `.properties` file given with `--credentials` (keys `kankan.trello.key`, `kankan.trello.token`); `~/.config/kankan/credentials` (mode 600). kankan edits a properties file in place: comments, order and other keys are kept;
- writes a `kankan-registry` block into the board description (the rest of the description is yours);
- finds or creates the lists `Backlog`, `Done`, `Blocked` (asking before adopting existing ones) and the labels `HOLD`, `Question`, `Error`;
- asks for the project's **test command** (run by `kankan verify`; may be empty);
- asks for the **branch where the agents integrate** (default `kankan-agents`) and creates it from `main` (or adopts `origin/kankan-agents` if it exists);
- installs a `pre-push` hook that refuses to push `refs/agent-*` and to push the agents' branch (only kankan does that), and warns if the agents' branch is checked out.

`init` is idempotent. `kankan init --reauth` only replaces the credentials. A board already bound to another clone is refused; `--takeover` binds it to this clone — use it **only after stopping the other clone's agents**: they stop at their next cycle.

You may rename and reorder the workflow lists and create your own lists and labels: kankan recognizes its lists and labels by ID. Archiving a workflow list stops kankan with `config_error`.

## 4. Starting agents

```bash
kankan up claude=2 codex=1        # make sure these agents run; starts only the missing ones
kankan agent claude [--seat claude-1] [--attach]
kankan autostart enable claude=2 codex=1   # systemd user service kankan-up@<project>.service
kankan autostart disable
kankan stop claude-1              # the agent exits after its current task
```

Each agent gets a **seat** (`claude-1`, `codex-2`…) with three lists on the board (`<seat> · In Progress`, `· Implemented`, `· Review`) and a worktree in `~/coding/.kankan/<project>/<seat>`. Seats are reused; at most 4 per type by default (`seat_cap` in `<git dir>/kankan/config.json`).

The agent runs in the tmux session `ai-<project>-<seat>`; attach with `tmux attach -t ai-<project>-<seat>` to watch. Ctrl-C typed there reaches the agent (the launcher and supervisor ignore Ctrl-C, Ctrl-Z and Ctrl-\\). kankan never types while you are typing (it waits `write_quiet_seconds`, default 20 s) and never sends Esc or Ctrl-C. `airesumer.sh` finds these sessions and resumes agents after a usage limit.

### 4.1 Working alongside the agents

The agents integrate into `kankan-agents`; only kankan moves it, so never check it out (`init`, `up` and `doctor` warn if you do). `main` and all your other branches are yours: check them out, commit and pull as usual.

- **Take the agents' work:** on your branch, `git merge kankan-agents`. From another clone (with `push_main` on): `git pull origin kankan-agents`. Never push to `kankan-agents` yourself: the `pre-push` hook blocks it in the kankan clone only.
- **Give the agents your work:** `kankan merge-request my-feature` (default: your `main`) creates a card at the top of `Backlog`, "Merge branch my-feature into kankan-agents", naming the exact commit to merge. An agent merges it and resolves conflicts, an agent of the other type reviews and integrates it. If you work in another clone, push `my-feature` first and use `--remote`. `--bottom` queues it after the other cards.
- **See how far apart they are:** `kankan status` prints `kankan-agents is N commits ahead / M behind main`; `kankan doctor` warns when an integration has waited more than 7 days for you to merge it.

For the old behaviour (agents integrate straight into `main`) answer `main` at `kankan init`; then keep `main` **not** checked out while they work, because kankan advances it directly ([spec §12.5](../aispecs/kankan_spec.md#125-integration-branch-and-user-branches)).

## 5. The golden rule

> **Never move cards that are in the seats' lists; to stop a card use `HOLD` and wait for kankan to bring it back.**

You may freely:

- create cards in `Backlog` and reorder them (the top card is taken first);
- add and remove `HOLD`, add dependency attachments, write comments;
- unblock a card in `Blocked` with `kankan resolve <shortLink> --done`, or move it to `Backlog` to have it reimplemented.

If you move, archive or delete a card that is in progress anyway, kankan asks the agent to commit its work in progress and give the card back, and leaves the card where you put it ([spec §14.6](../aispecs/kankan_spec.md#146-cards-moved-by-the-user-safety-net)). This is best effort ([spec §14.5](../aispecs/kankan_spec.md#145-precedence-of-human-changes)).

## 6. The workflow

1. An idle agent takes, in this order: its own unfinished cards; cards in an `Implemented` list implemented by an agent of the other type (review); the top eligible card of `Backlog` (implementation).
2. Implementation happens on the branch `YYkankan-<shortLink>`; the card moves to the seat's `In Progress`, then to its `Implemented` list.
3. A reviewer of the other type moves it to its `Review` list, fixes problems with separate commits, merges `kankan-agents` into the branch, runs the tests (`kankan verify`) and integrates: `kankan-agents` gets one merge commit per card, whose message holds the card title, URL and the trailers `Kankan-Card` and `Kankan-Tested`. To undo a card: `git revert -m 1 <merge>`.
4. The card goes to `Done`.

With only one agent type running, implemented cards wait in `Implemented`; `kankan status` says why.

**Dependencies:** attach the cards a card depends on (Trello "Attachment → Trello card"). The card waits until every attached card is in `Done`. Missing cards, cards on other boards, archived cards outside `Done` and cycles make it ineligible (shown by `kankan status` and `kankan reconcile`).

## 7. Talking to the agents

| Label | Meaning | What you do |
|---|---|---|
| `HOLD` (you) | do not assign; if in progress, suspend | remove it to make the card eligible again |
| `Question` | the agent asked something (comment with a mention) | answer with a **comment** on the card, or in the tmux session |
| `Error` | the agent or kankan needs help | read the comment; fix, then remove the label |
| `Resume YYYY-MM-DD hh:mm` | usage limit until then | nothing |

Your comment is forwarded to the waiting agent once, and kankan replies "forwarded to the agent". If that reply does not appear, repeat the comment. Only comments by the configured user (`--notify`) are forwarded.

`HOLD` on a card that was already integrated cannot stop it: the card goes to `Done` with a comment telling how to revert it.

## 8. Blocked cards

When a reviewer cannot resolve a merge conflict safely, it blocks the card: kankan moves the unfinished merge, with exactly the agent's partial resolution, to `~/coding/.kankan/<project>/blocked-<shortLink>` and the card to `Blocked`.

```bash
kankan resolve <shortLink>          # lists the conflicts and opens tmux session kankan-resolve-<project>-<shortLink>
# edit the files, git add, git commit (in the blocked worktree)
kankan resolve <shortLink> --done   # checks the merge, removes the worktree, sends the card back to review
```

## 9. Diagnostics

```bash
kankan status             # seats (alive, free, dead, unavailable), launcher state, current task, waiting cards
kankan claims             # card refs: phase, holder, age, branch
kankan doctor             # installation and project checks, fixes and manual steps (§15.5); --no-fix, --offline
kankan reconcile          # report inconsistencies (§15.3)
kankan reconcile --fix    # repair only the certain cases
kankan adapter check claude-1         # how kankan reads that agent's screen
kankan adapter check saved-screen.txt --type codex
```

Every agent command answers with `--json` an envelope `{"ok", "status", "message", "data"}` and an exit code (0 success, 10 no_work, 11 already_claimed, 12 ineligible, 13 main_moved, 14 needs_action, 15 on_hold, 20 network_error, 21 trello_error, 30 needs_reconciliation, 31 not_owner, 40 config_error, 41 usage_error, 50 internal_error). The audit log is `<git dir>/kankan/log.jsonl`.

## 10. Administration

```bash
kankan release <card> --force        # park a card's ref; the card stays where it is
kankan seat recover <seat> [--kill]  # a seat that is alive but stuck; --kill terminates its pane session first
kankan seat remove <seat>            # only if free, holding no cards and with empty lists
kankan purge-remote [--remote origin]  # delete refs/agent-* pushed by mistake
```

All of these are recorded in the audit log. A command started before them (for example an `integrate`) can no longer change anything (`not_owner`).

## 11. Adapters

How kankan reads each agent's screen (ready, working, usage limit, trust window, known dialogs) is defined by adapters. Override only what changes in `~/.config/kankan/agents.json` (user) or `<git dir>/kankan/agents.json` (project), for example:

```json
{"codex": {"queue_while_working": true}}
```

`login_check` is the command `kankan doctor` runs to see whether an agent is logged in (exit 0 = logged in): `claude auth status` and `codex login status` by default; set it to `[]` to skip the check.

See [`docs/adapter-notes.md`](adapter-notes.md) for what was observed on the installed agent versions.

## 12. Limits

- Daemons that detach from the agent's terminal session (for example the Gradle daemon) are not terminated when a dead seat is recovered; processes still running in the seat worktree are reported in the audit log (`seat_escapees`) ([spec §7.5](../aispecs/kankan_spec.md#75-liveness-and-automatic-recovery)).
- Human changes to a card's list are honoured on a best-effort basis ([spec §14.5](../aispecs/kankan_spec.md#145-precedence-of-human-changes)); `reconcile` reports leftovers.
- If the agents integrate into a branch you have checked out (the old `main` setup), your files fall behind when it advances ([spec §12.5](../aispecs/kankan_spec.md#125-integration-branch-and-user-branches)).
- Exclusion between two clones bound to the same board relies on the registry check; it is not absolute ([spec §6.3](../aispecs/kankan_spec.md#63-boardregistry-binding)).
