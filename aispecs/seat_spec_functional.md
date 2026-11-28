# kankan seat commands — functional specification

**Status:** implemented locally ([plan](../aiplans/2026-11-22-seat.md)).
**Date:** 2026-11-22; revised the same day after an independent review (integrated and blocked cards, labels, restart during a removal, unfinished Q&A answers).
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3, [`questions_spec_functional.md`](questions_spec_functional.md) and [`stopall_spec_functional.md`](stopall_spec_functional.md); this feature changes them as listed in [§7](#7-changes-to-other-specifications).
**Companion:** [`seat_spec_technical.md`](seat_spec_technical.md) — how kankan implements this feature.

**Split rule.** This file describes what users observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to other specifications name their file.

## 1. Scope

The commands acting on one seat are grouped under `kankan seat`, so that `kankan stop` and `kankan start` act only on the whole project:

| Command | Effect |
|---|---|
| `kankan seat start <seat> [--attach]` | starts the agent of an existing seat ([§2.1](#21-kankan-seat-start-seat---attach)) |
| `kankan seat stop <seat>` | the agent exits after its current task (was `kankan stop <seat>`, [§2.2](#22-kankan-seat-stop-seat)) |
| `kankan seat recover <seat> [--kill]` | unchanged ([initial §15.4](initial_spec_functional.md#154-administration-commands)) |
| `kankan seat remove <seat> [--graceful] [--yes]` | deletes the seat whatever its state, giving its cards to the rest of the board ([§3](#3-kankan-seat-remove-seat---graceful---yes)) |

`kankan stop` no longer takes a seat: `kankan stop <seat>` is a `usage_error` (kankan has no other users yet, so no alias is kept).

The feature also stops a new Q&A conversation from prompting its agent when the card has nothing new since the latest answer ([§4](#4-qa-card-taken-again-with-nothing-new)), which a removed Q&A seat makes common.

## 2. Starting and stopping one seat

### 2.1 `kankan seat start <seat> [--attach]`

Starts the agent of a seat that already exists, work seat or Q&A seat; the type comes from the seat's name (`claude-2`, `codex-qa`). It is `kankan agent <type> --seat <seat>` without repeating the type, with the same checks before starting (agent CLI, skill link, seeds, project name) and the same automatic recovery of a dead seat. Like `kankan up`, it returns once the agent has occupied the seat, and reports a launcher that did not start; `--attach` instead attaches your terminal to the agent's tmux session from the first moment, as `kankan agent --attach` does. Autostart is unchanged.

| Situation | Result |
|---|---|
| The seat runs already | success, "already running", nothing done (running it again is harmless) |
| Unknown seat | `usage_error`: `seat start` never creates a seat; the message names `kankan agent <type>` and `kankan up`, which create seats |
| The seat's tmux session is still open without a running agent, or it has leftover processes | as `kankan agent --seat` (refused, with `kankan seat recover <seat> --kill`) |
| Being removed gracefully ([§3.3](#33-graceful-removal---graceful)) | started, to finish its cards; it still takes no new card |
| Being removed immediately and interrupted ([§3.7](#37-repeated-request)) | `usage_error`: run `kankan seat remove <seat>` to complete the removal |

`kankan agent` stays the command that adds an agent and, when no seat is free, creates a new one; `kankan up` ensures counts per type.

### 2.2 `kankan seat stop <seat>`

Exactly the former `kankan stop <seat>` ([stopall §1](stopall_spec_functional.md#1-scope)): the agent exits after its current task (a Q&A agent after its current answer), autostart is unchanged, and it refuses with `usage_error` while the seat's launcher runs different kankan code. Its message now points to `kankan seat recover <seat> --kill` and `kankan stop`. Restart the seat with `kankan seat start <seat>`.

## 3. `kankan seat remove <seat> [--graceful] [--yes]`

Removes a seat for good: its agent stops, its cards go elsewhere on the board, its three lists are archived, it leaves the configuration and its worktree is deleted. The seat can be in any state (running, stopped, stuck): stopping it first is not needed. Nothing on Trello is lost: a card's description, comments and attachments go with the card.

### 3.1 Where the cards go

Each card in one of the seat's lists, or held by the seat, goes to the place where the rest of the board continues its work:

| Card | Destination |
|---|---|
| In the seat's `In Progress` (its implementation) | top of `Backlog`. The work committed on the card's branch is kept: the next implementer, of either type, continues from that branch ([initial §11](initial_spec_functional.md#11-branches-and-worktrees)). |
| In the seat's `Review` (another seat's implementation it was reviewing) | back to the `Implemented` list of the card's implementer, as `yield` does, to be reviewed by another agent. |
| In the seat's `Implemented` (its finished work, waiting for review) | the `Implemented` list of another seat of the same type; if there is none, of any other work seat (the card is still reviewed only by an agent of the other type, since kankan reads the implementer from the card's claim, not from the list); if no other work seat exists, the top of `Backlog`, where the next implementer finds the work done on the branch and only confirms it. |
| Already integrated, on its way to `Done` | `Done`, as kankan would have done. |
| On its way to another list (a `block` to `Blocked` or a handoff to `Backlog` whose move had not happened, a card another seat holds) | the list it was going to. A blocked card's merge worktree is untouched. |
| Any other card in its lists (moved there by hand) | top of `Backlog`. |

A card on `HOLD` is moved too and keeps `HOLD`. Every moved card gets a comment naming the removed seat and the reason for the move. The `Question`, `Error` and `Resume` labels the removed seat had set on a card are removed, since nothing could clear them afterwards; other labels stay. A card the seat held that you archived or deleted is left where it is. Cards in `Blocked` are not touched by a removal; `kankan resolve --done` later sends them to an existing seat's `Implemented` list.

The same "implementer's `Implemented`" rule applies afterwards whenever a card returns to the `Implemented` list of a seat that no longer exists (a review `yield`, `kankan resolve --done`): it goes to another seat's `Implemented` list as in the table, never to an archived list. Before, `resolve --done` refused such a card.

### 3.2 Immediate removal (default)

1. kankan lists what the removal does: the agent it stops, each card with its destination, uncommitted changes in the worktree, and the autostart change ([§3.5](#35-autostart-and-stop-record)).
2. **Confirmation**, only when the seat holds cards or its lists contain cards. The question says that the agent is stopped now, that a task in progress is interrupted (an implementation goes back to `Backlog`, a review is lost and the card goes back to `Implemented` for another reviewer), that uncommitted changes in the worktree are lost (what is committed on the card's branch is kept), and that `--graceful` lets the agent finish its cards instead. `--yes` skips it; without a terminal `--yes` is required (`usage_error` otherwise). Any answer other than `y`/`yes` changes nothing.
3. Once confirmed, the seat claims no new card and autostart is adjusted ([§3.5](#35-autostart-and-stop-record)); then the agent and its launcher are terminated, as with `kankan seat recover <seat> --kill`. A card the agent claimed while you were answering is moved by the same rules.
4. Uncommitted changes in the seat's worktree are discarded with the worktree; an unfinished merge is aborted (the next `sync` repeats it).
5. The cards are moved as in [§3.1](#31-where-the-cards-go).
6. When no card is left in its lists, the lists are archived, its worktree is deleted and the seat leaves the configuration.

A seat with no cards is removed without asking, also when its agent is running.

### 3.3 Graceful removal (`--graceful`)

No confirmation is asked: no task is interrupted. Changes the agent left uncommitted outside a task, if any, are discarded with the worktree, as at the end of every task.

1. From now on the seat takes **no new card**: neither from `Backlog` nor for review. Other agents keep reviewing the cards in its `Implemented` list as usual.
2. Its agent finishes the cards the seat holds: its reviews are integrated, and its finished implementations go straight to another seat's `Implemented` list, chosen as in [§3.1](#31-where-the-cards-go) (its own lists receive no new card). A task waiting for your answer (`waiting_human`) or a usage limit keeps it waiting, as for `kankan stop --graceful`; a held card that cannot be worked on now (for example no model configured for its role) also keeps it waiting. A Q&A seat finishes its current answer, also one interrupted by a restart.
3. When the seat holds no card in progress or in review, its agent exits and the removal completes as in [§3.2](#32-immediate-removal-default) steps 5–6: the cards still in its lists (those waiting for review in its `Implemented` since before the request, or moved there by hand) go where [§3.1](#31-where-the-cards-go) says; reviewing them needs no agent of the removed seat.

If the seat is not running and holds cards, the command starts its agent so that it can finish them (as `kankan agent <type> --seat <seat>`, with the same checks). If it holds none, the removal completes at once. Autostart is adjusted as soon as the removal is requested ([§3.5](#35-autostart-and-stop-record)), so a reboot during the wait starts no replacement.

The command shows each change of the seat's state and card while it waits, like `kankan stop --graceful`. **Ctrl-C** ends only the waiting: the removal stays requested and completes by itself when the agent has finished; the command reports `needs_action`. When the agent stops before finishing (`kankan stop`, a reboot, an error), the command reports it with `needs_action`. A seat being removed is shown as `removing` by `kankan status`, with the error of a removal that could not complete. `kankan up` (with or without arguments) and `kankan start` first start such a seat again to finish, or complete its removal when nothing is left, then start the other agents. Running `kankan seat remove <seat>` again while it is being removed gracefully is described in [§3.7](#37-repeated-request).

`--graceful` refuses with `usage_error`, changing nothing, while the seat's launcher runs different kankan code than the command: the agent could not finish its tasks ([stopall §2](stopall_spec_functional.md#2-kankan-stop---graceful---yes) step 2). The message says to run it without `--graceful`, or to restart the agents first (`kankan stop`, then `kankan start`).

### 3.4 The Q&A seat

The Q&A seat has no lists: its cards are in the board's `Q&A` list, which belongs to the board and is never archived or moved by a removal. Removing the Q&A seat ends its active conversation, if any ([questions §3.6](questions_spec_functional.md#36-end-of-a-conversation)); the card stays in `Q&A` with its whole thread. Without `--graceful` an answer being written is interrupted (its parts already posted stay), and the confirmation is asked only when a conversation is active. With `--graceful` the agent first finishes its current answer. A Q&A agent started later takes the card again over its whole thread ([§4](#4-qa-card-taken-again-with-nothing-new)).

### 3.5 Autostart and stop record

As soon as a removal is requested (after the confirmation, or at once with `--graceful`): when the autostart unit asks for more agents of the removed seat's type than the seats of that type left, its count for that type is lowered to the number of seats left (a count of zero is dropped; the `qa=` argument is dropped with the Q&A seat), so the next boot does not create the seat again. A unit left with no argument is disabled. The record of the last `kankan stop` is adjusted the same way. The report shows the new `kankan up` arguments.

Seats being removed never count in `kankan up` without arguments, in the help printed without a stop record, or in the counts recorded by `kankan stop`.

### 3.6 Report and errors

The report lists the cards moved with their destination, the lists archived, the worktree deleted and the autostart change.

| Situation | Result |
|---|---|
| Unknown seat | `usage_error` |
| No terminal, cards present, no `--yes` (immediate) | `usage_error`, nothing changed |
| Confirmation declined | success, "nothing changed" |
| `--graceful` while the seat's launcher runs another kankan | `usage_error`, nothing changed |
| Processes of the seat survive the kill | `needs_action` with `kankan seat recover <seat> --kill`; nothing moved |
| A card move fails on Trello | the claim is already updated; the lists are archived only once no open card is left in them, so the removal stops with `needs_action` naming the cards; running the command again completes it |
| A claim of the seat cannot be read (malformed) | `needs_action` naming it, as `kankan reconcile` reports it; nothing moved |
| Ctrl-C while waiting (`--graceful`) | removal stays requested; `needs_action` |
| `--graceful` again on a seat being removed gracefully | `needs_action`, nothing changed ([§3.7](#37-repeated-request)) |
| Without `--graceful` on a seat being removed gracefully, declined | success, "nothing changed"; the graceful removal goes on |
| The agent stops before finishing (`--graceful`) | `needs_action`; `kankan up` starts it again |

Running the command again (or `kankan up`) after an interruption completes the removal: the seat leaves the configuration only at the very end. A seat already removed is reported as unknown.

### 3.7 Repeated request

When `kankan seat remove <seat>` is run while that seat is being removed gracefully, kankan first says so: the graceful removal is still running, with the seat's current state and card (or that its agent is stopped and waits for `kankan up`). Then:

- **With `--graceful`** (most likely a repetition): nothing changes. kankan says to wait for the removal to complete, follow it with `kankan status`, or run `kankan seat remove <seat>` without `--graceful` to remove the seat now. Result `needs_action`.
- **Without `--graceful`**: kankan asks `Remove <seat> now instead? [y/N]`, listing what the immediate removal does ([§3.2](#32-immediate-removal-default) steps 1–2: the cards and their destinations, the task interrupted, the uncommitted changes lost). This is the only question asked. `--yes` answers it; without a terminal `--yes` is required (`usage_error` otherwise). Confirmed: the immediate removal runs from [§3.2](#32-immediate-removal-default) step 3. Declined: nothing changes and the graceful removal goes on.

A removal that was confirmed without `--graceful` and interrupted (a crash, a survivor process) is simply completed by a new run, with or without `--graceful`, without asking again.

## 4. Q&A card taken again with nothing new

When a Q&A seat takes a card whose thread already has a complete answer from kankan (all its parts posted) and nothing new since (no comment or attachment added after the latest answer), it starts the conversation without prompting its agent and waits, as after a restart ([questions §3.7](questions_spec_functional.md#37-restarts)): the next comment or attachment is delivered with the instruction to read the whole thread first. This happens to the active card of a removed Q&A seat, and to a card dragged back into `Q&A` or freed from `HOLD` without a new comment. Before, the agent was prompted with nothing to answer and could repeat its answer. An answer cut short (for example part `1/3` posted when the seat was removed) does not count: the new agent is prompted and answers again. A comment you posted while the latest answer was being written counts as already seen.

## 5. Acceptance tests

1. `kankan seat stop claude-1` behaves as the former `kankan stop claude-1`; `kankan stop claude-1` is a `usage_error`.
2. `seat remove` of a running seat with no cards removes it without asking: agent stopped, lists archived, configuration entry and worktree gone.
3. `seat remove --yes` of a running seat implementing a card: the agent is killed within seconds, the uncommitted changes are gone with the worktree, the card is at the top of `Backlog` with a comment, and the next implementer continues from the branch's last commit.
4. `seat remove --yes` of a seat reviewing a card: the card is back in its implementer's `Implemented` and another agent of the other type reviews it.
5. `seat remove --yes` of a seat with a card in its `Implemented`: the card goes to another seat of the same type's `Implemented` list; with no such seat, to another work seat's; with no other work seat, to the top of `Backlog`. In the first two cases it is reviewed only by the other type.
6. Without a terminal and without `--yes`, a seat with cards is refused and nothing changes; declining the confirmation changes nothing.
7. `seat remove --graceful` of a seat implementing a card: the seat takes no new card, finishes the implementation, whose card goes to another seat's `Implemented` list, then exits and is removed. Ctrl-C while waiting leaves the removal requested and it completes anyway.
8. A seat being removed gracefully that is stopped by `kankan stop` is started again by `kankan start` and `kankan up` to finish, and is not counted in `kankan up` without arguments.
9. A card yielded by a reviewer whose implementer seat was removed goes to another seat's `Implemented`, not to an archived list.
10. With an autostart unit `claude=2` and two claude seats, removing one changes the unit to `claude=1`, and the stop record likewise; removing the Q&A seat drops `qa=`; a unit left without arguments is disabled.
11. `seat remove --graceful` refuses when the seat's launcher runs another kankan.
12. Removing the Q&A seat with an active conversation leaves the card in `Q&A`; a Q&A agent started later takes it and is not prompted until a new comment arrives, then answers it; when the removal cut an answer short, the new agent answers again.
13. Removing the seat that reviewed a card already integrated but still in its `Review` list sends the card to `Done`, not to `Backlog`; a card whose move to `Blocked` had not happened goes to `Blocked`.
14. A seat being removed gracefully that is stopped and is the only registered seat is started again by `kankan up` without arguments.
15. `seat remove --graceful` on a seat already being removed gracefully says that the removal is still running and changes nothing; `seat remove` without `--graceful` on it asks one confirmation: declined, the graceful removal goes on; confirmed (or `--yes`), the seat is removed at once as in test 3.
16. `kankan seat start claude-2` on a stopped seat starts its agent and returns once it occupies the seat; again while it runs, it reports "already running"; `kankan seat start claude-9` (unknown) is a `usage_error` and creates nothing; `kankan seat start codex-qa` starts the Q&A seat; a seat being removed gracefully is started and takes no new card.

## 6. Out of scope

- Cancelling a graceful removal: a removal can only be escalated to an immediate one ([§3.7](#37-repeated-request)).

## 7. Changes to other specifications

- **Initial §7.1 Creation and removal:** `kankan seat remove` is allowed in any state and gives the seat's cards to the board ([§3](#3-kankan-seat-remove-seat---graceful---yes)); a seat being removed is never chosen for a new agent ([initial §7.2](initial_spec_functional.md#72-seat-choice)).
- **Initial §9.1 Priority:** a seat being removed takes only its own work.
- **Initial §15.4 Administration commands:** `kankan seat stop <seat>`; `kankan seat remove` as in [§3](#3-kankan-seat-remove-seat---graceful---yes).
- **Initial §16.1 User commands:** `kankan stop <seat>` becomes `kankan seat stop <seat>`; new `kankan seat start <seat> [--attach]` ([§2.1](#21-kankan-seat-start-seat---attach)).
- **Initial §8.1 Process chain:** `kankan up` first restarts or completes seats being removed; the autostart unit is adjusted by removals ([§3.5](#35-autostart-and-stop-record)).
- **Initial §12.4 `block` and `resolve`:** `resolve --done` sends the card to an existing seat's `Implemented` when its implementer's seat was removed ([§3.1](#31-where-the-cards-go)).
- **Initial §14.1 Labels:** a removal clears the `Question`, `Error` and `Resume` labels the removed seat set.
- **Initial §14.6 Cards moved by the user:** a held card the user archived or deleted stays where it is when its seat is removed.
- **Stopall §1, §3, §5:** `kankan stop <seat>` is replaced by `kankan seat stop <seat>`; `kankan stop` never counts seats being removed in its record, and removals adjust that record; `kankan start` first restarts or completes seats being removed ([§3.3](#33-graceful-removal---graceful)).
- **Questions §3.1 Start and §3.7 Restarts:** a new conversation with nothing new since a complete latest answer waits without a prompt ([§4](#4-qa-card-taken-again-with-nothing-new)).
- **Questions §6 (its change to initial §7.1):** `kankan seat remove <type>-qa` no longer requires the seat to be free ([§3.4](#34-the-qa-seat)); §3.8 names `kankan seat stop` for the Q&A seat.
- **Initial §18.3 Acceptance tests:** add the tests of [§5](#5-acceptance-tests).
