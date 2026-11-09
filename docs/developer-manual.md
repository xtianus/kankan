# kankan developer manual

For using kankan see the [user manual](user-manual.md). The design is in [`aispecs/kankan_spec.md`](../aispecs/kankan_spec.md); links marked "spec §" point to its sections, links marked "section" to this manual's.

## 1. Setup and tests

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q                 # everything except the live Trello tests
.venv/bin/python -m pytest -q -m trello_live  # live Trello tests only (§3)
.venv/bin/python -m tests.live.agents_e2e     # live run with real agents, manual (§3.5)
```

Runtime code uses the Python standard library only (Python ≥ 3.10); pytest is a dev dependency. Test markers (`pyproject.toml`):

- `tmux`: needs a real tmux server;
- `slow`: slow tests;
- `trello_live`: runs against a real Trello test board; excluded by default (`addopts = "-m 'not trello_live'"`).

The local tests use a fake Trello server, real Git in temporary directories, real tmux and a fake agent ([spec §18.1](../aispecs/kankan_spec.md#181-strategy)).

## 2. Agent adapters

The default adapters are in `kankan/adapters_default.json`; the screens they were built from are in `tests/fixtures/screens/` and described in [`adapter-notes.md`](adapter-notes.md).

## 3. Live Trello test board

The `trello_live` tests (`tests/live/`) create, move and delete lists, cards and labels on a real board ([spec §18.2](../aispecs/kankan_spec.md#182-test-board)). Use a **dedicated** board: never point them at a board with real work.

Two Trello accounts take part, as in real use: **yours**, which owns the test board, and **the kankan bot account**, a separate Trello account that kankan acts as. Create the bot account as described in [user manual §3.1](user-manual.md#31-a-trello-account-for-kankan) (with an uncommon username such as `kankan-<you>-<random>`, so nobody invites it, or a look-alike, by mistake); the sections below say which account does what.

### 3.1 Create the board

1. Logged in as **yourself** (not as the kankan bot), create a new empty board (for example "kankan test"). The board must belong to your account and workspace, as a real project board would: the tests then reproduce the real scenario, where the bot is a guest on the user's board.
2. Invite the kankan bot account to the board as a normal member. Do not create the board from the bot account: that would hide problems a real user meets, such as the bot's membership and permissions on someone else's board.
3. Put the marker `kankan-test-board` anywhere in the board description. Before any write the tests check for it and stop (exit code 3) if it is missing: this protects real boards from a wrong URL.
4. Copy the board URL, `https://trello.com/b/<shortLink>/<name>`.

### 3.2 API key and token

The key comes from a Trello *app* (formerly "Power-Up"); one app is enough for kankan and its tests. Do every step of this section **logged in as the kankan bot account**, in a private browser window: the app, its key and the token all belong to the bot. Your own account owns only the boards.

1. Open `https://trello.com/apps/admin` (the old `https://trello.com/power-ups/admin` still works) and press **New** under *Your Apps*.
2. Fill the form:
   - *App specifications*: **My app doesn't use Power-up capabilities** (kankan adds nothing to the Trello UI; this cannot be changed later, and it removes the *Iframe connector URL* field);
   - *App name* (for example "Kankan app for tests"), *Workspace* (the bot's own workspace; the app belongs to it), *Email*, *Support contact*, *Author*;
   - press **Create**. The app page opens on its *OAuth 2.0* tab; kankan does not use OAuth 2.0.
3. Open the **Trello Auth** tab, press **Generate a new Trello Auth API key for this app** and confirm with **Generate API key**. The tab then shows the **API key** (and a *Secret*, which kankan does not need).
4. Get a token: on the same **Trello Auth** tab click the **Token** link next to the API key; the page names your app and lists the access it gets (it never expires). Press **Allow** and copy the token. This token also has the `account` scope, which kankan does not need and which is harmless for the dedicated bot account; for the minimal scope open instead `https://trello.com/1/authorize?expiration=never&scope=read,write&response_type=token&name=kankan&key=<key>` (the URL `kankan init` prints).

Because the bot, a member of your test board ([section 3.1](#31-create-the-board)), owns the token, kankan's questions and mentions reach you as notifications, and in the live run of [section 3.5](#35-live-run-with-real-agents) you answer questions as a real user would. Trello does not let you reset an API key, so keep the token private; it stays valid until you revoke it from your Trello account settings.

### 3.3 Configure the tests

The configuration stays outside the repository. Either create `~/.config/kankan/test.properties` (mode 600):

```properties
kankan.test.board.url=https://trello.com/b/<shortLink>/<name>
kankan.trello.key=<key>
kankan.trello.token=<token>
```

or set environment variables:

- `KANKAN_TEST_BOARD_URL`: the board URL (takes precedence over `kankan.test.board.url`);
- `KANKAN_TEST_CREDENTIALS`: path of a `.properties` file with `kankan.trello.key` and `kankan.trello.token` (and optionally `kankan.test.board.url`), used instead of `~/.config/kankan/test.properties`.

The tests run with an isolated `HOME`; the default file is read from the real home directory (`KANKAN_REAL_HOME`, set automatically by `tests/conftest.py`).

Without a complete configuration the live tests are skipped with "no test board configured".

### 3.4 Run and clean up

```bash
.venv/bin/python -m pytest -q -m trello_live
```

Each run names its lists, cards and labels with a unique prefix `kt-<timestamp>-<random>`; at the end it deletes the cards and labels and archives the lists. An interrupted run can leave some behind; remove every `kt-` item (and the `ke-` cards and registry block of an interrupted [section 3.5](#35-live-run-with-real-agents) run) from the test board with:

```bash
.venv/bin/python -m tests.live.cleanup
```

It deletes `kt-`/`ke-` cards and `kt-` labels, archives `kt-` lists, removes any kankan registry block, and refuses to touch a board without the marker. The runner of [section 3.5](#35-live-run-with-real-agents) instead removes only its own cards and its own registry block, and refuses to start while `Backlog`, `Done`, `Blocked` or seat lists hold other cards.

### 3.5 Live run with real agents

`tests/live/agents_e2e.py` drives one real Claude Code agent and one real Codex agent through the whole workflow on the test board, while you watch and take part. It is never run by `pytest`; start it by hand:

```bash
.venv/bin/pip install -e '.[dev]'
ln -s ~/coding/kankan/.venv/bin/kankan ~/.local/bin/kankan   # once: kankan on the login PATH (Codex needs it)
.venv/bin/python -m tests.live.agents_e2e    # options: --no-ask --no-hold --timeout 60 --keep --notify USER --base DIR
```

What it does:

1. Checks the machine with `kankan doctor` (git, tmux, both agents and their login, skill links, `kankan` reachable), the test board configuration ([section 3.3](#33-configure-the-tests)) and its marker, and that `Backlog`, `Done`, `Blocked` and the seat lists hold no cards.
2. Creates a throw-away clone on `main` with a local bare `origin` in `<temp>/kankan-e2e-<run-id>/`, binds it to the test board with `kankan init` (taking over the registry block, after printing who held it), with the real list and label names (`Backlog`, `Done`, `Blocked`, `claude-1 · …`, `codex-1 · …`, `HOLD`, `Question`, `Error`): they are adopted by name and **stay on the board** between runs. The agents integrate into `kankan-agents`, which is pushed to the bare origin.
3. Creates the cards, named `ke-<timestamp>-<random> <title>`: *Say hello*, *Type one random word*, *What's your name* (each appends to a file), *Ask for a colour* (the agent asks you), *Count slowly* (counts to 5 with pauses, for HOLD), and a merge request of a branch `user-work` holding a `user.txt` commit.
4. Starts `kankan up claude=1 codex=1` on your default tmux server and prints the board URL and `tmux attach -t ai-kankan-e2e-claude-1` / `ai-kankan-e2e-codex-1` (detach with `Ctrl-b d`).
5. Watches the board every 10 s, printing each card's list and labels when they change, and rings the bell when you are needed:
   - the colour card gets `Question`: answer with a comment on the card, from the Trello account that owns the token (or the one given with `--notify`);
   - the slow card enters an `In Progress` list: add `HOLD` to it; when it is back in `Backlog`, remove `HOLD` so it resumes from its branch.
6. When every card is in `Done` it waits up to 5 minutes for the side effects (ref deletion, labels, push), then verifies: one merge per card on `kankan-agents` with correct trailers, file contents, `main` and `user-work` untouched, origin up to date, each card reviewed by the other agent type, the HOLD suspension and resumption on the same branch, a clean `kankan reconcile`.
7. Stops the agents, deletes its cards and its registry block, and removes the run directory on success. On failure, timeout or Ctrl-C it first saves pane captures, `log.jsonl` and `kankan status` in `<run dir>/diagnostics/` and keeps the run directory.

It spends real tokens and takes about 15–30 minutes. The agents load your global `CLAUDE.md`/`AGENTS.md`, so your personal instructions apply to them too; the card descriptions tell them that the kankan workflow takes precedence and that they must never push `main`.

## 4. Rules for changes

- Every commit that adds, changes or removes a Trello endpoint call revises [`aispecs/kanban_requirements.md`](../aispecs/kanban_requirements.md) in the same commit.
- An adapter for another Kanban backend is complete when it implements every MUST in that document and the `trello_live` acceptance tests pass against a dedicated test board of that product.
- Before publishing the repository, check that no test fixture contains real board URLs or tokens: `git grep -nE 'trello.com/b/|[0-9a-f]{64}'`.
