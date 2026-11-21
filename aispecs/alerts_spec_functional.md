# kankan alerts — functional specification

**Status:** specified, not implemented.
**Date:** 2026-11-22
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3, [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md), [`cardinstructions_spec_functional.md`](cardinstructions_spec_functional.md) and [`questions_spec_functional.md`](questions_spec_functional.md); this feature changes them as listed in [§6](#6-changes-to-the-initial-specification), [§7](#7-changes-to-the-board-settings-specification) and [§8](#8-changes-to-the-card-instructions-specification).
**Companion:** [`alerts_spec_technical.md`](alerts_spec_technical.md) — how kankan implements this feature.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (revised when the feature is implemented, [technical §8](alerts_spec_technical.md#8-trello-usage)).

**Split rule.** This file describes what users and agents observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

Some problems stop or hold back kankan's agents without belonging to one card: a launcher that stops on a configuration problem, a launcher that keeps failing, an agent command that refuses to run, agents that fail to start at boot. Today they are visible only in the agent's tmux session, in the audit log or in the systemd journal, so a user who watches only the board never learns about them.

This feature reports them on the board, as comments on a card named **`Alerts`** at the top of the `kankan` list. Each comment mentions the board's admins, so Trello notifies them.

Problems of one card are still reported on that card (`Error`, `Question`, `Blocked`, [initial §14.2](initial_spec_functional.md#142-notifications)), and problems of a setting card on the setting card ([board settings §2](boardsettings_spec_functional.md#2-board-settings)). An alert never replaces those.

## 2. The `Alerts` card

**Place.** The `Alerts` card is the **first card of the `kankan` list**; the `Instructions` help card comes second ([§8](#8-changes-to-the-card-instructions-specification)), then the setting cards. kankan keeps this order: `kankan init` and `kankan doctor` (with fixes) create the card and put the two cards back in this order when the user moved them.

**Always available.** When an alert is due and the card is missing (never created, archived or deleted), kankan creates it at the top of the `kankan` list and posts the alert there. Archiving the card is therefore a way to clear old alerts, never a way to lose new ones.

**Description.** A fixed text kept current by `kankan doctor`, like the `Instructions` card: each comment is one alert about kankan's agents that does not belong to one card; kankan never reads the comments of this card, so the user may delete them or archive the card (kankan creates a new one at the next alert); and the closing line "This card is maintained by `kankan doctor`: edits to it are overwritten."

**Not a setting.** The name `Alerts` is reserved: the card is never an unknown setting. Two open `Alerts` cards in the `kankan` list get `Error` on both, like two setting cards with the same title; meanwhile alerts go to the upper one.

**Old alerts.** kankan keeps its latest 50 comments on the card and deletes older ones, so the card stays readable.

## 3. Alerts

| Alert | When | Posted by | What the comment says |
|---|---|---|---|
| A1 Launcher stopped | A launcher stops on a configuration problem and is not restarted (exit for `config_error` or `usage_error`, for example the `Q&A` list archived, a workflow list archived or deleted, an invalid adapter configuration). | The launcher, before it exits. | The seat, the error message, and that the seat stays stopped until the problem is fixed and the agents are started again (`kankan up`). |
| A2 Launcher failed | A launcher ends with any other error (an internal error, a Trello error) and its supervisor restarts it. Network errors give no alert: the comment could not be posted anyway. | The launcher, before it exits. | The seat, the error message, and that the supervisor restarts the launcher. |
| A3 Seat given up | The supervisor gives up after too many launcher failures in a short time (5 in 10 minutes). | The supervisor. | The seat, the last exit code, and that the seat stays stopped: read the audit log, then start the agents again. |
| A4 Board binding lost | The launcher finds that the board is no longer bound to this repository (different `registry-id`) and stops after its current task. | The launcher. | The seat and that it stops; how to bind again (`kankan init`). |
| A5 Different kankan | An agent command refuses because it runs a different kankan than its seat's launcher ([initial §16.2](initial_spec_functional.md#162-agent-commands)). | The agent command, before it refuses. | The seat, the version and directory of the launcher's kankan and of the command's, and how to restart the agents (`kankan stop`, then `kankan start`). |
| A6 Start at boot failed | `kankan up` fails while it runs without a terminal (in practice, started by `kankan autostart` at boot). | `kankan up`. | The error message, and that the agents of this project did not start. |

**Comment.** Every alert comment has:
1. a first line `kankan alert: <title>`, with the alert's title from the table and the seat when there is one;
2. the problem, in the words of the message shown in the agent's session or printed by the command;
3. what to do;
4. the machine's host name and the kankan version that posted it;
5. the mention of the board's admins other than kankan's own account (the people [initial §14.2](initial_spec_functional.md#142-notifications) mentions for cards kankan created).

**No floods.** A launcher restarted by its supervisor, or an agent that keeps running commands, can meet the same problem many times. The same alert (same alert type, same seat or command, same problem text) is posted **at most once every 24 hours** (`alert_repeat_hours` in the project configuration). A different text is a different alert: for example a different version in A5.

**Never in the way.** Posting an alert never changes what kankan does: the launcher still exits with the same code, the supervisor still restarts it, the agent command still refuses, `kankan up` still fails. When the alert cannot be posted (Trello unreachable, credential refused, `kankan` list missing or archived), it is only recorded in the audit log; it is not posted later. Posting adds at most a few seconds to the exit.

## 4. What the user does

- **Watch.** Trello notifies the board's admins of each alert; the `Alerts` card shows them newest first.
- **Act.** Each alert says what to do. Nothing has to be done on the card itself: kankan does not wait for an answer and never reads replies there.
- **Clear.** Delete comments or archive the card at will.

## 5. Agents

Agents see no difference. An agent command that refuses (A5) answers as before; the alert is posted by the command itself, so the user learns about it even when the agent does not report it.

## 6. Changes to the initial specification

How this feature changes [`initial_spec_functional.md`](initial_spec_functional.md), by its section numbers. That file is not edited.

**§8.1 Process chain.** Add: "The launcher, the supervisor and `kankan up` without a terminal report problems that stop agents as alerts on the `Alerts` card ([alerts §3](alerts_spec_functional.md#3-alerts))."

**§14.2 Notifications.** Add: "Problems of kankan's agents that belong to no card are posted as comments on the `Alerts` card of the `kankan` list, mentioning the board's admins ([alerts](alerts_spec_functional.md))."

**§15.2 Situations and behaviour.** In the rows "The launcher dies", "Different `registry-id`" and "Workflow list archived or deleted", add: "alert on the `Alerts` card". Add a row: "Supervisor gives up after repeated launcher failures | The seat stays stopped; alert on the `Alerts` card."

**§15.5 `kankan doctor`.** In "Project", add: "the `Alerts` card (created; description rewritten when outdated; moved back to the top of the `kankan` list)."

**§16.2 Agent commands.** After the sentence on the code identifier, add: "The refusal is also posted as an alert ([alerts §3](alerts_spec_functional.md#3-alerts), A5)."

## 7. Changes to the board settings specification

How this feature changes [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md), as already changed by [card instructions §8](cardinstructions_spec_functional.md#8-changes-to-the-board-settings-specification). That file is not edited.

**§2 Board settings, "The `kankan` list".** The list holds one card per setting, plus the `Alerts` card ([alerts §2](alerts_spec_functional.md#2-the-alerts-card)) and the `Instructions` help card, which are not settings.

**§2 Board settings, table.** In the row "Card whose name is not a known setting", add: "The name `Alerts` is reserved for the alerts card and is never an unknown setting." In the row "Two cards with the same title", add: "Two `Alerts` cards: `Error` on both; alerts go to the upper one."

## 8. Changes to the card instructions specification

How this feature changes [`cardinstructions_spec_functional.md`](cardinstructions_spec_functional.md). That file is not edited.

**§6 The `Instructions` card.** Replace "kept by kankan as the **first card of the list**, above the setting cards" with "kept by kankan as the **second card of the list**, after the `Alerts` card ([alerts §2](alerts_spec_functional.md#2-the-alerts-card)) and above the setting cards".

## 9. Acceptance tests

1. A launcher stopping on a configuration problem (the `Q&A` list archived under a running Q&A seat) posts A1 on the `Alerts` card, mentioning the board's admins; the launcher's exit code and the supervisor's behaviour are unchanged.
2. A launcher failing with an internal error posts A2; its supervisor restarts it; the same failure after the restart posts nothing new within 24 hours; a failure with a different message posts again.
3. After 5 failures in 10 minutes, the supervisor posts A3.
4. An agent command running a different kankan than its launcher refuses with `config_error` and posts A5 with both versions and directories; a second command with the same mismatch posts nothing new.
5. With the `Alerts` card archived, the next alert creates a new `Alerts` card at the top of the `kankan` list.
6. With Trello unreachable, an alert is recorded only in the audit log, and the launcher exits with the code it had without this feature, after at most a few seconds more.
7. `kankan doctor` creates a missing `Alerts` card, rewrites an edited description and restores the order `Alerts`, `Instructions`, setting cards; `--no-fix` reports each and writes nothing.
8. A card named `Alerts` is not an unknown setting; two of them get `Error`.
9. `kankan up` run without a terminal with a missing agent CLI posts A6; run from a terminal it posts nothing.
10. After 50 alerts, the oldest of kankan's comments on the card are deleted.

## 10. Out of scope

- Alerts while Trello is unreachable or the credential is refused, and posting them later.
- A "resolved" comment when a problem goes away, or an `Error` label on the `Alerts` card while problems are open.
- Reading replies or reactions on the `Alerts` card.
- Other channels (email, chat).
- Problems of one card, which stay on that card.
- The launcher noticing by itself that kankan was upgraded while it runs: the agent commands detect it (A5).
