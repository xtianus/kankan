# kankan doctor and live agents end-to-end run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dedicated integration branch `kankan-agents` with merge-request cards (the user keeps `main`), a `kankan doctor` command that checks and repairs an installation, and a manually launched runner that drives one real Claude Code agent and one real Codex agent through the whole kankan workflow on the live Trello test board, with the user watching in tmux and taking part in an `ask` and a `HOLD`.

**Architecture:** A standalone module `tests/live/agents_e2e.py` (run with `python -m tests.live.agents_e2e`, never collected by pytest) builds a throw-away clone with a local bare `origin`, binds it to the test board with `kankan init`, creates scenario cards in `Backlog`, starts `kankan up claude=1 codex=1` on the user's default tmux server, watches the board and guides the user through the interactive steps, then verifies Git and Trello and tears everything down. Pure helpers live in `tests/live/e2e_support.py` and are unit-tested against the fake Trello server in the normal suite.

**Tech Stack:** Python ≥ 3.10 standard library, pytest (unit tests of the helpers only), real `git`, `tmux`, `claude`, `codex`, Trello REST.

**Spec:** `aispecs/kankan_spec.md` (§5.1 board, §6.1 init, §8 launcher, §9.4 HOLD, §11–12 branches and integration, §13 skill and signals, §14 labels and comments, §18.2 test board). Developer docs: `docs/developer-manual.md` §3.

## Global Constraints

- Runtime code (`kankan/`) uses the standard library only, Python ≥ 3.10; the runner and helpers live under `tests/` and may import `kankan` and `tests.live.conftest`.
- Python uses 4-space indentation. Everything in English.
- Every commit that changes a Trello endpoint call revises `aispecs/kanban_requirements.md` in the same commit.
- Before any board write, check that the board description contains `kankan-test-board` (`tests.live.conftest.MARKER`); otherwise stop with exit code 3.
- Board configuration comes from `tests.live.conftest.live_config()` (`KANKAN_TEST_BOARD_URL`/`KANKAN_TEST_CREDENTIALS` or `~/.config/kankan/test.properties`); never from the repository.
- Workflow lists and labels use the real names (`Backlog`, `Done`, `Blocked`, `<seat> · In Progress|Implemented|Review`, `HOLD`, `Question`, `Error`) and are **kept** on the board between runs (adopted by name). Only the run's cards are created and deleted.
- Run cards are named `<run-id> <title>` with `run-id` = `ke-<epoch seconds>-<6 hex>`; cleanup recognizes the `ke-` prefix (the API smoke test keeps `kt-`).
- Execution order: Task 1, Task 4, then Tasks 2, 3, 5, 6, 7 (Task 4 introduces the integration branch the others rely on).
- Commit only once, at the end of Task 7 (user rule: no commit per step of a linear workflow), then push to origin.
- Markdown: one line per paragraph or list item, no hard wrapping.
- The runner is never started by `pytest` (no `test_` prefix, no marker).

## Review Focus

- Leftover cards in `Backlog`, `Done`, `Blocked` or seat lists from another run or from manual use: the agents would take them, or `reconcile` would report Done cards without a merge in the fresh clone. The runner must refuse to start **before touching the registry** and name them (test in Task 2).
- A previous run interrupted with `--keep` or a crash leaves the registry block and `ai-kankan-e2e-*` tmux sessions: a new run must refuse with a clear message about the sessions and must take over the registry (test in Task 2 for the board; Task 6 checks sessions).
- The user answers the `ask` from a Trello account different from the token owner: kankan forwards only the configured member, so the runner prints the expected `@username` and accepts `--notify` (Task 6).
- Ctrl-C at any point: panes captured first, agents stopped within a bounded wait, this run's cards deleted, the registry block removed only if it is still this run's, run directory kept and printed (Task 6, manual check).
- "All cards in Done" is reached before integration side effects settle (ref deletion, label removal, push): verification must wait for a settled state, or healthy runs fail intermittently (Task 6).
- kankan's own comments are posted by the same Trello member as the user's answer: the answer must be picked by the absence of the `` `kankan:<op>` `` marker, not by author (test in Task 3).

---

### Task 1: `create_card` accepts a description

**Files:**
- Modify: `kankan/trello.py` (`TrelloClient.create_card`)
- Modify: `tests/fakes/fake_trello.py` (POST `/1/cards` stores `desc` if not already)
- Modify: `aispecs/kanban_requirements.md` (row C6: card creation with title and description)
- Test: `tests/test_trello.py`

**Interfaces:**
- Produces: `TrelloClient.create_card(list_id: str, name: str, desc: str = "") -> Card` — sends `desc` in the POST body only when non-empty.

- [ ] **Step 1: Write the failing test** `test_create_card_with_description`: on a fake board, `client.create_card(list_id, "t", desc="do X")` returns a `Card` with `desc == "do X"` and `client.card(card.id).desc == "do X"`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_trello.py -k create_card` — expected FAIL (unexpected keyword `desc`).
- [ ] **Step 3: Implement** the parameter in `create_card` and, if the fake ignores it, read `desc` from the POST body in the fake.
- [ ] **Step 4: Update C6** in `aispecs/kanban_requirements.md`: "Card creation (title and description) and deletion …", mapping `POST /1/cards` (`idList`, `name`, `desc`).
- [ ] **Step 5: Run** the test file again — expected PASS.
- [ ] **Step 6: No commit yet** (single commit at the end of Task 7).

### Task 2: Run setup and board hygiene helpers

**Files:**
- Create: `tests/live/e2e_support.py`
- Test: `tests/test_e2e_support.py` (normal suite, fake Trello, `git_repo`/`fake_trello` fixtures)

**Interfaces:**
- Consumes: `kankan.init.init`, `kankan.config.{Paths, load_config, save_config, registry_block, REGISTRY_START, REGISTRY_END}`, `TrelloClient.create_card(..., desc=)`.
- Produces:
  - constants `RUN_PREFIX = "ke-"`, `PROJECT_NAME = "kankan-e2e"`, `TEST_COMMAND = "sh check.sh"`, `SEAT_LIST = re.compile(r"^(claude|codex)-\d+ · ")`
  - `@dataclass ScenarioCard(key: str, title: str, desc: str, interactive: Optional[str])` — `interactive` is `None`, `"ask"` or `"hold"`
  - `new_run_id() -> str`
  - `scenario(ask: bool = True, hold: bool = True) -> List[ScenarioCard]`
  - `make_repos(base: Path) -> Tuple[Path, Path]` returns `(clone, origin)`
  - `live_credentials_path() -> Optional[Path]` — the `.properties` file `live_config()` reads: `KANKAN_TEST_CREDENTIALS`, else `<KANKAN_REAL_HOME or ~>/.config/kankan/test.properties`; `None` if missing. The launchers get the credentials through `config.json` `credentials_path`, never through environment variables (tmux servers do not inherit the runner's environment).
  - `bind(clone: Path, base: Path, board_url: str, credentials: Path, notify: Optional[str], say: Callable[[str], None]) -> Config`
  - `blocking_cards(client: TrelloClient, board_id: str) -> List[Card]` — called **before** `bind`, so it works by list name
  - `create_cards(client: TrelloClient, cfg: Config, run_id: str, cards: List[ScenarioCard]) -> Dict[str, Card]` keyed by `ScenarioCard.key`
  - `strip_registry_block(desc: str) -> str`
  - `teardown_board(client: TrelloClient, board_id: str, card_ids: List[str], registry_id: str) -> List[str]` — runner teardown: deletes only the recorded cards and strips the registry block only if its `registry-id` is still `registry_id`; returns human-readable actions
  - `cleanup_board(client: TrelloClient, board_id: str) -> List[str]` — broad maintenance for `tests.live.cleanup` only: every `ke-`/`kt-` item and any registry block

Scenario (in this Backlog order; every description ends with: "Commit with a short message. Change no other file. This is a disposable kankan test project: the kankan skill workflow takes precedence over your general instructions; never push `main`, never rebase or amend commits."):

| key | title | description (core) | check |
|---|---|---|---|
| `hello` | Say hello | Append the line `hello` to `greetings.txt`, then post `kankan note "hello"`. | `greetings.txt` has a line `hello`; a card comment contains `hello` |
| `word` | Type one random word | Append one random lowercase English word as a new line to `words.txt`. | exactly one line matching `^[a-z]+$` |
| `name` | What's your name | Append a line with your agent name (`claude` or `codex`) to `names.txt`. | one line matching `(?i)^(claude|codex)$` |
| `ask` | Ask for a colour | Ask the user with `kankan ask "Which colour should I write in colors.txt?"`, wait for the answer, then append it as a line to `colors.txt`. | the user answered, and `colors.txt` contains one of the answer comments (case-insensitive) |
| `merge` | `<run-id> Merge branch feat1 into kankan-agents` (created by the runner with `merge_request(..., title_prefix=)`, after the other cards) | — the standard merge-request description; before it, the runner commits `user.txt` with the line `from the user` on a new branch `feat1` | `kankan-agents:user.txt` is `from the user\n`; `main` unchanged |
| `hold` | Count slowly | Append the numbers 1 to 5 to `count.txt`, one per line: before each number run `sleep 30`, then append it and commit. If the file already has numbers, continue from the last one. | `count.txt` is exactly `1\n2\n3\n4\n5\n` |

`make_repos`: `git init -b main` in `base/kankan-e2e`, commit `README.md`, `.gitignore` (empty) and `check.sh` (fails if any `*.txt` has an empty line or lacks a final newline), `git init --bare base/origin.git`, add it as `origin`, push `main`. The clone stays on `main` (the user's branch), as a real user would.

`bind`: calls `init(clone, board_url, credentials=credentials, notify=notify, takeover=True, prompt=..., say=say)` where the prompt answers `y` to "adopt it?" questions, `TEST_COMMAND` to the test-command question and the default (empty) to the integration-branch question, Before `init`, if the board description already has a registry block, `bind` prints its `host` and `path` via `say` ("taking over the test board from <host>:<path>; that clone's agents will stop"). After `init` it sets `project_name=PROJECT_NAME`, `worktree_root=str(base / "worktrees")`, `push_main=True`, `poll_seconds=10` and saves.

`blocking_cards`: open cards in any open list named `Backlog`, `Done` or `Blocked` or matching `SEAT_LIST`. Foreign cards are never deleted: the runner lists them and the user archives them (or runs `tests.live.cleanup` for `ke-`/`kt-` leftovers).

- [ ] **Step 1: Write the failing tests:**
  - `test_scenario_flags`: `scenario(ask=False, hold=False)` keys are `["hello", "word", "name"]`; the default has all five with `interactive` set only on `ask` and `hold` (the `merge` card is added by the runner).
  - `test_make_repos`: `origin` has `main` equal to the clone's `main`; the clone has `main` checked out; `sh check.sh` exits 0 in the clone.
  - `test_bind_adopts_existing_lists`: with lists `Backlog`, `Done`, `Blocked` and label `HOLD` already on the fake board, `bind` reuses their IDs and creates no duplicate list; config has `push_main`, `project_name == "kankan-e2e"`, `worktree_root` under `base`, `test_command == "sh check.sh"`.
  - `test_bind_takes_over_foreign_registry`: a board description with another registry block plus `kankan-test-board` binds successfully and keeps the marker text.
  - `test_blocking_cards`: cards in `Backlog`, `Done` and `claude-1 · Review` are returned; one in a foreign list is not; the board description is unchanged.
  - `test_seat_lists_adopted`: after `bind`, `seats.ensure_created(project, "claude-1", "claude")` on a board that already has the three `claude-1 · …` lists reuses their IDs.
  - `test_create_cards_order_and_names`: names are `<run-id> <title>`, descriptions set, Backlog order equals scenario order.
  - `test_teardown_board`: deletes only the given card IDs (another `ke-` card survives); strips the registry block when its ID matches, leaves it when another clone took over; keeps `kankan-test-board` and other description text; leaves lists and labels.
  - `test_cleanup_board`: deletes every `ke-` and `kt-` card, archives `kt-` lists, strips any registry block, keeps the marker text.
  - The `bind` tests use the `virtual_time` fixture, so `settle_seconds` does not slow the suite.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_e2e_support.py` — expected FAIL (module missing).
- [ ] **Step 3: Implement** the interfaces above in `tests/live/e2e_support.py`.
- [ ] **Step 4: Run** the tests — expected PASS; then the whole default suite with `setsid -w .venv/bin/python -m pytest -q -m "not slow"`.
- [ ] **Step 5: No commit yet** (single commit at the end of Task 7).

### Task 3: Final verification

**Files:**
- Modify: `tests/live/e2e_support.py`
- Test: `tests/test_e2e_support.py`

**Interfaces:**
- Consumes: Task 2 names; `kankan.project.Project`, `kankan.diag.reconcile`, `kankan.refstore` (card refs), `<gcd>/kankan/log.jsonl`.
- Produces:
  - `user_answers(client: TrelloClient, card_id: str, notify_id: str) -> List[str]` — texts of the comments by `notify_id` that do not end with a `` `kankan:<op>` `` marker, oldest first (the same filter as `forward.poll`, which may forward several together).
  - `settled(project: Project, client: TrelloClient, cards: Dict[str, Card], origin: Path) -> bool` — all run cards in `Done`, no `refs/agent-cards/*`, no `HOLD`/`Question`/`Error` on run cards, `origin` `kankan-agents` equal to the clone's `kankan-agents`.
  - `verify_run(project: Project, client: TrelloClient, cards: Dict[str, Card], origin: Path, ask_ran: bool, hold_ran: bool) -> List[str]` — problems found; empty means pass.

`verify_run` checks, each failure adding one readable line:
1. every run card is in `cfg.done` and carries none of `HOLD`, `Question`, `Error`;
2. no `refs/agent-cards/*` remain;
3. first-parent history of `kankan-agents` since the initial commit has exactly one merge per card (the `merge` card included), each with `Kankan-Card: <short>` and `Kankan-Tested: <hash>` where `<hash>` is its second parent;
4. file checks of the scenario table, read with `git show kankan-agents:<file>`; `main` and `feat1` still point at the SHAs recorded at setup, and the clone still has `main` checked out; `colors.txt` only if `ask_ran`, and then `user_answers` must be non-empty (no answer is a failure, not a skip); `count.txt` only if `hold_ran`;
5. `git -C origin rev-parse kankan-agents` equals the clone's `kankan-agents` (`push_main` pushes the integration branch; push failures are not retried by kankan, so a mismatch is reported together with the `push_failed` events of `log.jsonl`);
6. for every card, `log.jsonl` has a `task_assigned` with `kind == "review"`, and the **last** `task_assigned` with `kind == "impl"` for that card before it names a seat of a different type (`seat.split("-")[0]`); the `hold` card has several `impl` assignments (resumption after `HOLD`);
7. if `hold_ran`, `log.jsonl` shows, in order: a `task_assigned` `impl` for the hold card; a `suspend_requested` from that seat; a `transition` of the hold card with `phase == "parked"`; a later `task_assigned` `impl` for the same card with the **same `branch`** (resumption from the existing branch). A HOLD during active work goes through the launcher's suspension request and the agent's `yield`; `hold_yield`/`hold_wip` only appear in recovery paths and are not required;
8. `diag.reconcile(project) == []` (it writes `<gcd>/kankan/reconcile.json` in the disposable clone, which is harmless; foreign Done cards were rejected before the run).

- [ ] **Step 1: Write the failing tests** on a synthetic run built with real Git and the fake board (no agents): create two cards, simulate integrations with `git commit-tree` merges carrying the trailers, write matching `log.jsonl` lines, move cards to `Done`.
  - `test_verify_run_passes_on_consistent_run` → `[]`.
  - `test_verify_run_reports_missing_merge` → one line naming the card.
  - `test_verify_run_reports_same_type_review` → a line naming the card and both seats.
  - `test_verify_run_reports_origin_behind`.
  - `test_user_answers_filters_member_and_marker`: comments `"kankan: question … \`kankan:op1\`"` (notify member), `"red"` (another member) and `"blue"` (notify member) → `["blue"]`.
  - `test_verify_run_ask_without_answer_fails`.
  - `test_verify_run_hold_requires_suspension_and_branch_reuse`: logs without `suspend_requested`, or with a resumed `impl` on a different branch, each give one problem line.
  - `test_settled`: false while a card ref remains or `origin` is behind; true afterwards.
- [ ] **Step 2: Run** them — expected FAIL.
- [ ] **Step 3: Implement** `user_answers`, `settled` and `verify_run`.
- [ ] **Step 4: Run** — expected PASS.
- [ ] **Step 5: No commit yet** (single commit at the end of Task 7).

### Task 4: Agents' integration branch `kankan-agents` and merge requests

**Execution order:** implement this task right after Task 1 (order 1, 4, 2, 3, 5, 6, 7): Tasks 2, 3, 5 and 6 rely on `integration_branch` and `merge_request`.

Covers five points: (1) `init` asks for the agents' branch (default `kankan-agents`) and creates or adopts it; (2) `kankan merge-request <branch>` creates the card; (3) a skill section for merge cards, including "nothing to merge"; (4) ahead/behind between `kankan-agents` and the user's branch in `kankan status` and `kankan doctor` (Task 5); (5) spec and manuals (Task 7).

The agents no longer integrate into `main`: they integrate into a dedicated branch, `kankan-agents` by default, that only kankan moves and nobody checks out. **Reserved branches** are the integration branch and the card branches `??kankan-<shortLink>`; every other branch belongs to the user, who can check it out freely. The user takes the agents' work with a plain `git merge kankan-agents` (it only reads that branch), and gives the agents their own work through a **merge-request card**, which goes through the normal implement → review → verify → integrate workflow. Rule: each branch has one writer; cross-merges only read the other side. The existing integration already fits: `integrate` builds `R` with the integration branch as first parent and the tested card tip (which may contain user history) as second parent.

**Files:**
- Modify: `kankan/config.py` (field `main_branch` → `integration_branch`, default `"kankan-agents"`; `user_branch: str = "main"`; `Config.from_dict` accepts the old key `main_branch` as an alias, and when both keys are present `integration_branch` wins, so already-bound clones keep integrating into `main`)
- Modify (mechanical rename, audited file by file): `kankan/refstore.py` (`RefStore(repo, integration_branch)`), `kankan/project.py`, `kankan/seats.py`, `kankan/agents_cmd.py`, `kankan/launcher.py`, `kankan/workflow.py`, `kankan/worktrees.py`, `kankan/cards.py`, `kankan/integrate.py`, `kankan/agentcmds.py`, `kankan/diag.py` (also the literal "merge on main" wording in `reconcile`), `kankan/cli.py`
- Modify: `kankan/init.py` (branch prompt, creation or adoption, `git config kankan.integrationBranch`), `kankan/hooks/pre-push` (also refuses pushes to `refs/heads/<integration>` unless `KANKAN_PUSH=1`), `kankan/integrate.py` (`_push` sets `KANKAN_PUSH=1`; `merge_request`), `kankan/cli.py` (`merge-request`), `kankan/diag.py` (`status` ahead/behind), `kankan/trello.py` (`create_card(…, pos="bottom")`), `kankan/messages.py`, `kankan/skill/SKILL.md`, `aispecs/kanban_requirements.md` (C6: card creation is now used in production, by `merge-request`, with `pos=top`)
- Test: `tests/test_branches.py` (new), existing tests updated for the rename

**Compatibility names kept** (documented as "the integration branch"): config `push_main`, status `main_moved`, the JSON key `"main"` in `kankan task` and `kankan sync` output (both also gain `"integration_branch"`), `purge-remote` behaviour unchanged. Prose and messages say "the integration branch" or its name.

**Interfaces:**
- Consumes: Task 1 `create_card(list_id, name, desc)`.
- Produces:
  - `Config.integration_branch: str = "kankan-agents"`, `Config.user_branch: str = "main"`.
  - `TrelloClient.create_card(list_id: str, name: str, desc: str = "", pos: str = "bottom") -> Card`.
  - `reserved_branch(cfg: Config, name: str) -> bool` in `kankan/worktrees.py` — the integration branch or `^\d\dkankan-[A-Za-z0-9]{8}$`.
  - `merge_request(project: Project, branch: Optional[str] = None, remote: bool = False, top: bool = True, title_prefix: str = "") -> Card` in `kankan/integrate.py` — `branch` defaults to `cfg.user_branch`; returns the created card.
  - `diag.status` result gains `"branches": {"integration": str, "user": str, "ahead": int, "behind": int, "oldest_unmerged_integration": Optional[str]}` (`ahead`/`behind` count **commits**, card commits and integration merges included; the date is the committer date of the oldest first-parent integration merge of the integration branch not contained in the user branch); `"user": None` with no counts when `user_branch` does not exist.
  - CLI: `kankan merge-request [<branch>] [--remote] [--bottom] [--json]`.

`init`:
- new prompt after the test command: `Branch where the agents integrate [kankan-agents]: ` (on re-runs the configured value is the default);
- `user_branch`: `main` if it exists, otherwise the currently checked-out branch; with a detached HEAD and no `main`, ask `Your main branch: `;
- if the integration branch does not exist locally: if `origin/<name>` exists (after `git fetch origin <name>` when `origin` is configured), create the local branch from it (**adopt**, never reset or force-push); otherwise create it from `user_branch`; say which;
- **changing** the integration branch of an already-configured clone is refused (`needs_action`) while any seat is alive or any card ref exists: "stop the agents and finish or release their cards first". `Project.reload_config` does not rebuild the `RefStore`, so running launchers would otherwise target the old branch;
- answering the same name as `user_branch` (e.g. `main`, the old behaviour) is allowed with a warning that this branch must then not be checked out;
- stores `git config kankan.integrationBranch <name>` for the hook; `worktrees.main_checked_out(repo, cfg.integration_branch)` keeps warning if the **integration** branch is checked out anywhere; user branches never produce a warning.

Single writer, enforced where Git allows: the `pre-push` hook refuses a push whose remote ref is `refs/heads/$(git config kankan.integrationBranch)` unless `KANKAN_PUSH=1` (set only by `integrate._push`). This protects pushes from the kankan clone only; a push from another clone is not protected, which the manual states.

`merge-request`:
- validates `branch` with `git check-ref-format --branch`; refuses (`usage_error`) a reserved branch (`reserved_branch`) and any revision expression;
- resolves the source to a commit **at request time** (snapshot semantics): local `refs/heads/<branch>` by default; with `--remote`, or when no local branch exists, `git fetch origin refs/heads/<branch>:refs/remotes/origin/<branch>` (updates only the remote-tracking ref, never a user branch) and uses `refs/remotes/origin/<branch>`; `usage_error` if neither exists;
- refuses (`needs_action`, "nothing to merge") if that commit is already contained in the integration branch;
- creates in `Backlog` (top unless `--bottom`) a card titled `<title_prefix>Merge branch <branch> into <integration>` whose description (`messages.MERGE_REQUEST_DESC`) names the branch **and the commit SHA**: merge exactly `<sha>` into the card branch (no fetch inside the card; the commit is already in the shared object store); resolve conflicts keeping the intent of both sides; run the tests; commit; then `kankan done`.

Skill (`SKILL.md`), section "Merge-request cards" (title `… Merge branch … into …`): the implementer merges the SHA named in the description; **if there is nothing new** (Git says "Already up to date", or `kankan done` answers `needs_action` because the branch has no commit beyond the integration branch, e.g. another card integrated the same source meanwhile), it creates `git commit --allow-empty -m "Nothing new to merge from <branch> (<sha>)"` and runs `kankan done` again; the reviewer checks every conflict resolution and summarizes them with `kankan note`. Everywhere else "main" becomes "the integration branch (`data.integration_branch`)".

- [ ] **Step 1: Write the failing tests** in `tests/test_branches.py` (fake Trello, `git_repo`, real Git):
  - `test_init_creates_kankan_agents`: default answer → `kankan-agents` at `main`'s commit, `integration_branch == "kankan-agents"`, `user_branch == "main"`, `git config kankan.integrationBranch` set, no warning while `main` is checked out.
  - `test_init_adopts_remote_integration_branch`: a bare origin with `kankan-agents` ahead of `main` and no local branch → the local branch is created at `origin/kankan-agents`.
  - `test_init_refuses_branch_change_with_card_refs`: configured `kankan-agents`, one card ref, re-run answering `other` → `needs_action`, config unchanged.
  - `test_init_user_branch_fallback`: repository whose only branch is `trunk` (checked out) → `user_branch == "trunk"`.
  - `test_init_warns_when_integration_branch_checked_out`.
  - `test_config_keys`: `{"main_branch": "main"}` → `integration_branch == "main"`; both keys present → `integration_branch` wins; saving writes only `integration_branch`.
  - `test_pre_push_refuses_integration_branch`: `git push origin kankan-agents` from the clone is refused; `integrate._push` (with `KANKAN_PUSH=1`) succeeds; pushing `feat1` is allowed.
  - `test_integrate_moves_only_integration_branch`: after an integration `kankan-agents` advanced; `main`, checked out with an uncommitted edit, is unchanged and its worktree untouched.
  - `test_merge_request_card`: `merge_request(project, "feat1")` → card at the top of `Backlog`, description contains `feat1` and its SHA; `kankan-agents`, `26kankan-Ab12Cd34` and `main~1` → `usage_error`; a branch already contained → `needs_action` "nothing to merge"; missing branch → `usage_error`.
  - `test_merge_request_remote`: `feat1` only on origin → fetched into `refs/remotes/origin/feat1`, SHA in the card; a stale local `feat1` is untouched when `--remote` is given.
  - `test_merge_request_histories` (no agents; the merge simulated with Git on a card branch, then `sync`, `verify`, `integrate`, `done` through the agent commands): (a) `feat1` diverged from `kankan-agents`; (b) `feat1` previously merged `kankan-agents`; (c) `feat1` contains commits with `Kankan-Card` trailers. Each: one new first-parent merge on `kankan-agents`, `reconcile` empty.
  - `test_merge_request_conflict_block_resolve`: conflicting `feat1` → the implementer's merge conflicts → `block` → `resolve --done` → review integrates; `kankan-agents` contains both sides.
  - `test_merge_request_nothing_new_after_concurrent_integration`: the card merges `feat1`, another card integrates the same commits first, `done` answers `needs_action`; after the empty commit `done` succeeds and the review integrates.
  - `test_merge_request_flow` (fake agent, real tmux, marker `slow`): user commit on `feat1` adding `user.txt`, `merge_request(project, "feat1")`, fake agents run the merge scenario → card in `Done`, `git show kankan-agents:user.txt` exists, `main` and `feat1` unchanged.
  - `test_status_ahead_behind`: one integrated card (card commit plus integration merge) and one user commit on `main` → `ahead == 2`, `behind == 1`, `oldest_unmerged_integration` is the merge's committer date; `status_text` shows `kankan-agents is 2 commits ahead / 1 behind main`; with `user_branch` missing → `"user": None`.
  - `test_create_card_top`: `create_card(..., pos="top")` places the card first (fake Trello honours `pos`).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_branches.py` — expected FAIL.
- [ ] **Step 3: Implement**: the config rename with alias (one audited pass over the files listed; the default suite must keep passing), `init` prompt, creation, adoption and change guard, the hook and `KANKAN_PUSH`, `reserved_branch`, `merge_request` and its CLI, `status` branches block, `create_card` `pos`, skill and messages, C6 revision.
- [ ] **Step 4: Update the existing e2e smoke** `tests/test_e2e.py`: no `git checkout --detach`; assert the merge lands in `kankan-agents` and `main` is unchanged.
- [ ] **Step 5: Run** `tests/test_branches.py`, then `setsid -w .venv/bin/python -m pytest -q` — expected PASS.
- [ ] **Step 6: No commit yet** (single commit at the end of Task 7).

### Task 5: `kankan doctor`

Checks the machine and, inside a project clone, the project; fixes what it safely can and lists the manual steps for the rest. `kankan up` and `kankan agent` run its offline subset as a preflight.

**Files:**
- Create: `kankan/doctor.py`
- Modify: `kankan/cli.py` (`doctor` command), `kankan/agents_cmd.py` (preflight in `agent`/`up`), `kankan/launcher.py` (agent PATH), `kankan/init.py` (make `_ensure_labels` and `_add_excludes` public as `ensure_labels` and `add_excludes`; update callers), `kankan/adapters.py` and `kankan/adapters_default.json` (optional `login_check`), `kankan/messages.py` (all texts), `tests/conftest.py` (autouse `KANKAN_SKIP_PREFLIGHT=1`)
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `skill_install.{targets, skill_dir, skill_install}`, `init.{install_hook, ensure_labels, add_excludes}`, `worktrees.main_checked_out`, `adapters.load_adapter`, `diag.reconcile`, `agents_cmd.{unit_path, autostart_enable}`, `Project.open`, `TrelloClient.{me, board, snapshot}`.
- Produces:
  - `@dataclass Check(name: str, status: str, detail: str, action: Optional[str] = None)` with `status` in `"ok" | "fixed" | "manual" | "warn" | "skipped"`; `action` is the exact command or step for `manual` and `warn`.
  - `run(cwd: Optional[Path], fix: bool = True, online: bool = True, types: Sequence[str] = ("claude", "codex")) -> List[Check]` — `cwd=None` runs the machine checks only. Offline project checks use `Paths.discover` and `load_config`; only online checks open a `Project` (it needs credentials). With `fix=False` no mutating helper is called.
  - `preflight(types: Sequence[str]) -> None` — offline machine checks for the given types only (`kankan-path`, `agent:<type>`, `skill`); fixes the skill; raises `KankanError(Status.CONFIG_ERROR, …)` listing every manual action; never gates on `login:<type>`; does nothing when `KANKAN_SKIP_PREFLIGHT=1`.
  - `report_text(checks: List[Check]) -> str` — one line per check (`ok`, `fixed`, `MANUAL`, `warn`, `skipped`), then a numbered "Manual steps" list.
  - `Adapter.login_check: List[str] = []` — argv whose exit status 0 means "logged in"; empty means "cannot verify".
- CLI: `kankan doctor [--no-fix] [--offline] [--json]`; exit 0 (`success`) when no check is `manual`, 14 (`needs_action`) otherwise; `warn` does not fail. `--json` puts the checks in `data.checks`.

Checks, in order (`fix` = what `run(fix=True)` does; otherwise the `action` reported):

| name | condition | fix | manual action |
|---|---|---|---|
| `git` | `git --version` ≥ 2.38 | — | upgrade Git to ≥ 2.38 |
| `tmux` | installed, `tmux -V` ≥ 3.2 | — | install or upgrade tmux to ≥ 3.2 |
| `kankan-path` | `shutil.which("kankan")` exists; `warn` if its shebang interpreter is not `sys.executable` (another installation) | — | add the directory of this kankan to `PATH` (`pipx ensurepath`) |
| `agent:<type>` | the adapter `command[0]` is on `PATH`; `warn` for a missing type if another type is present, `manual` if none is | — | install Claude Code / Codex CLI |
| `login:<type>` | `login_check` exits 0 within 15 s → `ok`; non-zero → `warn` (it may also mean another credential mode); timeout or start error → `skipped`; `skipped` when empty or the agent is missing | — | (`warn`) start `<command>` once and check that you are logged in |
| `skill` | every `skill_install.targets()` is a symlink to `skill_dir()` | `skill_install(force=True)` for symlinks; a real directory is never touched | move `<target>` away, then `kankan doctor` |
| `logind` | `busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager KillUserProcesses` is `b false`; `skipped` if `busctl` fails | — | (`warn`) set `KillUserProcesses=no` in `/etc/systemd/logind.conf`, or agents die when you log out |
| `linger` | only if some `~/.config/systemd/user/kankan-up@*.service` exists: `loginctl show-user $USER -p Linger` is `Linger=yes` | — (persistent account setting: never automatic) | `loginctl enable-linger` (or `sudo loginctl enable-linger $USER`) |
| `project` | `cwd` is inside a Git clone with `<gcd>/kankan/config.json`; outside a clone all project checks are `skipped` | — | `kankan init <board-url>` (and stop: the next checks need it) |
| `adapters` | `load_adapter` succeeds for each type | — | fix the JSON in the named `agents.json` |
| `hook` | the kankan `pre-push` hook exists with current content **and is executable** | `install_hook`, then `chmod 0755` (its existing-hook path never repairs the mode) | the warning text it returns (foreign hook) |
| `exclude` | `/.kankan/` in `<gcd>/info/exclude` | `add_excludes` | — |
| `integration-branch` | the integration branch exists (else `manual`: `kankan init` recreates or adopts it) and `git config kankan.integrationBranch` matches the config (else fixed); `warn` if `main_checked_out(repo, cfg.integration_branch)` is not empty | set the git config key | missing: `kankan init`; checked out (`warn`): switch `<path>` to one of your branches, only kankan may move `<integration>` |
| `branches` | `user_branch` exists (else `skipped`, detail names it): detail `kankan-agents is N commits ahead / M behind <user_branch>`, `ok`; `warn` when `oldest_unmerged_integration` is older than 7 days (age since integration, not since the original commit) | — | (`warn`) `git merge <integration>` on your branch |
| `autostart` | if the unit exists, its `Environment=PATH=` contains the directories of `kankan` and of every installed agent | only when the unit equals `unit_content(project, wanted)` apart from the PATH line (a generated, unedited unit): rewrite the file and `systemctl --user daemon-reload`, keeping its enabled/disabled state (never `autostart_enable`, which also enables) | edited unit: add `<dirs>` to its `Environment=PATH=` |
| `credentials` | online: credentials found and `me()` works | — | `kankan init --reauth` |
| `registry` | online: board registry block names this clone | — | stop the other clone's agents, then `kankan init --takeover` |
| `lists` | online: `Backlog`, `Done`, `Blocked` and every seat list in `cfg.seats` are open | — | unarchive list `<name>` on Trello |
| `labels` | online: `HOLD`, `Question`, `Error` exist; `skipped` if `registry` failed | `ensure_labels` under the project lock, then `save_config` | (`warn` after a fix) restart the agents (`kankan stop <seat>`, `kankan up …`): launchers read label IDs at startup; a recreated `HOLD` does not restore which cards were held |
| `reconcile` | online: `diag.reconcile(project)` is empty | — | (`warn`) `kankan reconcile`, then `kankan reconcile --fix` if appropriate |

`--offline` marks the online checks `skipped`. A failing check never stops the following ones, except `project`, `credentials` and `registry` (the checks that need them become `skipped`; no board repair happens on a board bound to another clone).

Preflight: `up` calls `preflight` **once**, before starting anything, for the types that actually need a new agent (no call when every requested agent already runs); `agent` calls it for its type (`up` passes an internal flag so `agent` does not repeat it). PATH: `agents_cmd.agent` passes `env={"PATH": …}` to `tmux.new_session` (the existing `env` parameter), with `Path(sys.executable).parent` prepended when it contains a `kankan` script, because a tmux server started earlier keeps its own stale environment; the launcher applies the same prepend to the agent's environment.

`login_check` defaults: `codex` → `["codex", "login", "status"]`; `claude` → verify on the installed Claude Code which command reports the login state without starting a session (candidate `["claude", "auth", "status"]`); if none exists leave it empty. Record the finding in `docs/adapter-notes.md`.

- [ ] **Step 1: Write the failing tests** in `tests/test_doctor.py` (fake Trello, `git_repo`, isolated HOME, `monkeypatch` for `PATH` and `shutil.which`, fake `busctl`/`loginctl`/`codex` scripts on a temporary `PATH`):
  - `test_skill_missing_is_fixed`: no targets → check `skill` is `fixed`, both symlinks exist; with `fix=False` it is `manual` and nothing is created.
  - `test_skill_real_directory_is_manual`: `~/.claude/skills/kankan` is a real directory → `manual`, directory untouched.
  - `test_agents_missing`: neither agent on `PATH` → both `agent:*` `manual`; only `claude` present → `agent:codex` is `warn`, `login:codex` `skipped`.
  - `test_login_check`: fake `codex login status` exiting 1 → `login:codex` `warn` with action naming `codex`; exiting 0 → `ok`; sleeping past the timeout → `skipped`; empty `login_check` → `skipped`; none of these makes `preflight` fail.
  - `test_outside_project`: `cwd` not in a clone → every project check `skipped`, exit status ignores them.
  - `test_uninitialized_project`: clone without config → `project` `manual` with action `kankan init <board-url>`, later project checks `skipped`.
  - `test_hook_and_exclude_fixed`: bound project with hook and exclude removed → both `fixed`, hook file present and executable; a kankan hook with mode 0644 → `fixed`, mode 0755.
  - `test_no_fix_never_mutates`: with `fix=False`, missing hook, exclude, skill and label stay missing and the board receives no write.
  - `test_autostart_generated_unit_rewritten`: generated unit with a stale PATH → `fixed`, enabled state unchanged (fake `systemctl` records calls: `daemon-reload` only); an edited unit → `manual`, file untouched.
  - `test_offline_without_credentials`: bound project, credentials file removed, `--offline` → local checks run, no `credentials` failure.
  - `test_missing_label_recreated`: label `Question` deleted on the fake board → `labels` `fixed`, config has the new label ID, a `warn` asks to restart the agents; with a foreign registry the label is **not** recreated.
  - `test_archived_list_manual`: `Blocked` archived → `lists` `manual`, action names `Blocked`.
  - `test_foreign_registry_manual`: registry block of another clone → `registry` `manual` with `--takeover` in the action.
  - `test_integration_branch_checked_out_warns`: `kankan-agents` checked out in the clone → `integration-branch` is `warn`, HEAD unchanged; `main` checked out → `ok`.
  - `test_branches_check`: integration branch 2 commits ahead and 1 behind `main` → detail `kankan-agents is 2 commits ahead / 1 behind main`, status `ok`; a user commit dated 30 days ago imported by a merge request integrated today → still `ok`; an integration merge with committer date 8 days ago not in `main` → `warn`; `user_branch` missing → `skipped`.
  - `test_offline_skips_online_checks`.
  - `test_cli_exit_codes`: `kankan doctor --json` returns 14 with a `manual` check and 0 when all checks are `ok`/`fixed`/`warn`/`skipped`; `data.checks` lists them.
  - `test_preflight_blocks_agent_start`: with `KANKAN_SKIP_PREFLIGHT` unset and `codex` missing, `up(project, {"claude": 1, "codex": 1})` raises `config_error` whose message contains the install action **before any tmux session is created** (no partial start).
  - `test_preflight_skipped_when_running`: all requested agents already counted as live → `preflight` not called.
  - `test_preflight_custom_adapter`: a project `agents.json` overriding `codex.command` to a script on `PATH` passes the `agent:codex` check.
  - `test_agent_session_path`: on a private tmux server started with a stale `PATH`, the session created by `agents_cmd.agent` sees `PATH` starting with `Path(sys.executable).parent`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_doctor.py` — expected FAIL (module missing).
- [ ] **Step 3: Implement** `kankan/doctor.py`, the CLI command, the preflight hook in `agents_cmd.agent`, the launcher PATH change, the public `init` helpers and `Adapter.login_check`; add `KANKAN_SKIP_PREFLIGHT=1` to the autouse fixture in `tests/conftest.py`.
- [ ] **Step 4: Verify `login_check` for Claude Code** on the installed version (log in state and logged-out state if possible) and set the default; write the result in `docs/adapter-notes.md`.
- [ ] **Step 5: Run** `tests/test_doctor.py`, then `setsid -w .venv/bin/python -m pytest -q -m "not slow"` — expected PASS.
- [ ] **Step 6: Manual check** on this machine: `kankan doctor` outside a clone, then in a test clone bound to the test board; every line matches reality and the manual steps are copy-pasteable.
- [ ] **Step 7: No commit yet** (single commit at the end of Task 7).

### Task 6: The runner

**Files:**
- Create: `tests/live/agents_e2e.py`
- Modify: `tests/live/cleanup.py` (use `cleanup_board`: also `ke-` cards and any registry block)

**Interfaces:**
- Consumes: everything from Tasks 2–5 (machine prerequisites: `doctor.run(None, fix=True)`; after `bind`: `doctor.run(clone, fix=False)` must have no `manual` check), `tests.live.conftest.{live_config, MARKER}`, `kankan.skill_install.targets`, `kankan.agents_cmd.up`, `kankan.tmux.Tmux`, `kankan.project.Project`.
- Produces: `main(argv: Optional[List[str]] = None) -> int`; exit codes `0` pass, `1` verification failed or timeout, `2` prerequisites missing, `3` board without marker.

Options: `--no-ask`, `--no-hold`, `--timeout MINUTES` (default 60), `--keep` (leave agents, cards, registry and run directory for inspection), `--notify USER`, `--base DIR` (default the system temp dir). The runner always works in a new `<base>/kankan-e2e-<run-id>/` it creates, and only ever deletes that directory.

Flow:
1. **Prerequisites** (exit 2 with one message each): `live_config()` called at run time (not conftest's import-time `_CONFIG`) and present, plus `live_credentials_path()`; `doctor.run(None, fix=True)` has no `manual` check (git, tmux, both agents, skill symlinks to this checkout, `kankan` on PATH; `login:*` warnings are printed, not fatal); the runner removes `TMUX` and `KANKAN_TMUX_SOCKET` from its environment so `Tmux()` addresses the user's default server, and there is no session named `ai-kankan-e2e-*` on it; board marker present (exit 3).
2. **Setup:** `blocking_cards` must be empty (**before** any board write; list them, suggest archiving them or `python -m tests.live.cleanup`), then `make_repos`, `bind`, `doctor.run(clone, fix=False)` with no `manual` check, `create_cards`, then create `feat1` with one commit adding `user.txt` **without leaving `main`** (a temporary worktree `git worktree add <run dir>/feat1-wt -b feat1`, commit, `git worktree remove`), record the SHAs of `main` and `feat1`, and `merge_request(project, "feat1", top=False, title_prefix=f"{run_id} ")` so the card carries the `ke-` prefix (record all card IDs and the registry ID for teardown). Cards are created in scenario order, the merge request last.
3. **Start:** `up(project, {"claude": 1, "codex": 1})`. Print board URL, `tmux attach -t ai-kankan-e2e-claude-1`, `tmux attach -t ai-kankan-e2e-codex-1`, the expected Trello user for answers (`cfg.notify_username`), and the temp dir.
4. **Watch** every 10 s with one snapshot: print a timestamped line per card only when its list name or labels change (`HH:MM:SS  hold   claude-1 · In Progress  [HOLD]`).
5. **Guide** (print with a terminal bell, never block on `input()`):
   - `ask` card gets `Question` → "Reply with a comment on <url> as @<user>, e.g. 'blue'". When the label goes away, record `user_answer`.
   - `hold` card enters an `In Progress` list → "Add the HOLD label to <url> now". When the card is back in `Backlog` with `HOLD` → "Work suspended. Remove HOLD to let it resume". Then keep watching.
6. **End** when all run cards are in `Done`, then wait up to 5 minutes for `settled(...)` (or timeout / Ctrl-C).
7. **Verify** with `verify_run(..., ask_ran, hold_ran)`; print each problem.
8. **Teardown** unless `--keep`: on failure, timeout or Ctrl-C, **first** save diagnostics in `<run dir>/diagnostics/` (each pane with `capture-pane -p -S -500`, `log.jsonl`, `diag.status_text(diag.status(project))`); then kill the `ai-kankan-e2e-*` sessions and wait at most 60 s for the seats to be dead (if processes survive, say so and keep the run directory); `teardown_board` with the recorded card IDs and registry ID; delete the run directory only on success, otherwise print its path.

- [ ] **Step 1: Write the failing test** `tests/test_e2e_support.py::test_runner_prerequisites_missing_config`: with `KANKAN_REAL_HOME` monkeypatched to the isolated home and `KANKAN_TEST_BOARD_URL`/`KANKAN_TEST_CREDENTIALS` removed, `agents_e2e.main([])` returns 2 and prints "no test board configured" (without the monkeypatch it would find the developer's real `test.properties`).
- [ ] **Step 2: Run** it — expected FAIL (module missing).
- [ ] **Step 3: Implement** `tests/live/agents_e2e.py` and extend `tests/live/cleanup.py`.
- [ ] **Step 4: Run** the test and the default suite — expected PASS.
- [ ] **Step 5: Manual run** with the user: `.venv/bin/python -m tests.live.agents_e2e`. Watch both panes, answer the `ask`, set and remove `HOLD`. Expected: exit 0, "all checks passed", board left with only the permanent lists and labels and no `ke-` cards. Then run once with `--no-ask --no-hold` and interrupt it with Ctrl-C mid-way: expected diagnostics path printed, no `ke-` cards on the board, no `ai-kankan-e2e-*` sessions.
- [ ] **Step 6: Record** in `docs/adapter-notes.md` any new screen or behaviour observed in the real run (for example how Claude Code and Codex react to the queued suspension message).
- [ ] **Step 7: No commit yet** (single commit at the end of Task 7).

### Task 7: Documentation

**Files:**
- Modify: `docs/developer-manual.md` (new §3.5, §1 command list)
- Modify: `aispecs/kankan_spec.md` (§2, §5.2, §6.1, §8.7, §11, §12, §12.5, §13.1, new §15.5, §16.1, §16.2, §18.2)
- Modify: `docs/user-manual.md` (§2, §9, §11)
- Modify: `README.md` (one line on `kankan-agents` and `merge-request` in the quick start)
- Modify: `aiknowledge/notes.md` (Architecture, Testing)

- [ ] **Step 1: developer manual.** In §1 add the line `.venv/bin/python -m tests.live.agents_e2e  # live run with real agents, manual (§3.5)`. Add **§3.5 Live run with real agents** covering: what it does (one card lifecycle per scenario row, real Claude Code and Codex, cross-review, integration and push to a local bare origin); prerequisites (test board as in §3.1–3.3, `claude` and `codex` installed and logged in, `kankan skill install` done, the Trello token belongs to the account that will answer, or `--notify`); it is never run by `pytest`; cost and time (real tokens, about 15–30 minutes); how to watch (`tmux attach -t ai-kankan-e2e-<seat>`, detach with `Ctrl-b d`); the two interactive steps (answer the question with a card comment; add `HOLD` when asked, remove it after the card returns to `Backlog`); options; what stays on the board (permanent workflow lists and labels) and what is removed (run cards, registry block); diagnostics on failure; the agents load your global `CLAUDE.md`/`AGENTS.md`, so their behaviour follows your personal instructions too. Update §3.4 cleanup text: `tests.live.cleanup` also removes `ke-` cards and any registry block; the runner itself removes only its own cards and its own registry block, and refuses to start while `Backlog`, `Done`, `Blocked` or seat lists hold other cards.
- [ ] **Step 2: spec.** Integration branch: §2 "The main branch is called `main` (configurable)" becomes "The agents integrate into a dedicated branch, `kankan-agents` by default (`integration_branch`); every other branch belongs to the user"; replace "main" with "the integration branch" in §11–§13 where it means the agents' branch; §12.5 becomes "Integration branch and user branches": only kankan moves the integration branch and nobody checks it out; the user takes the agents' work with `git merge kankan-agents` and gives them work with `kankan merge-request <branch>` (one writer per branch; cross-merges only read); old configurations with `main_branch` keep working; §6.1 adds the branch prompt, creation or adoption from origin, and the change guard; §12.4: the merge target saved by `block` is the one in `MERGE_HEAD`, not necessarily the integration branch; §17/§6.1: the `pre-push` hook also refuses pushes to the integration branch except from `integrate` (`KANKAN_PUSH=1`), protecting only the kankan clone; §16.1 adds `kankan merge-request [<branch>] [--bottom]`. Doctor: §8.7: adapters may define `login_check`. New §15.5 `kankan doctor`: purpose, the check table of Task 5 in short form (what is fixed automatically, what is reported), exit codes, the preflight in `agent`/`up` and the agent PATH rule. §16.1: row `kankan doctor [--no-fix] [--offline] [--json]` | Checks the installation and the project, fixes what it can, lists manual steps (15.5). §18.2: add "A manual run with real agents (`tests/live/agents_e2e.py`) uses the workflow lists and labels with their real names, kept on the test board and adopted by name; it creates and deletes only its own cards."
- [ ] **Step 3: user manual.** Replace the "keep `main` not checked out" advice (§4) with a section "Working alongside the agents": `main` and your branches are yours; take the agents' work with `git merge kankan-agents` (or `git pull origin kankan-agents` from another clone; never push to it); give them your work with `kankan merge-request feat1` (push `feat1` first if you work in another clone); `kankan status` shows how far `kankan-agents` and `main` diverge; for the old behaviour answer `main` at `init` and keep `main` not checked out. §2 Installation: after `kankan skill install` add `kankan doctor   # checks git, tmux, agents and their login, skill, PATH; fixes what it can` and say it is the first thing to run when something does not start; mention that `kankan up` refuses to start an agent type whose CLI is missing or whose skill cannot be installed, and that a failed login check is only a warning. §9 Diagnostics: `kankan doctor` inside a clone also checks credentials, board binding, lists, labels, hook and autostart, with `--no-fix` and `--offline`. §11 Adapters: `login_check`.
- [ ] **Step 4: notes.md.** Architecture: add `doctor` (checks and fixes; offline subset is the `agent`/`up` preflight; tests set `KANKAN_SKIP_PREFLIGHT=1`). Testing: add "`python -m tests.live.agents_e2e` is a manual live run with real Claude Code and Codex agents on the test board (developer manual §3.5); it is never collected by pytest."
- [ ] **Step 5: Run** `setsid -w .venv/bin/python -m pytest -q` — expected all pass.
- [ ] **Step 6: Commit once** everything from Tasks 1–7 (the requirements revision of Task 1 travels in the same commit as the `create_card` change), message `feat: kankan doctor; manual live run with real agents`, then push to origin.
