# kankan developer manual

For using kankan see the [user manual](user-manual.md). The design is in [`aispecs/kankan_spec.md`](../aispecs/kankan_spec.md); section numbers below (§) refer to it.

## 1. Setup and tests

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q                 # everything except the live Trello tests
.venv/bin/python -m pytest -q -m trello_live  # live Trello tests only (§3)
```

Runtime code uses the Python standard library only (Python ≥ 3.10); pytest is a dev dependency. Test markers (`pyproject.toml`):

- `tmux`: needs a real tmux server;
- `slow`: slow tests;
- `trello_live`: runs against a real Trello test board; excluded by default (`addopts = "-m 'not trello_live'"`).

The local tests use a fake Trello server, real Git in temporary directories, real tmux and a fake agent (§18.1).

## 2. Agent adapters

The default adapters are in `kankan/adapters_default.json`; the screens they were built from are in `tests/fixtures/screens/` and described in [`adapter-notes.md`](adapter-notes.md).

## 3. Live Trello test board

The `trello_live` tests (`tests/live/`) create, move and delete lists, cards and labels on a real board (§18.2). Use a **dedicated** board: never point them at a board with real work.

### 3.1 Create the board

1. In Trello, create a new empty board (for example "kankan test").
2. Put the marker `kankan-test-board` anywhere in the board description. Before any write the tests check for it and stop (exit code 3) if it is missing: this protects real boards from a wrong URL.
3. Copy the board URL, `https://trello.com/b/<shortLink>/<name>`.

### 3.2 API key and token

The key comes from a Trello *app* (formerly "Power-Up"); one app is enough for kankan and its tests.

1. Open `https://trello.com/apps/admin` (the old `https://trello.com/power-ups/admin` still works) and press **New** under *Your Apps*.
2. Fill the form:
   - *App specifications*: **My app doesn't use Power-up capabilities** (kankan adds nothing to the Trello UI; this cannot be changed later, and it removes the *Iframe connector URL* field);
   - *App name* (for example "Kankan app for tests"), *Workspace* (the app belongs to it), *Email*, *Support contact*, *Author*;
   - press **Create**. The app page opens on its *OAuth 2.0* tab; kankan does not use OAuth 2.0.
3. Open the **Trello Auth** tab, press **Generate a new Trello Auth API key for this app** and confirm with **Generate API key**. The tab then shows the **API key** (and a *Secret*, which kankan does not need).
4. Get a token: open this URL with your key; the page names your app and lists the access it gets. Press **Allow** and the next page shows the token: `https://trello.com/1/authorize?expiration=never&scope=read,write&response_type=token&name=kankan&key=<key>` (this is the URL `kankan init` prints). The **Token** link on the *Trello Auth* tab works too, but asks for the wider scope `read,write,account`.

The same key and token used by `kankan init` work here. Trello does not let you reset an API key, so keep the token private; it stays valid until you revoke it from your Trello account settings.

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

Each run names its lists, cards and labels with a unique prefix `kt-<timestamp>-<random>`; at the end it deletes the cards and labels and archives the lists. An interrupted run can leave some behind; remove every `kt-` item from the test board with:

```bash
.venv/bin/python -m tests.live.cleanup
```

It deletes `kt-` cards and labels and archives `kt-` lists, and refuses to touch a board without the marker.

## 4. Rules for changes

- Every commit that adds, changes or removes a Trello endpoint call revises [`aispecs/kanban_requirements.md`](../aispecs/kanban_requirements.md) in the same commit.
- An adapter for another Kanban backend is complete when it implements every MUST in that document and the `trello_live` acceptance tests pass against a dedicated test board of that product.
- Before publishing the repository, check that no fixture contains real board URLs or tokens (user manual §13).
