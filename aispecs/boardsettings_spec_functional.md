# kankan board settings — functional specification

**Status:** implemented locally; release observation gates remain for Claude exit behavior, UI-originated Trello checklist activity and execution under Python 3.10. See the [implementation review](../aiplans/2026-11-10-boardsettings-review.md).
**Date:** 2026-11-10
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3; this feature changes it as listed in [§4](#4-changes-to-the-initial-specification).
**Companion:** [`boardsettings_spec_technical.md`](boardsettings_spec_technical.md) — how kankan implements this feature.
**Implementation plan:** [`2026-11-10-boardsettings.md`](../aiplans/2026-11-10-boardsettings.md); local validation and remaining release observations are recorded in the [implementation review](../aiplans/2026-11-10-boardsettings-review.md).
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (implemented endpoint capabilities); the user manual's section on model and effort (implemented).

**Split rule.** This file describes what users and agents observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

Some settings are changed from the board instead of the clone's configuration: they are meant for policy choices the user makes while agents run (e.g. which agent, model and effort handle a class of cards). Settings that are structural (list and label IDs, `registry-id`), local to the clone (paths, credentials) or that could run commands (`test_command`) never move to the board: anyone who can edit the board can change a board setting.

The feature adds the `kankan` list of setting cards ([§2](#2-board-settings)) and its first settings, the model and effort of the agents per role ([§3](#3-model-and-effort)).

## 2. Board settings

**The `kankan` list.** It holds **one card per setting**; the card's **name** is the setting's key. A setting's value is held in one of two ways, fixed by the setting:
- **description value:** the card's description is the value; a typo can only affect that one setting;
- **checklist value:** the card's checklists, whose items list every allowed choice; the ticked items are the value ([§3](#3-model-and-effort)). The description holds kankan's help text.

kankan creates the list and the cards ([initial §6.1](initial_spec_functional.md#61-kankan-init-board-url), [initial §15.5](initial_spec_functional.md#155-kankan-doctor---no-fix---offline)). A new description-value card has the setting's current value as its description and, as its first comment, a help text with the allowed values and the default; a new checklist-value card has its current value ticked. The list is recognized by ID: the user may rename or move it.

**Description values.** The value is the whole description with surrounding whitespace removed. Each such setting has a fixed set of allowed values or a bounded number; matching ignores case. An empty description means the default.

**When a change applies.** kankan reads the setting cards every 30 seconds while at least one launcher runs, besides every board read of a launcher and `kankan doctor`; while no launcher runs, an edit waits for the next start or `doctor`. A card is judged only when its last activity on Trello is at least **10 seconds** old (by Trello's clock): until then the user is still editing it, and neither a valid nor an invalid state is acted on. Feedback therefore arrives 10–40 seconds after the last change. A new value applies from the next task start; a task already running keeps the value it started with.

| Case | Behaviour |
|---|---|
| Valid new value | Applied; comment "applied: `<old>` → `<new>`, from the next task"; kankan's `Error` removed if present. |
| Value equal to the current one (e.g. only the case changed) | Nothing. |
| Invalid card fixed back to the value in force | kankan's `Error` removed; comment "fixed; still using `<current>`". |
| The allowed values change (kankan upgrade, adapter override) | Every setting card is judged again, edited or not. |
| Invalid value | The **last valid value stays in force**; `Error` and a comment saying what is wrong and which value is still in use (e.g. "`opsu` is not valid (allowed: …); still using `opus`", "codex: two models ticked"), with a mention ([initial §14.2](initial_spec_functional.md#142-notifications)). Reported once per edit, not on every cycle. |
| Empty description (description value) | Reset to the default; comment "applied: reset to default `<default>`". |
| Card deleted, archived or moved out of the `kankan` list | Description value: as an empty description, back to the default. Checklist value: the last valid value stays in force. A card moved to another list is an ordinary card there (in `Backlog` it becomes a task). `doctor` recreates the setting card with the value in force. |
| Card whose name is not a known setting (typo, rename, a card moved in from another list) | `Error` and a comment "unknown setting `<name>`", with a mention. |
| Two cards with the same key | `Error` on both; the last valid value stays in force until one is removed. |

kankan removes only the `Error` labels it set on setting cards; an `Error` added by the user stays. On each setting card kankan keeps only its **latest 10 comments** besides the help text and deletes its older ones; comments written by people are never deleted.

The settings themselves are defined with the features that use them; `kankan status` shows each setting's effective value and marks those whose card is in error.

## 3. Model and effort

The model and the effort of the agents are board settings with checklist values: they affect the speed and the cost of every task.

**Cards.** Two cards, **`implementation`** and **`review`**, give the model and effort for every task of that role (merge-request cards are implementation). They are the defaults that card classes, a future feature, will override. Each card has **one checklist per agent type**, named after the type (`claude`, `codex`, and any type added later). A checklist lists only the **valid combinations** of model and effort for that agent, one item each, named `<model> · <effort>` and grouped by model (e.g. `sonnet · high`, `opus · max`): not every model accepts every effort, so unsupported pairs are never offered. **Order:** wherever kankan lists combinations (checklists, the seed question, comments) the most powerful comes first: models from the most to the least powerful, and within each model its efforts from the highest down (`fable · max`, …, `fable · low`, `opus · max`, …); the order is the adapter's and the user can change it. The combinations come from the agent's adapter ([initial §8.7](initial_spec_technical.md#87-adapters)); the user adds models released later to the adapter configuration.

**Validity.** A card is valid only when every agent checklist has **exactly one item** ticked and that item is one of the adapter's combinations. There is no default: no tick is an error, as are two ticks, a ticked item that is unknown or renamed, a missing or duplicated checklist, or a checklist for an unknown type. An invalid card follows [§2](#2-board-settings): the last valid value stays in force, `Error`, and a comment that names the problem.

**Seeds.** The values ticked when kankan creates the cards are the **seeds**, configured per agent type and role in the user-editable adapter configuration (`~/.config/kankan/agents.json`, or the project's). kankan ships no seeds: `init` and `doctor` **ask** for each agent type and role without one, listing the combinations in order with one-character keys (`1`–`9`, then `a`–`z`, then `A`–`Z`); a single keystroke answers, any other key repeats the question. The answer is saved as the seed in `~/.config/kankan/agents.json`, keeping the rest of the file, and printed with the file it went to. Without a terminal, `init` and `doctor` do not guess: they stop with the manual step (run `kankan doctor` in a terminal, or set the seeds in `agents.json`). A seed must be one of that adapter's combinations; otherwise `init`, `doctor`, `agent` and `up` refuse it. When Codex's own model list stops offering a seeded combination, that seed counts as missing and `init`/`doctor` ask for a new one. `agent` and `up` do not ask: a missing seed matters to them only when no valid value was ever recorded for that agent type and role, and then they refuse with the same manual step. Seeds are a starting point, not a fallback: kankan uses them only when no valid value has ever been recorded for that agent type and role (the card was invalid from the start, or the agent type was added later), and keeps `Error` on the card meanwhile.

**What the user is told.** Whenever `init` or `doctor` creates or recreates a card, they print the values ticked and where they come from, e.g. "implementation: claude `sonnet`/`high`, codex `<model>`/`high` (seeds from ~/.config/kankan/agents.json; change them there, or tick other items on the card)". `doctor` always prints the effective model and effort per role and agent type.

**Changes to the lists.** When an adapter's lists change (configuration override or kankan upgrade), the cards are judged again at once, and `doctor` adds the new items unticked, removes the items no longer listed and restores the adapter's order. A ticked item that disappears makes the card invalid: `Error`, and the last valid value stays in force until the user ticks another item, provided the agent still accepts it (see below).

**Checking a combination.** The agent CLIs are updated independently of kankan, so any combination can stop working. Before a combination is first used, and again after each new version of an agent CLI, kankan checks it with a tiny one-shot request to that agent (about 2,800 tokens for Claude Code, 2,500 for Codex), run in an empty temporary directory and with every option that keeps instruction files (`CLAUDE.md`, `AGENTS.md`), memory, tools, plugins and settings out of it. The check also runs right after a change is applied, and in `doctor`. If the agent rejects the combination, the setting card gets `Error` with the agent's message, and that agent type takes no task of that role until the user ticks another combination or the CLI changes; its other role and the other agent types continue, and `kankan status` says why cards wait. A check that does not finish (the agent does not answer in time) is repeated after 10 minutes by default; until a check passes, the combination is not used, and `kankan status` shows it as not verified yet. The checks never stop the launcher from noticing `HOLD`, user moves or a stop request. A seat whose agent cannot start with that role's combination starts with the other role's, if it passed; when neither can be used, the seat behaves as an agent dying at startup ([§4](#4-changes-to-the-initial-specification), §15.2).

**Applying the values.** Before assigning a task, the launcher takes the model and effort of the task's role for its agent type. If the running agent was started with other values, the launcher ends it (the adapter's exit command, e.g. `/exit`; signals only if it does not exit) and starts it again with the new values, keeping the seat and its cards; then it assigns the task. Nothing of the task is lost, because every task starts a new conversation. `kankan status` shows each seat's running model and effort, and the claim comment names them (e.g. "implementing with claude `sonnet`/`high`").

## 4. Changes to the initial specification

How this feature changes the initial behaviour described in [`initial_spec_functional.md`](initial_spec_functional.md), by initial section number. The initial file is not edited: with the feature, these statements add to or replace the ones quoted.

**§5.1 Trello board.** Add:

- **Settings list:** `kankan`, one card per board setting ([§2](#2-board-settings)). It is not a workflow list: its cards are never tasks.

**§5.1 Trello board.** Replace the text starting “kankan recognizes lists and labels **only by ID**. The user …” with:

> kankan recognizes lists and labels **only by ID**. The user may create other lists and labels and rename or reorder the workflow ones; kankan ignores what it does not know. An archived or deleted workflow list produces `config_error`; an archived or deleted `kankan` list does not ([§2](#2-board-settings)).

**§6.1 `kankan init <board-url>`.** Replace the text starting “3. finds or creates the lists `Backlog`, `Done`, `Blocked`; …” with:

3. finds or creates the lists `Backlog`, `Done`, `Blocked` and the settings list `kankan`; if lists with those names exist, asks whether to adopt them (an adopted `kankan` list keeps its cards: those that are not settings get `Error`, [§2](#2-board-settings));

**§6.1 `kankan init <board-url>`.** Replace the text starting “11. warns if the integration branch is checked out in any wo…” with:

11. warns if the integration branch is checked out in any worktree ([initial §12.5](initial_spec_functional.md#125-integration-branch-and-user-branches));
12. asks for the missing model and effort seeds, then creates a card for every known setting that has none in the `kankan` list ([§2](#2-board-settings)) and prints the model and effort it ticked and where they come from ([§3](#3-model-and-effort)).

**§14.1 Labels.** Replace the text starting “| `Error` | kankan (`fail`, protocol, inactivity, unknown sc…” with:

| `Error` | kankan (`fail`, protocol, inactivity, unknown screen, invalid setting card) | Intervention needed. |

**§14.1 Labels.** Replace the text starting “The fixed labels are created by `init` and recognized by ID.…” with:

> The fixed labels are created by `init` and recognized by ID. kankan adds and removes labels **one at a time** and never rewrites the whole list, so it does not delete a `HOLD` the user just set. The seat state records the cause of every label set on a task card (on setting cards, the board settings state does, [§2](#2-board-settings)): a user answer removes only the label created by `ask`/`fail` on that card. kankan removes its own labels when the cause ends and when the card reaches `Done`.

**§14.4 What the user does.** Add:

- change a board setting by editing its card's description in the `kankan` list ([§2](#2-board-settings));

**§15.2 Situations and behaviour.** Add:

| `kankan` list archived or deleted | No `config_error`: the last valid settings stay in force, `status` warns, `doctor` restores the list and its cards. |
| Invalid or unknown setting card | `Error` on that card; the last valid value stays in force ([§2](#2-board-settings)). |
| Invalid seed in `agents.json` | `init`, `doctor`, `agent` and `up` refuse it with the allowed models and efforts ([§3](#3-model-and-effort)). |
| Missing seed in `agents.json` | `init` and `doctor` ask for it (in a terminal); `agent` and `up` refuse only when no valid value was ever recorded for that agent type and role, with the manual step ([§3](#3-model-and-effort)). |
| The agent does not restart with the new model or effort (e.g. the CLI rejects the model) | As an agent dying at startup: the supervisor restarts with backoff; the card being assigned gets `Error` with a comment naming the model and effort. |

**§15.5 `kankan doctor [--no-fix] [--offline]`.** Replace the text starting “- **Online:** credentials (`kankan init --reauth`), kankan's…” with:

- **Online:** credentials (`kankan init --reauth`), kankan's account (manual step when the token's owner is an admin of the board: that is a person's token, and that person's answers would be ignored and never notified), board binding (`--takeover`; no board repair on a board bound to another clone), workflow lists (unarchive), the `kankan` list (unarchived or recreated) and its setting cards (missing ones created with the current value and help text; changed descriptions and checklists applied as in [§2](#2-board-settings); checklist items synchronized with the adapters' lists, missing seeds asked (in a terminal, without `--no-fix` and `--offline`), seeds validated, ticked combinations checked (not with `--offline`), and the effective model and effort per role and agent type printed, from the last board read (refreshed first when online), [§3](#3-model-and-effort)), labels (recreated; running launchers must be restarted, and a recreated `HOLD` does not restore which cards were held), `reconcile` issues (warning).

**§15.5 `kankan doctor [--no-fix] [--offline]`.** Replace the text starting “`--no-fix` changes nothing. **Preflight:** `kankan up` (once…” with:

> `--no-fix` changes nothing: it asks no question and writes nothing, on the board or locally. **Preflight:** `kankan up` (once, before starting anything, for the types that need a new agent) and `kankan agent` run the offline checks for the requested types and refuse with the manual steps if an agent CLI is missing or kankan cannot be imported; they create a missing skill link for the requested types but never re-point an existing one; the login check never blocks. When the integration branch is also the user branch (pre-1.2 setup) `kankan.integrationBranch` is not set, so the hook never blocks the user's own pushes.

**§16.1 User commands.** Replace the text starting “| `kankan status` | Seats (alive, dead, free, unavailable), …” with:

| `kankan status` | Seats (alive, dead, free, unavailable), launcher state, current task, waiting cards and reason, board settings with their effective values and the cards in error ([§2](#2-board-settings)), each seat's running model and effort ([§3](#3-model-and-effort)). |

## 5. Acceptance tests

1. `init` creates the `kankan` list and one card per known setting with its value and help text (first comment for description values, description for checklist values); `doctor` creates the cards of settings added later and restores a deleted card or list.
2. A valid edit applies from the next task start, not to the running task, with one "applied" comment; an invalid edit keeps the last valid value, sets `Error` with one comment and a mention, and fixing the card removes the `Error`; an `Error` set by the user stays.
3. For a description value, empty description, deleted card and card moved out of the list reset the setting to its default (for a checklist value they keep the last valid value); an unknown name and duplicate keys get `Error`; an archived `kankan` list does not stop the agents.
4. After more than 10 kankan comments on a setting card, the older ones are deleted; the help comment and comments by people stay. Several launchers on the same edit produce one comment.
5. A card edited less than 10 seconds before the read is not judged; the settings are read every 30 seconds while a launcher runs, once per project however many launchers run.
6. Model and effort checklists: they offer only the adapter's combinations; exactly one ticked combination per agent is valid; none, two, an unknown item, a missing or duplicated checklist are invalid with a comment naming the problem; `doctor` adds new adapter items unticked, and a removed ticked item makes the card invalid.
7. Seeds: `init`/`doctor` ask for each missing seed with one keystroke, listing the combinations most powerful first, save it in `~/.config/kankan/agents.json` keeping the file's other content, and stop without a terminal; the cards are created with the seeds and `init`/`doctor` print them; an invalid seed is refused; a card invalid from the start runs with the seeds and keeps `Error`; `agent`/`up` refuse a missing seed only when no valid value was ever recorded for that type and role.
8. A task whose role needs other values than the running agent's: the agent exits with the adapter's exit command (or signals if it ignores it), restarts with the new flags, keeps the seat and its cards, then gets the task; equal values cause no restart; the claim comment and `status` show the model and effort.
9. Model check: a new combination, or a new CLI version, is checked once from an empty temporary directory without instruction files; a rejected combination sets `Error` on the setting card with the agent's message and that type takes no task of that role until another combination is ticked; a combination the adapter dropped keeps working while the check passes; a check that times out is repeated later and `status` shows the combination as not verified; a seat whose start role is blocked starts with its other role's combination, or fails like an agent dying at startup when neither passed; a running check never delays `HOLD`, user moves or a stop request.
10. Robustness: an invalid card fixed back to the value in force loses its `Error`; an adapter change re-judges unchanged cards; a slow read older than the last applied one is ignored; two `doctor` runs never build the same card twice and an interrupted build is completed by name; `doctor --no-fix` asks nothing and writes nothing; a restart re-checks `HOLD`, user moves and stop requests before the task is given.
