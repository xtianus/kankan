# kankan user manual

kankan lets several AI coding agents (Claude Code and Codex CLI) work in parallel on one Git repository, taking tasks from a Trello board. One agent implements a card; an agent of the **other** type reviews it, integrates it into the agents' branch `kankan-agents` and closes it. Your `main` and your other branches stay yours ([section 4.1](#41-working-alongside-the-agents)). You create and order the cards, start the agents and step in only when an agent asks or when you want to stop a card. You can also ask an agent questions about the code on `Q&A` cards and talk with it through their comments ([section 6.2](#62-questions-qa-cards)).
One installation of kankan can be used on many different projects, provided that each project has its own git clone and Trello board.

## 1. Requirements

- Linux, Python ≥ 3.10, Git ≥ 2.38, tmux ≥ 3.2 and pipx; Claude Code and/or Codex CLI installed and logged in. Install both agent CLIs for the workflow where an agent of the other type reviews each implementation.
- A Trello account, an API key and a token (see [section 3](#3-binding-a-project-to-a-board)).
- On systems using systemd, `KillUserProcesses=no` in logind lets tmux sessions survive logout. Enable lingering if agents must start at boot before you log in (see [section 1.3](#13-session-and-boot-configuration)).

### 1.1 Install the required software

On **Ubuntu 24.04 LTS**, install the system packages below. `python3-venv` supports pipx's isolated environments; `curl` and CA certificates are used to download the agent CLIs. Other Linux distributions need equivalent packages meeting the minimum versions above; older Ubuntu releases may ship an older Git.

```bash
sudo apt update
sudo apt install python3 python3-venv git tmux pipx curl ca-certificates
pipx ensurepath
```

Log out and back in so the login environment picks up the PATH change ([pipx installation guide](https://pipx.pypa.io/latest/how-to/install-pipx.html)). Install the agent CLIs as your normal user, using their official installers ([Claude Code setup](https://code.claude.com/docs/en/setup), [OpenAI Codex CLI documentation](https://learn.chatgpt.com/docs/codex/cli)):

```bash
curl -fsSL https://claude.ai/install.sh | bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Open a new terminal, then check the installed system tools:

```bash
python3 --version
git --version
tmux -V
pipx --version
```

Compare the output with the minimum versions in section 1. If a tool is too old, upgrade it using your distribution's packages or upgrade the distribution before continuing. Check the agent CLIs and sign in, following their browser prompts:

```bash
claude --version
codex --version
claude auth login
codex login
```

To check authentication later, run `claude auth status` and `codex login status`. Run the corresponding login command again to sign in or change accounts.

### 1.2 Check and configure the login PATH

For the default per-user installers, `~/.local/bin` must be on your login PATH. After installing kankan in section 2, check it from a login shell:

```bash
bash -lc 'command -v kankan; command -v claude; command -v codex'
```

Each installed command should print a path. If one is missing, run `pipx ensurepath` again. For a Bash login shell, you can also add the following line to `~/.profile` (or `~/.bash_profile` if that is the startup file you use):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Log out and back in, then repeat the check. Restart any running Codex background service after changing the login PATH so it inherits the updated environment. A PATH change only in the current terminal or in `~/.bashrc` may not reach that service.

### 1.3 Session and boot configuration

On systemd systems, check the **running** logind setting and your user's lingering status:

```bash
busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager KillUserProcesses
loginctl show-user "$USER" -p Linger
```

The first command should print `b false`, meaning `KillUserProcesses=no`. If it prints `b true`, create a logind configuration override:

```bash
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nKillUserProcesses=no\n' | sudo tee /etc/systemd/logind.conf.d/99-kankan.conf
```

Reboot at a convenient time to apply the setting, then repeat the `busctl` check. To inspect the main configuration and overrides together, run `systemd-analyze cat-config systemd/logind.conf` ([logind configuration reference](https://www.freedesktop.org/software/systemd/man/latest/logind.conf.html)).

`Linger=yes` is needed only for starting agents at boot before login. Enable it for your user and check the result:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

To turn lingering off, run `sudo loginctl disable-linger "$USER"` ([loginctl reference](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)). Lingering keeps the user service manager available; after binding your project, use `kankan autostart enable` to configure the agents to start ([section 4](#4-starting-agents)).

## 2. Installation

If you already have kankan installed, see [section 10.1, Updating kankan](#101-updating-kankan).

First clone the kankan repository. You can put the clone in any writable folder; no specific location or folder name is required. For example, to use `~/tools/kankan`:

```bash
mkdir -p ~/tools
git clone https://github.com/xtianus/kankan.git ~/tools/kankan
```

Install from that clone, then install the shared agent skill and check the setup:

```bash
pipx install ~/tools/kankan            # puts kankan in ~/.local/bin; then `pipx ensurepath` if needed
kankan skill install                   # links the shared skill into ~/.claude/skills and ~/.codex/skills
kankan doctor                          # checks git, tmux, the agents and their login, the skill, PATH; fixes what it can
```

Replace `~/tools/kankan` with your chosen clone path if different. This is the source of the kankan tool, separate from the project repository you want the agents to work on. That project can also live anywhere; run `kankan init` inside its clone (for example `~/coding/myproject`, as in [section 3.2](#32-kankan-init)).

`kankan doctor` is the first thing to run when something does not start: it fixes what is safe (skill links, the `pre-push` hook, missing labels, the PATH of a generated autostart unit) and prints numbered manual steps for the rest (exit 14 while any remain). `kankan up` and `kankan agent` refuse to start an agent type whose CLI is missing; they create a missing skill link but never change an existing one; a failed login check is only a warning.

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
5. Keep the key and the token at hand: `kankan init` asks for them ([section 3.2](#32-kankan-init)), or put them in a `.properties` file and pass it with `--credentials`. 

Trello does not let you reset an API key, so keep the key and the token private; the token stays valid until it is revoked from the bot's account settings.

To change the token use `kankan init --reauth`.

Who gets notified: kankan mentions the person who created the card; for cards kankan created itself, the board's admins. Who can answer: any member of the board except kankan's own account; comments and attachments by people outside the board are ignored.

### 3.2 `kankan init`

Run once in the project clone (for example `~/coding/myproject`):

```bash
kankan init <trelloBoard> [--credentials <file.properties>]
```

- connects to the Trello board specified by `<trelloBoard>`. This is the board that you use to interact with the agents. The parameter is a URL in the format `https://trello.com/b/<shortLink>/<name>`. Just copy it from the browser address bar when you open the board. Example: `https://trello.com/b/Vz4CZ6ZS/myprojectboard`. 
- checks the Trello credentials, and if they are missing asks for the bot's API key and token ([section 3.1](#31-a-trello-account-for-kankan)); for the token it also prints an authorization URL with the minimal scope. Credentials are looked up in this order: `KANKAN_TRELLO_KEY`/`KANKAN_TRELLO_TOKEN`; the `.properties` file given with `--credentials` (keys `kankan.trello.key`, `kankan.trello.token`); `~/.config/kankan/credentials` (mode 600). kankan edits a properties file in place: comments, order and other keys are kept;
- writes a `kankan-registry` block into the board description (the rest of the description is yours);
- finds or creates the lists `Backlog`, `Done`, `Blocked`, `Q&A` and `kankan` (asking before adopting existing ones; an adopted `Q&A` list keeps its cards, which become questions; a new `Q&A` list is placed right after `Blocked`; `kankan` holds the setting cards, see below) and the labels `HOLD`, `Question`, `Error`;
- asks for the project's **test command**, the optional shell command that kankan runs on every card before integrating it, as an extra quality check ([section 3.3](#33-the-test-command));
- asks for the **branch where the agents integrate** (default `kankan-agents`) and creates it from `main` (or adopts `origin/kankan-agents` if it exists);
- asks for the default model and effort per agent type and role if you have not chosen them yet (one keystroke each), then creates the setting cards in the `kankan` list: `Implementation Agents` and `Review Agents` (and `Q&A Agents` once a Q&A agent is configured, [section 6.2](#62-questions-qa-cards)), and **prints the model and effort it ticked on them** for each agent type, with the file they come from ([section 6.1](#61-model-and-effort)). Check them: they decide the speed and the cost of every task.

`init` is idempotent. `kankan init --reauth` only replaces the credentials. A board already bound to another git clone (of this project or any other repository) is refused; to move a board to another clone, see [section 12.1](#121-moving-a-board-to-another-clone).

You may rename and reorder the workflow lists and create your own lists and labels: kankan recognizes its lists and labels by ID. Archiving a workflow list stops kankan with `config_error`, except the `Q&A` list: without it only the Q&A agent stops (after its current turn), `kankan status` warns and `kankan doctor` restores it.

### 3.3 The test command

The test command is an optional quality step: it ensures that all tests are run before a change is integrated. If you omit it, agents will run the tests of their choice with no deterministic assurance that any test actually run. If you use it, the test will always be run.
When not sure, don't provide a test command at initialization time: it can easily be provided later (see below).

- **Without a test command**, the agents choose. kankan tells the implementer and the reviewer to run the project's tests, but each agent picks which tests to run and how. kankan does not check that they ran or that they passed.
- **With a test command**, the run is deterministic. kankan itself runs the same command on the exact commit it is about to integrate, and refuses to integrate unless it passes. The agents still run tests of their own as well.

The test command is the shell command that runs your project's automated tests, for example `pytest -q`, `npm test`, `./gradlew test` or `make test`. `kankan init` saves it as `test_command` in `<git dir>/kankan/config.json`. It stays in your clone and cannot be set from the board.

Leave it empty if the project has no automated tests, or if they cannot run unattended, or if running them is too expensive. Setting it has a cost: the whole command runs on every card, whatever the card changed. A long suite makes every integration wait for it, even after a one-line change, and the agents may already have run some of the same tests.

**How to write it.**

- It runs unattended: no prompts, no watch mode (for example `vitest run`, not `vitest`), no GUI.
- Its exit code must be non-zero when any test fails. Chain steps with `&&`, not `;`, so an earlier failure is not hidden.
- It runs from the root of a fresh worktree: use relative paths, and do not rely on untracked files in your main checkout (such as a local `.env`). Install dependencies in the command if needed (for example `npm ci && npm test`).
- Any file it writes (build output, coverage, caches) must be ignored by `.gitignore`. Otherwise the worktree is no longer clean and the result is not recorded.
- It must finish within `test_timeout`, and it must not rely on background services outliving it: start and stop them inside the command.
- For anything complex, put the steps in a script in the repository and use that as the command (for example `sh check.sh`).

**Changing it later.** Run `kankan init` again: it shows the current command in brackets. Press Enter to keep it, or type a new one. An empty answer keeps the old value, so to clear the command, set `test_command` to `""` in `<git dir>/kankan/config.json`.

## 4. Starting agents

```bash
kankan up claude=2 codex=1        # make sure these agents run; starts only the missing ones
kankan agent claude [--seat claude-1] [--attach]
kankan autostart enable claude=2 codex=1   # systemd user service kankan-up@<project>.service
kankan autostart disable
kankan stop claude-1              # the agent exits after its current task
kankan stop [--graceful]          # every agent of the project, and autostart disabled (asks first)
kankan start                      # start again what the last `kankan stop` stopped
```

Each agent gets a **seat** (`claude-1`, `codex-2`…) with three lists on the board (`<seat> · In Progress`, `· Implemented`, `· Review`) and a worktree in `~/coding/.kankan/<project>/<seat>`. Seats are reused; at most 4 per type by default (`seat_cap` in `<git dir>/kankan/config.json`).

`kankan stop` without a seat stops **all** the project's agents, the Q&A agent included, and disables autostart. It shows what it will stop and asks for confirmation (`--yes` skips the question and is required from a script). By default it quits the agents **immediately**, as `kankan seat recover --kill` would: each interrupted card stays with its seat, gets a comment and is resumed at the next start. With `--graceful` it instead waits for each agent to finish its current task (a Q&A agent its current answer), showing what each one is doing; that can take long, and Ctrl-C only stops the waiting (the agents still stop after their task). At the end kankan checks that no agent runs, no tmux session of the project is open and autostart is off, and lists anything left with what to do (exit 14). Running `kankan stop` again is harmless: what is already stopped or disabled is reported as such.

`kankan start` resumes everything that `kankan stop` stopped: the same agents (`kankan up` with the same counts) and autostart if it was enabled. Without a stop record it prints the commands to run instead: `kankan up …` and `kankan autostart enable …`, preceded by `kankan init` when the clone is not bound to a board yet.

The **Q&A agent** ([section 6.2](#62-questions-qa-cards)) is started by adding `qa=<type>` to `kankan up` or `kankan autostart enable` (for example `kankan up claude=2 codex=1 qa=claude`), or alone with `kankan agent <type> --qa`. It gets the seat `<type>-qa` (`claude-qa`, `codex-qa`), with its own worktree and no lists; there is at most one per project and it does not count towards `seat_cap`. It answers only the cards of the `Q&A` list, and the other agents never take them. Starting a Q&A agent of the other type replaces the existing Q&A seat: kankan refuses while that seat is alive (stop it first with `kankan stop <type>-qa`); otherwise it removes the old seat and the new agent continues the old one's conversation, if any. `kankan stop claude-qa` lets the agent finish its current turn; the conversation is kept and continues at the next start.

The agent runs in the tmux session `ai-<project>-<seat>`; attach with `tmux attach -t ai-<project>-<seat>` to watch. Ctrl-C typed there reaches the agent (the launcher and supervisor ignore Ctrl-C, Ctrl-Z and Ctrl-\\). kankan never types while you are typing (it waits `write_quiet_seconds`, default 20 s) and never sends Esc or Ctrl-C. `airesumer.sh` finds these sessions and resumes agents after a usage limit.

### 4.1 Working alongside the agents

The agents integrate into `kankan-agents`; only kankan moves it, so never check it out (`init`, `up` and `doctor` warn if you do). `main` and all your other branches are yours: check them out, commit and pull as usual.

- **Take the agents' work:** on your branch, `git merge kankan-agents`. From another clone (with `push_main` on): `git pull origin kankan-agents`. Never push to `kankan-agents` yourself: the `pre-push` hook blocks it in the kankan clone only.
- **Give the agents your work:** `kankan merge-request my-feature` (default: your `main`) creates a card at the top of `Backlog`, "Merge branch my-feature into kankan-agents", naming the exact commit to merge. An agent merges it and resolves conflicts, an agent of the other type reviews and integrates it. If you work in another clone, push `my-feature` first and use `--remote`. `--bottom` queues it after the other cards.
- **See how far apart they are:** `kankan status` prints `kankan-agents is N commits ahead / M behind main`; `kankan doctor` warns when an integration has waited more than 7 days for you to merge it.

For the old behaviour (agents integrate straight into `main`) answer `main` at `kankan init`; then keep `main` **not** checked out while they work, because kankan advances it directly.

## 5. The golden rule

> **Never move cards that are in the seats' lists; to stop a card use `HOLD` and wait for kankan to bring it back.**

You may freely:

- create cards in `Backlog` and reorder them (the top card is taken first);
- create cards in `Q&A`, reorder them, and move, archive or delete the card under discussion to end its conversation ([section 6.2](#62-questions-qa-cards));
- add and remove `HOLD`, add attachments (files, links, dependency cards), write comments;
- change the model and effort by ticking items on the setting cards of the `kankan` list ([section 6.1](#61-model-and-effort));
- unblock a card in `Blocked` with `kankan resolve <shortLink> --done`, or move it to `Backlog` to have it reimplemented.

If you move, archive or delete a card that is in progress anyway, kankan asks the agent to commit its work in progress and give the card back, and leaves the card where you put it. This is best effort.

## 6. The workflow

1. An idle agent takes, in this order: its own unfinished cards; cards in an `Implemented` list implemented by an agent of the other type (review); the top eligible card of `Backlog` (implementation).
2. Implementation happens on the branch `YYkankan-<shortLink>`; the card moves to the seat's `In Progress`, then to its `Implemented` list.
3. A reviewer of the other type moves it to its `Review` list, fixes problems with separate commits, merges `kankan-agents` into the branch, runs the tests (`kankan verify`) and integrates: `kankan-agents` gets one merge commit per card, whose message holds the card title, URL and the trailers `Kankan-Card` and `Kankan-Tested`. To undo a card: `git revert -m 1 <merge>`.
4. The card goes to `Done`.

With only one agent type running, implemented cards wait in `Implemented`; `kankan status` says why.

**Dependencies:** attach the cards a card depends on (Trello "Attachment → Trello card"). The card waits until every attached card is in `Done`. Missing cards, cards on other boards, archived cards outside `Done` and cycles make it ineligible (shown by `kankan status` and `kankan reconcile`).

**Attachments:** files and links attached to a card by board members reach the agent, for implementation and review alike; card links stay dependencies. Files are downloaded to `<git dir>/kankan/attachments/<card>/` and removed when the card reaches `Done`. A file that cannot be downloaded within about a minute is reported as an error each time, so attach very large files elsewhere and link them. Files larger than `attachment_max_bytes` (default 25 MiB, in `<git dir>/kankan/config.json`) are listed but not downloaded; attachments added by people outside the board or by kankan's own account are listed but neither downloaded nor passed on as links. `kankan reconcile --fix` removes attachment directories left behind, for example for cards that left their seat without reaching `Done`.

### 6.1 Model and effort

The model and the reasoning effort the agents use decide how fast and how expensive each task is. You choose them on the board, in the `kankan` list, which holds two or three cards:

- **`Implementation Agents`**: the model and effort for implementing a card (merge requests included);
- **`Review Agents`**: the model and effort for reviewing and integrating a card;
- **`Q&A Agents`**: the model and effort of the Q&A agent, once one is configured ([section 6.2](#62-questions-qa-cards)). The first `kankan up qa=<type>` or `kankan agent <type> --qa` registers the Q&A seat; without a `questions` seed for that type it then stops and asks you to run `kankan doctor`, which asks for the seed like the others and creates the `Q&A Agents` card; run the start command again. The other agents never need it. A change applies between two answers: kankan never restarts the Q&A agent while it is answering, and the restarted agent continues the conversation from the comments.

In `agents.json` (the seeds), `kankan status` and `kankan doctor` the three roles are called `implementation`, `review` and `questions`.

Each card has one checklist per agent type (`claude`, `codex`) listing every **combination** of model and effort that agent accepts, for example `sonnet · high` or `opus · max` (not every model accepts every effort, so only valid pairs are listed). **Tick exactly one item in each checklist.** There is no default: no tick, two ticks or an unknown item is an error. Trello does not untick the old item for you, so switch by ticking the new item and unticking the old one.

kankan waits until a card has not been touched for 10 seconds and responds within about 10–40 seconds while agents run (otherwise at the next `kankan up` or `kankan doctor`):

- **valid:** it comments "applied: …" and the new values are used from the **next task**; a task already running keeps its values. When a task needs other values than the agent was started with, kankan restarts the agent with them before giving it the task; nothing of the task is lost.
- **invalid:** the card gets `Error` and a comment saying what is wrong; the **previous values stay in use** until you fix the card, then kankan removes the `Error` it set. An `Error` that you added stays.

**Checking a combination.** The agents are updated independently of kankan, so a combination can stop working. Before a combination is first used, and after each update of an agent, kankan checks it with a tiny request to that agent (a few thousand tokens), run without your `CLAUDE.md`/`AGENTS.md` files, memory, tools or plugins. If the agent rejects it, the setting card gets `Error` with the agent's message, and that agent type takes no task of that role until you tick another combination or the CLI version changes; its other role and other agent types continue. A timed-out check stays unverified and is retried after 10 minutes by default. `kankan status` distinguishes rejected and unverified pairs. Checks run in the background, so HOLD and stop requests remain responsive.

kankan keeps only its latest 10 comments on each setting card; yours stay. Do not move the setting cards out of the `kankan` list: a card moved to `Backlog` becomes a task. If you delete a setting card, the last values stay in use and `kankan doctor` recreates it.

**Seeds.** When kankan creates the cards it ticks the **seed** values, one per agent type and role. The first time, `kankan init` (or `kankan doctor`) asks you for each of them, listing the combinations with the most powerful first:

```
Default model and effort for claude, implementation (affects speed and cost):
  1 fable · max     2 fable · xhigh   3 fable · high   …
  9 opus · max      a opus · xhigh    b opus · high    …
Press a key:
```

Press the key of your choice. kankan saves it in `~/.config/kankan/agents.json` ([section 11](#11-adapters)), where you can change it later:

```json
{"claude": {"seed": {"implementation": {"model": "sonnet", "effort": "high"}, "review": {"model": "opus", "effort": "high"}}}}
```

Run `init` and `doctor` in a terminal the first time: from a script they cannot ask, and they stop with the manual step for setting seeds. `agent` and `up` never ask: they need a seed only for a type and role with no recorded valid board value. Seeds never replace a valid board selection. Invalid seeds are refused even when board values exist. If Codex's model list stops offering a seeded combination, the seed counts as missing: `kankan doctor` says so and asks for a new one.

`doctor --offline` reads cached selections and checks; `doctor --no-fix` asks nothing, starts no model checks, and changes no files or board data. Online fixing `doctor` refreshes catalogs, repairs missing setting cards and synchronizes their items without choosing a replacement for an invalid existing selection.

`kankan init` and `kankan doctor` print the values they tick and where they come from, and `doctor` always prints the model and effort in use per role and agent type. `kankan status` shows the model and effort each running agent was started with, and the comment kankan writes when it takes a card names them.

### 6.2 Questions: `Q&A` cards

Ask the agents about the code without asking for a change: create a card in the `Q&A` list (or drag one there) with your question in the description. No label is needed. The Q&A agent ([section 4](#4-starting-agents)) takes the top card of `Q&A` that is not on `HOLD`, reads the code and answers with a comment that mentions you. The card stays in `Q&A`, without labels, and nothing is committed.

- **One conversation at a time.** The card the agent took stays under discussion until you end it; the other cards in `Q&A` wait, whatever their order. Reorder them freely; a card dragged above the one under discussion waits too. While no Q&A agent runs, `Q&A` cards wait and `kankan status` says so.
- **Follow-ups.** Continue the dialogue with a comment, an attachment (file or link), or both. What you add is forwarded to the agent within about 30 seconds after it finished its previous answer, all together, in the same agent conversation; kankan replies "forwarded to the agent". What you add while the agent is still answering is forwarded after that answer. You can also type in the agent's tmux session.
- **Long answers** are split into numbered comments (`1/3`, `2/3`, …); only the last one mentions you.
- **Errors.** When the agent cannot answer because something needs you (for example an attachment it cannot download), the card gets `Error` with a comment; so does an agent that stops without answering. Add a comment (or fix the attachment): the next follow-up removes the `Error` label and the conversation goes on.
- **Asking for a change.** Write "now implement it" (or similar): the agent does not change the code itself. It posts a summary of what to implement, mentioning you, and moves the card to the **bottom** of `Backlog`, where you can still reorder it, put it on `HOLD` or move it elsewhere. From there it is a normal card: an agent implements it following the summary (which prevails over the description, since the description was a question) and the comments, another reviews and integrates it. When your request is ambiguous, the agent asks you to confirm first. If the summary is wrong, drag the card back into `Q&A` and continue the dialogue.
- **Ending a conversation.** Move the card out of `Q&A` (to `Done` or any other list), archive it or delete it: kankan never moves it back, and the agent starts the next card in a fresh conversation. `HOLD` also ends the conversation, but the card stays in `Q&A`; remove `HOLD` and the card is taken again, the agent continuing from its comments. A card taken again later (after `HOLD`, or dragged back) gets the whole comment thread, with what you added since the last answer marked as new.
- **Restarts.** If the Q&A agent or kankan restarts (crash, reboot, a changed model, a replaced Q&A seat, `kankan stop` and a new start), the agent takes up the card under discussion again and reads the dialogue from the comments; when its last answer already covered everything you wrote, it waits for your next comment instead of answering again. An answer interrupted by a crash or a failure is completed, never duplicated. A usage limit puts `Resume` on the card, and what you add meanwhile is forwarded on resumption.
- **The Q&A agent never commits.** It works on a checkout of the agents' branch, reset for every card. If it commits anyway, its answer is still posted but the card gets `Error` with a comment saying so; the commit is discarded when the next card starts.

Attachments of a `Q&A` card are downloaded as for any task and removed when the conversation ends, unless the card was handed over for implementation.

### 6.3 Card instructions

A card's description is its specification: every agent working on the card, the implementer and the reviewer, gets it unchanged. After it you can add **agent sections** that tell some agents **how** to work, for example to consult a second opinion before integrating:

```
Implement feature X: …

## agent: claude review
Consult your advisor before `kankan integrate`.

## agent: all implementation
Write the commit messages in Italian.
```

- Each section starts with an agent heading and runs to the next one or to the end of the description, so write the specification first.
- `## agent: <type>` reaches the agents of that type (`claude`, `codex`, …) in both phases; `## agent: <type> implementation` and `## agent: <type> review` only in that phase; `all` instead of a type reaches every agent. Any heading level works, and case and extra spaces are ignored.
- An agent gets only the sections that name it; when two disagree, the more specific wins. Sections say how to work: they never change what to build and never make an agent skip kankan's steps.
- A misspelt heading (`## agent: cluade`) stops the card from being started: it gets `Error` and a comment listing the problems, and `kankan status` shows it waiting with reason `invalid_headings`. Fix the heading: kankan removes its `Error` and the card is picked up. The other cards go on meanwhile.
- The `Instructions` card, first in the `kankan` list, has ready-to-copy headings for the agent types configured in your project; copy the lines from its grey blocks. kankan keeps that card up to date: `kankan doctor` overwrites edits to it and puts it back at the top of the list.

Agent sections are not read on `Q&A` cards.

## 7. Talking to the agents

| Label | Meaning | What you do |
|---|---|---|
| `HOLD` (you) | do not assign; if in progress, suspend | remove it to make the card eligible again |
| `Question` | the agent asked something (comment with a mention) | answer with a **comment** on the card, or in the tmux session |
| `Error` | the agent or kankan needs help | read the comment; fix, then remove the label |
| `Resume YYYY-MM-DD hh:mm` | usage limit until then | nothing |

Your comment is forwarded to the waiting agent once, and kankan replies "forwarded to the agent". If that reply does not appear, repeat the comment. Comments by any board member are forwarded, except kankan's own and those of people outside the board. On the `Q&A` card under discussion, every comment and attachment you add is forwarded, without waiting for a question ([section 6.2](#62-questions-qa-cards)).

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
kankan status             # seats (alive, free, dead, unavailable), launcher state, current task, waiting cards, the Q&A agent
kankan claims             # card refs: phase, holder, age, branch
kankan doctor             # installation and project checks, fixes and manual steps (§15.5); --no-fix, --offline
kankan reconcile          # report inconsistencies, including attachment directories of cards no longer in progress (§15.3)
kankan reconcile --fix    # repair only the certain cases; also removes those attachment directories
kankan adapter check claude-1         # how kankan reads that agent's screen
kankan adapter check saved-screen.txt --type codex
```

`kankan status` shows the Q&A seat, its state, the card under discussion and the number of other cards waiting in `Q&A`, with a warning when the `Q&A` list is missing or when cards wait and no Q&A agent runs. In `kankan status --json` this is the `qa` object (`null` when neither a Q&A seat nor a `Q&A` list is configured): `seat` (its name or `null`), `liveness` and `launcher_state` (as for the other seats), `card` and `short` (the card under discussion, or `null`), `turn` (the current turn token, or `null`), `queue` (the number of other open cards in `Q&A`) and `warning` (a message, or `null`).

`kankan reconcile` does not report a Q&A card in `Done` as missing a merge, unless it was handed over for implementation afterwards.

Every agent command answers with `--json` an envelope `{"ok", "status", "message", "data"}` and an exit code (0 success, 10 no_work, 11 already_claimed, 12 ineligible, 13 main_moved, 14 needs_action, 15 on_hold, 20 network_error, 21 trello_error, 30 needs_reconciliation, 31 not_owner, 40 config_error, 41 usage_error, 50 internal_error). The audit log is `<git dir>/kankan/log.jsonl`.

## 10. Administration

```bash
kankan release <card> --force        # park a card's ref; the card stays where it is
kankan seat recover <seat> [--kill]  # a seat that is alive but stuck; --kill terminates its pane session first
kankan seat remove <seat>            # only if free, holding no cards and with empty lists; for <type>-qa only if free (its conversation ends)
kankan purge-remote [--remote origin]  # delete refs/agent-* pushed by mistake
```

All of these are recorded in the audit log. A command started before them (for example an `integrate`) can no longer change anything (`not_owner`).

### 10.1 Updating kankan

1. **Pull the latest code and compare versions.** Use the kankan source clone you installed from. Pulling updates that clone while your agents continue using the copy installed by pipx.

   ```bash
   git -C ~/tools/kankan pull --ff-only
   cat ~/tools/kankan/kankan/VERSION
   kankan --version
   ```

   After the pull succeeds, compare the number in the `VERSION` file with the number printed by the installed command. **If they match, you’re done.** If they differ, or your installed kankan reports that `--version` is an unknown option, continue below.

2. **Pause each project that has running agents or autostart enabled.** In each such project clone, run the following commands, substituting its path. Confirm the stop and wait for it to finish before reinstalling. Agents finish their current work, and kankan records the settings it needs to restart them.

   ```bash
   cd ~/coding/myproject
   kankan stop --graceful
   ```

3. **Reinstall once and verify the installed version.** Run these commands from any directory. Plain `pipx install` skips an existing installation, so use `--force` to install the copy you just pulled.

   ```bash
   pipx install --force ~/tools/kankan
   kankan --version
   ```

   Check that the installed version now matches the file from step 1.

4. **Resume each project you paused in step 2.** Return to its project clone and run the commands below. `kankan start` restores the agents and re-enables autostart if it was enabled. Check that the expected agents appear in the status output.

   ```bash
   cd ~/coding/myproject
   kankan start
   kankan status
   ```

## 11. Adapters

How kankan reads each agent's screen (ready, working, usage limit, trust window, known dialogs) is defined by adapters. Override only what changes in `~/.config/kankan/agents.json` (user) or `<git dir>/kankan/agents.json` (project), for example:

```json
{"codex": {"queue_while_working": true}}
```

`full_access` lists the arguments the agent's `command` must contain: kankan runs every agent in **full-access mode** (Claude Code with `--dangerously-skip-permissions`, Codex with `--dangerously-bypass-approvals-and-sandbox`), because a permission prompt or a sandbox stops the agent in the middle of a task (Codex's sandbox, for example, blocks the network kankan needs to reach Trello). If you override `command`, keep those arguments: otherwise `kankan up` refuses to start the agent and `kankan doctor` reports it.

`models` **replaces** the inherited model list, preserving the order you write. It lists each model with the efforts it accepts, the most powerful first (this order is used wherever kankan lists the combinations), which make the items of the setting cards' checklists ([section 6.1](#61-model-and-effort)); `seed` is the combination ticked when the cards are created, and `exit_command` the command kankan types to end the agent before restarting it with another model or effort. When a new model comes out, add it with its efforts to `models` in your `agents.json`; `kankan doctor` then adds it, unticked, to the checklists.

Codex normally discovers its account model catalog through the installed CLI, cached per CLI version; an explicit `models` override takes precedence. Claude uses its shipped list or your override. Each model has an `efforts` array. At most 61 combinations fit the one-key seed picker. Startup and shutdown timeouts are configured locally with `start_timeout_seconds` (120) and `exit_timeout_seconds` (20); probe timeouts and retry intervals use `model_check_timeout_seconds` (90) and `model_check_retry_seconds` (600).

`login_check` is the command `kankan doctor` runs to see whether an agent is logged in (exit 0 = logged in): `claude auth status` and `codex login status` by default; set it to `[]` to skip the check.

See [`docs/adapter-notes.md`](adapter-notes.md) for what was observed on the installed agent versions.

## 12. Advanced usage

### 12.1 Moving a board to another clone

A board is bound to one clone at a time ([section 3.2](#32-kankan-init)). `kankan init --takeover` binds it to the clone where you run it instead. The other clone's agents notice it at their next cycle and stop, interrupted wherever they are.

kankan keeps part of its state only in the clone, never on Trello or on `origin`: the card claims (`refs/agent-*`), the implementation branches, the `blocked-…` worktrees of [section 8](#8-blocked-cards) and, unless `push_main` is on, the agents' integration branch. A takeover leaves all of that behind. To move a board without losing work:

1. In the old clone, run `kankan stop --graceful` and wait: each agent finishes its current task ([section 4](#4-starting-agents)). A plain `kankan stop` would interrupt them mid-task.
2. Let the cards in the seats' `Implemented` lists be reviewed and reach `Done` before stopping, or accept that they are implemented again; resolve the cards in `Blocked` with `kankan resolve` in the old clone, or accept the same.
3. Push the old clone's integration branch to `origin`: `KANKAN_PUSH=1 git push origin kankan-agents` in the old clone (the variable lets this one push past its `pre-push` hook, which otherwise refuses pushes to the agents' branch). The new clone adopts `origin/kankan-agents` when it creates its integration branch; if the new clone already has its own `kankan-agents` (it was bound before the push), hand the old work to the agents in the new clone instead: `git fetch origin`, `git branch old-agents origin/kankan-agents`, `kankan merge-request old-agents`.
4. In the new clone, run `kankan init <board-url> --takeover`.

Before binding, `--takeover` checks the board and the remote. When cards are still in seat lists or in `Blocked`, or `origin` has no integration branch, it lists them with what to do and asks `Take over anyway? [y/N]`; declining leaves the board unchanged. When nothing is found it proceeds without asking.

After a takeover that left cards behind, run `kankan reconcile --fix` in the new clone (`kankan doctor` points to it). It moves each card left without a claim, in a seat list whose agent is not running or in `Blocked` without its worktree, back to the **top** of `Backlog` with a comment. The card keeps its description, comments, attachments, labels and dependencies, and is implemented again from scratch: the work done on it in the old clone is lost unless you recover it from there yourself.

## 13. Limits

- The agents run in full-access mode: no permission prompts and no sandbox, with all the rights of the user running kankan. Use a dedicated user or machine (a VM or a container) if that is too much for your environment.

- Daemons that detach from the agent's terminal session (for example the Gradle daemon) are not terminated when a dead seat is recovered; processes still running in the seat worktree are reported in the audit log (`seat_escapees`).
- Human changes to a card's list are honoured on a best-effort basis; `reconcile` reports leftovers.
- If the agents integrate into a branch you have checked out (the old `main` setup), your files fall behind when it advances.
- Exclusion between two clones bound to the same board relies on the registry check; it is not absolute.

## Appendix A. Command reference

The commands you run yourself, in the project clone unless stated otherwise. The commands the agents run (`task`, `done`, `sync`, `verify`, `integrate`, `note`, `ask`, `fail`, `block`, `yield` and the Q&A answer commands) are left out: the agents learn them from the kankan skill.

Every command accepts `--json`, which prints the result as the envelope `{"ok", "status", "message", "data"}` instead of text; the exit codes are listed in [section 9](#9-diagnostics). `kankan --help` and `kankan <command> --help` print the same options.

### Setup

**`kankan skill install [--force]`**: links the shared kankan skill into `~/.claude/skills` and `~/.codex/skills` ([section 2](#2-installation)). Can be run from any folder. A link to another location is replaced; a real file or folder in its place is left alone with a warning.
- `--force`: recreate the links even when they are already correct.

**`kankan init [<board-url>] [--credentials <file>] [--reauth] [--takeover]`**: binds this clone to a Trello board ([section 3.2](#32-kankan-init)). Safe to run again: it keeps what is already set up and offers to change the test command ([section 3.3](#33-the-test-command)).
- `<board-url>`: the board's URL, `https://trello.com/b/<shortLink>/<name>`. Required the first time; afterwards it defaults to the board already bound. A different board is refused.
- `--credentials <file>`: a `.properties` file holding the Trello key and token (`kankan.trello.key`, `kankan.trello.token`); kankan writes newly entered credentials there too.
- `--reauth`: only ask for new credentials, changing nothing else.
- `--takeover`: bind a board that is bound to another clone; the other clone's agents stop ([section 12.1](#121-moving-a-board-to-another-clone)).

**`kankan doctor [--no-fix] [--offline]`**: checks the installation and the project, fixes what is safe and prints numbered manual steps for the rest (exit 14 while any remain) ([section 2](#2-installation)).
- `--no-fix`: only report, change nothing.
- `--offline`: skip the checks that need Trello.

### Agents

In these commands `<type>` is `claude` or `codex`.

**`kankan up <type>=<n>... [qa=<type>]`**: makes sure that `<n>` agents of each type run, starting only the missing ones ([section 4](#4-starting-agents)). For example `kankan up claude=2 codex=1 qa=claude`.
- `qa=<type>`: also run the Q&A agent, of that type ([section 6.2](#62-questions-qa-cards)).

**`kankan agent <type> [--seat <seat>] [--qa] [--attach]`**: starts one agent in its own tmux session.
- `--seat <seat>`: the seat to use (for example `claude-2`); by default kankan picks one.
- `--qa`: start the Q&A agent, in the seat `<type>-qa`.
- `--attach`: attach your terminal to the agent's tmux session.

**`kankan autostart enable <type>=<n>... [qa=<type>]`**: installs and enables the systemd user service `kankan-up@<project>.service`, which runs `kankan up` with these arguments at boot.

**`kankan autostart disable`**: disables that service.

**`kankan stop [<seat>] [--graceful] [--yes]`**: without a seat, stops every agent of the project, the Q&A agent included, and disables autostart, after showing what it will stop and asking for confirmation ([section 4](#4-starting-agents)).
- `<seat>`: stop only this agent, after its current task; autostart is not changed.
- `--graceful`: wait for each agent to finish its current task instead of quitting it immediately.
- `--yes`: do not ask for confirmation (required in scripts).

**`kankan start`**: starts again the agents and the autostart that the last `kankan stop` stopped.

### Working with the agents

**`kankan merge-request [<branch>] [--remote] [--bottom]`**: creates a card asking the agents to merge one of your branches into their integration branch ([section 4.1](#41-working-alongside-the-agents)).
- `<branch>`: your branch; default `user_branch` from the configuration (normally `main`).
- `--remote`: take the branch from `origin`, fetched now (for work pushed from another clone).
- `--bottom`: put the card at the bottom of `Backlog` instead of the top.

**`kankan resolve <shortLink> [--done]`**: resolves a card in `Blocked` by hand ([section 8](#8-blocked-cards)). `<shortLink>` is the card's short link from its URL.
- without `--done`: lists the conflicts and opens the tmux session `kankan-resolve-<project>-<shortLink>` in the blocked worktree;
- `--done`: checks the merge, removes the worktree and sends the card back to review.

### Diagnostics

**`kankan status`**: seats (alive, free, dead, unavailable), launcher states, current tasks, waiting cards and the Q&A agent ([section 9](#9-diagnostics)).

**`kankan claims`**: card refs with their phase, holder, age and branch.

**`kankan reconcile [--fix]`**: reports inconsistencies between the board, the refs and the worktrees.
- `--fix`: also repair the cases where the fix is certain, including moving stranded cards back to `Backlog` and removing leftover attachment directories.

**`kankan adapter check <seat> | <file> [--type <type>]`**: shows how kankan reads an agent's screen ([section 11](#11-adapters)).
- `<seat>`: a running agent's seat, for example `claude-1`;
- `<file>`: a file holding a saved screen;
- `--type <type>`: the agent type for a screen file (by default `codex` if the file name contains `codex`, else `claude`).

### Administration

These are recorded in the audit log ([section 10](#10-administration)).

**`kankan release <card> --force`**: parks a card's ref, overriding its holder; the card stays where it is on the board. `--force` is required.

**`kankan seat recover <seat> [--kill]`**: frees a seat that is alive but stuck: its session loses the seat, and the cards it held get a comment.
- `--kill`: first terminate the seat's tmux session and its processes.

**`kankan seat remove <seat>`**: deletes a seat. Only if it is free, holds no cards and its lists are empty; a Q&A seat only if it is free (its conversation ends).

**`kankan purge-remote [--remote <name>]`**: deletes the `refs/agent-*` refs pushed to a remote by mistake.
- `--remote <name>`: the remote, default `origin`.

## Appendix B. Configuration reference

kankan keeps each clone's configuration in `<git dir>/kankan/config.json`; `kankan init` creates it. `<git dir>` is usually `.git` in the project clone. The file stays in the clone and is never on the board or on `origin`. Policy settings that can change while agents run (agent, model and effort) are set on the board instead ([section 6.1](#61-model-and-effort)).

Edit the file only with no agent running (`kankan stop`, edit, `kankan start`): every agent reads it when it starts. Keep the JSON valid, and give numbers as numbers, not strings. A malformed file or value stops every command with `config_error` and the reason. A key you leave out takes its default value.

### Settings you may edit

**Tests** ([section 3.3](#33-the-test-command))

- `test_command` (text, default `""`): the shell command run before each integration; `""` means none.
- `test_timeout` (seconds, default `1800`): the longest a test run may last before it counts as failed.

**Agents and seats**

- `seat_cap` (number, default `4`): the maximum number of seats per agent type ([section 4](#4-starting-agents)).
- `idle_ready_minutes` (minutes, default `10`): how long an agent may sit idle with a task, or show a screen kankan cannot read, before kankan puts the `Error` label on the card. A Q&A agent that sits idle this long gets a fresh session; the card's conversation continues later from its comments.
- `idle_exit_minutes` (minutes, default none): if set, an agent with nothing to do exits after this long, freeing its seat. Leave unset to keep agents waiting for work.
- `write_quiet_seconds` (seconds, default `20`): how long you must have stopped typing in an agent's pane before kankan types into it.
- `tail_lines` (number, default `30`): how many lines at the bottom of an agent's screen kankan reads to tell its state. Raise it only if `kankan adapter check` shows it misreads the screen ([section 11](#11-adapters)).

**Board polling**

- `poll_seconds` (seconds, default `120`): how often an idle agent checks the board for work and comments. Lower means faster reactions and more Trello requests.
- `qa_poll_seconds` (seconds, default `30`): the same for the Q&A agent ([section 6.2](#62-questions-qa-cards)).
- `settings_poll_seconds` (seconds, default `30`): how often the board's setting cards are read ([section 6.1](#61-model-and-effort)).
- `trello_timeout` (seconds, default `5.0`): how long one Trello request may take before it counts as a network error.

**Branches and integration**

- `push_main` (`true`/`false`, default `false`): push the integration branch to `origin` after each integration, so other clones can pull it ([section 4.1](#41-working-alongside-the-agents)).
- `user_branch` (text, default `main`): your own main branch, used as the default branch of `kankan merge-request`. kankan never changes it.
- `main_moved_retries` (number, default `5`): how many times a reviewer may find the integration branch moved during an integration before kankan puts the `Error` label on the card; the reviewer keeps retrying, but you are told.

Change `integration_branch` (default `kankan-agents`) with `kankan init`, never in the file: `init` creates the new branch and refuses while seats hold cards.

**Attachments**

- `attachment_max_bytes` (bytes, default `26214400`, 25 MiB): card attachments larger than this are listed but not downloaded ([section 6](#6-the-workflow)).

**Names and paths**

- `project_name` (text, default the clone's folder name): the name used in tmux session names (`ai-<project>-<seat>`) and in the worktree path. Set it when two clones share a folder name: `kankan up` and `kankan agent` refuse to start and tell you to set it. Change it only when the project has no seats in use: existing worktrees are not moved.
- `worktree_root` (path, default `~/coding/.kankan/<project>`): where the seats' worktrees are created. The same caution applies.

**Credentials** ([section 3.1](#31-a-trello-account-for-kankan))

- `credentials_path` (path, default `~/.config/kankan/credentials`): the `.properties` file holding the Trello key and token; `kankan init --credentials <file>` sets it.
- `credentials_keys` (two texts, default `["kankan.trello.key", "kankan.trello.token"]`): the property names of the key and the token in that file. Change them only if the file already uses other names.

### Settings not to edit

kankan writes the rest itself, and a wrong value breaks the binding to the board: `board_id`, `board_short` and `registry_id`; the list and label IDs (`backlog`, `done`, `blocked`, `qa_list`, `settings_list`, `label_hold`, `label_question`, `label_error`); `seats`; `kankan_member_id` and `kankan_username`; `new_conversation`; and the test settings `trello_base_url` and `settle_seconds`. To repair them run `kankan doctor`, or `kankan init` again.
