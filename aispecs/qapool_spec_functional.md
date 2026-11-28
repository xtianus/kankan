# kankan Q&A agent pool — functional specification

**Status:** implemented locally (2026-11-27), on top of the board reader ([`boardreader_spec_functional.md`](boardreader_spec_functional.md)); outstanding: the live agents run of [technical §12](qapool_spec_technical.md#12-tests).
**Date:** 2026-11-25
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3, [`questions_spec_functional.md`](questions_spec_functional.md), [`seat_spec_functional.md`](seat_spec_functional.md), [`stopall_spec_functional.md`](stopall_spec_functional.md), [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md) and [`boardreader_spec_functional.md`](boardreader_spec_functional.md); this feature changes them as listed in [§9](#9-changes-to-the-initial-specification) and [§10](#10-changes-to-other-feature-specifications).
**Companion:** [`qapool_spec_technical.md`](qapool_spec_technical.md) — how kankan implements this feature.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (revised when the feature is implemented, [technical §10](qapool_spec_technical.md#10-trello-usage)); [`proposals/steering.md`](proposals/steering.md) (why comments are not sent to an agent in the middle of an answer); the user manual's Q&A section, rewritten when the feature is implemented to explain the pool and the title prefixes ([technical §12](qapool_spec_technical.md#12-tests)).

**Split rule.** This file describes what users and agents observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

Today one Q&A agent answers one card at a time: the other cards of `Q&A` wait until the user moves the active card out of the list, even when the agent has nothing to do. The feature replaces the single Q&A seat with a **pool** of Q&A agents of one type, opened and closed by kankan, so every card with something to answer gets an agent at once and nothing has to be moved out of `Q&A` for the next card to be answered.

The pool is driven by the agents' **prompt cache**. An agent that has just answered a card holds the card's conversation in a cache that makes the next turn cheaper and faster; the cache expires some time after the agent's last request. kankan keeps a card with the agent that answered it while that agent's cache is warm, opens another agent rather than spend a warm cache on a different card, and closes agents whose cache went cold when another agent can do their work.

## 2. Prompt cache

An agent's cache is **warm** until its expiry time, then **cold**. kankan knows the expiry in one of two ways, chosen per agent type by the adapter configuration ([initial §8.7](initial_spec_technical.md#87-adapters)):
- **exact:** the agent reports it. Claude Code reports its cache's expiry to its status line; kankan starts Claude agents with a status line of its own that records the expiry and then shows the user's own status line, so the pane looks as before.
- **estimated:** the expiry is the end of the agent's last turn plus a lifetime configured for the type (30 minutes for Codex).

A new agent type chooses `exact` (only with a reporting mechanism kankan knows) or `estimated` in its adapter. An `exact` agent that has not reported an expiry (for example an older Claude Code) is treated as `estimated`, with the type's configured lifetime (60 minutes for Claude).

An agent whose process restarts (a crash, a model or effort change, a usage limit cleared by a restart) starts cold. An agent whose card stopped being a Q&A conversation ([§6](#6-end-of-a-conversation)) is **available** even while its cache is warm: that cache is of no use to any other card.

## 3. The pool

- **One type.** The pool runs agents of the type given by `qa=<type>` (`kankan up qa=claude`, `kankan autostart enable … qa=codex`) or `kankan agent <type> --qa`. There is no limit on the number of agents.
- **Seats.** The first agent has the seat `<type>-qa` (`claude-qa`): it is the one kankan starts, it **routes** the cards of `Q&A` to the agents of the pool ([§4](#4-routing)) and it is never closed by kankan. The agents kankan opens get the seats `<type>-qa2`, `<type>-qa3`, …, the lowest free number first. Each agent has its own worktree, created when the agent opens and deleted when it closes.
- **Limits and settings.** The pool's agents do not count towards `seat_cap`. The `Q&A Agents` setting card gives the model and effort of every agent of the pool, each applying a change between its turns as today ([board settings §3](boardsettings_spec_functional.md#3-model-and-effort)); an agent restarted for a change starts cold.
- **What an agent does.** Each agent works exactly as the Q&A seat does today on one card at a time ([questions §3](questions_spec_functional.md#3-conversation)): answers through `kankan answer`, follow-ups delivered after its turn, `handoff`, `fail`, the commit rule, its own checkout reset for every new card.

## 4. Routing

### 4.1 A comment on a card whose agent is warm

The agent that answered the card gets the comment (or attachment) as a follow-up in the same session, as today: at once when it waits for the user, after its current answer when it is answering ([questions §3.3](questions_spec_functional.md#33-follow-ups)). Nothing changes for the other agents.

### 4.2 A card that needs an agent

A card of `Q&A` without `HOLD` needs an agent when it has something not yet answered and no agent holds its conversation, or the agent holding it is cold: a new card (its description is the first question), or a comment or attachment by a board member newer than the card's latest complete answer, including what arrived while every agent was stopped. "Newer than the latest answer" is decided as today when a conversation is taken up again ([questions §3.7](questions_spec_functional.md#37-restarts)): a comment posted while an answer was being written, followed by a restart of the agent, counts as already seen (accepted limitation). A comment or attachment deleted before an agent took it up needs no answer: an agent given the card finds nothing new and is not prompted ([seat §4](seat_spec_functional.md#4-qa-card-taken-again-with-nothing-new)).

kankan gives the card to an agent in this order:
1. the cold agent that still holds the card's conversation, which gets the new input as a follow-up of its session;
2. otherwise the **available** agent with the lowest seat number, cold or with an ended card ([§2](#2-prompt-cache)): it clears its session and starts a new one on the card, reading the whole comment thread ([questions §3.7](questions_spec_functional.md#37-restarts));
3. otherwise a new agent, opened for the card;
4. when opening an agent fails (a failed model check, a tmux error), the idle warm agent whose cache expires soonest, giving up that cache; when every agent is answering, the card waits and the opening is tried again at the next routing round.

While an agent of the pool is stopped by a usage limit, kankan opens no new agent: another agent of the same account would meet the same limit; the card waits for a free agent.

When several cards need an agent at the same time, the card whose oldest unanswered input is oldest goes first; the order of the `Q&A` list does not matter.

Routing happens every `qa_poll_seconds` (default 30) of data freshness ([board reader §3](boardreader_spec_functional.md#3-poll-settings)).

### 4.3 Closing agents

kankan closes an agent (removes its seat and deletes its worktree) in three cases, and never `<type>-qa`, nor an agent that is answering, waiting for a usage limit or starting:
- **after a search:** in a routing round where at least one card needed an agent, once the cards are given out, every other idle cold agent of the pool is closed; in a round where no card needed an agent, idle cold agents stay open, at no cost;
- **when its card's conversation ends** ([§6](#6-end-of-a-conversation)) by a move, an archive, a deletion or `HOLD`: the agent is closed at the next routing round, unless that round gives it another card (an agent with an ended card is available, [§4.2](#42-a-card-that-needs-an-agent) rule 2): giving out cards comes first, closing applies to the agents still without a card;
- **when it is gone for good:** its pane was closed, or its launcher keeps failing and its supervisor gave up restarting it. It is closed at the next routing round, an answer it had cut short is completed first, and its card needs an agent again when it has something not yet answered.

## 5. What the user sees

**Expiry note.** The last part of every answer ends with the time the agent's cache expires: `Prompt cache expires at 14:32 (Europe/Rome).` for an `exact` expiry, `Prompt cache may expire at 14:32 (Europe/Rome).` for an `estimated` one, in the machine's local time and time zone. A comment made before that time is answered by the same agent in the same session. A `handoff` summary carries no note: the card leaves `Q&A`.

**Title prefix.** kankan writes a prefix before the title of the Q&A cards an agent holds: `🟢 ` while that agent's cache is warm (also while it answers), `⚪ ` once the cache is presumed expired, the agent took another card or closed, or the pool stopped. A card no agent took yet has no prefix. The prefix is removed when the card leaves `Q&A` (handoff, move, archive) and from every Q&A card when the pool starts. The prefix characters are configurable; there may be several, written together, with one space before the title.

kankan remembers the prefix it wrote on each card and removes or replaces only that one: if the user edited the title so that it no longer starts with it, kankan leaves the title as the user wrote it, and writes a new prefix only at the card's next change of state. Wherever kankan uses a card's title (the task given to an agent, the merge commit message of a handed-off card, `kankan status`, its comments), it leaves out the prefix it wrote.

Changing a title has a cost the user accepts with this feature: Trello cannot make a write conditional, so if the user edits a title in the same moment kankan changes its prefix, one of the two changes can overwrite the other ([§9](#9-changes-to-the-initial-specification), initial §14.5). Each change also appears in the card's activity. Renaming a card does not notify anyone.

**Status.** `kankan status` lists the agents of the pool with their card, their state and their cache (warm until hh:mm, or cold), and the Q&A cards waiting for an agent.

## 6. End of a conversation

A conversation ends, as today, when the user moves the card out of `Q&A`, archives or deletes it, or puts `HOLD` on it ([questions §3.6](questions_spec_functional.md#36-end-of-a-conversation)); its agent becomes available and is closed at the next routing round unless that round gives it another card, or it is `<type>-qa` ([§4.3](#43-closing-agents)). A conversation also ends when its agent is given another card or closes: the card stays in `Q&A` with its whole thread, and its next comment makes it need an agent again ([§4.2](#42-a-card-that-needs-an-agent)). Moving a card out of `Q&A` is therefore no longer needed to have the next card answered.

## 7. Stop, start and seat commands

- **Stop.** `kankan stop` and `kankan seat stop <type>-qa` stop every agent of the pool: gracefully (each finishes its current answer) or at once, as for any seat ([stopall §1](stopall_spec_functional.md#1-scope)). The agents other than `<type>-qa` are removed when they stop; an answer one of them was posting when stopped at once is completed as it is removed (its remaining parts posted, nothing repeated), or, when Trello cannot be reached then, by the next start, the agent staying registered until then.
- **Start.** `kankan start`, autostart, `kankan up qa=<type>`, `kankan seat start <type>-qa` and a reboot start only `<type>-qa`. Every Q&A card is then idle: an answer cut short by an immediate stop is completed first (its remaining parts posted, nothing repeated), a question whose agent was stopped before it started answering is answered again, the title prefixes are removed, and a card gets an agent only when it has input newer than its latest complete answer ([§4.2](#42-a-card-that-needs-an-agent)). An agent never answers the same input twice because of a stop and start.
- **Routing seat restart.** When only the launcher of `<type>-qa` restarts (a crash recovered by its supervisor), the other agents keep their cards and routing continues; `<type>-qa`'s own agent starts cold. A restart during a stop does not undo it: the pool stays stopped until a start command.
- **Changing type.** `kankan up qa=codex` (also when the autostart unit runs it) and `kankan agent codex --qa`, with a Claude pool running, stop every Claude Q&A agent at once, without asking, remove their seats and start `codex-qa`, with every card idle as after a start: an answer being posted at that moment is completed as the old agent is removed (nothing repeated), and a question whose old agent had not started its answer is answered by the new pool. When an old agent cannot be removed (its answer cannot be completed because Trello cannot be reached), the command stops with `needs_action` before starting `codex-qa`, the pool staying stopped; running it again resumes.
- **Seat commands.** `kankan seat remove <type>-qa` removes the whole pool. The other seats of the pool (`<type>-qa2`, …) are kankan's: `seat start`, `seat stop` and `seat remove` refuse them with `usage_error` naming `<type>-qa`.

## 8. Acceptance tests

1. A new card in `Q&A` while `claude-qa` waits on another card with a warm cache: `claude-qa2` opens and answers the new card; both cards get `🟢 `.
2. A follow-up on the first card is answered by `claude-qa` in the same session; a follow-up arriving while it answers is delivered after its answer.
3. A new card while `claude-qa` is cold: no agent opens; `claude-qa` clears its session and answers the new card; the old card's prefix becomes `⚪ `.
4. A comment on a card whose agent is cold: that agent answers it in its own session, as a follow-up.
5. A comment on a card whose agent was closed, with `claude-qa2` and `claude-qa3` cold and idle: `claude-qa2` takes the card with a new session over the whole thread, and `claude-qa3` is closed (seat removed, worktree deleted).
6. Two cards needing an agent at once, one agent available: the card with the older unanswered input is taken first, the other gets a new agent.
7. Every answer ends with the expiry note: the exact wording for a Claude agent whose expiry reading was recorded before it ran `kankan answer`, "may expire" for Codex; a Claude agent that reported no expiry gets the "may expire" wording.
8. The prefix: none before the first answer; `🟢 ` while warm; `⚪ ` after the expiry time; removed when the card goes to `Done`, after a handoff, and on all Q&A cards when the pool starts; a title edited by the user to drop the prefix is not touched until the card's next change of state; `kankan task` and the merge commit message of a handed-off card show the title without the prefix.
9. Moving the card of `claude-qa2` to `Done` closes `claude-qa2` at the next routing round; moving the card of `claude-qa` to `Done` leaves `claude-qa` running; an idle cold `claude-qa2` stays open while no card needs an agent.
10. Opening an agent fails: the card goes to the idle warm agent whose cache expires soonest; with every agent answering, the card waits and the opening is retried.
11. A usage limit on one agent: no agent opens while it lasts.
12. `kankan stop` with three agents, one answering: every agent stops, the answer is completed; `kankan start` starts only `claude-qa`, removes the prefixes, and a card with a comment written while stopped is answered once.
13. An immediate `kankan stop` during a long answer, once of `claude-qa` and once of `claude-qa2`: the answer is completed without repeating its posted parts (for `claude-qa2` as it is removed, or with Trello unreachable then, at `kankan start`).
14. `kankan up qa=codex` with a Claude pool running, `claude-qa2` posting a long answer: the pool stops at once, the answer is completed without repeating its posted parts, `codex-qa` starts and every card is idle; with Trello unreachable the command stops with `needs_action` before starting `codex-qa`, and a rerun completes it. The same with `kankan agent codex --qa`.
15. `kankan seat stop claude-qa2` and `kankan seat remove claude-qa2` give `usage_error`; `kankan seat remove claude-qa` removes the whole pool.
16. The launcher of `claude-qa` killed and restarted by its supervisor: the other agents keep their cards and keep answering their follow-ups.
17. A change of the `Q&A Agents` setting restarts each agent between its turns; a restarted agent is cold and can be given another card.
18. An immediate `kankan stop` while `claude-qa` or `claude-qa2` is thinking, before it posted anything: after `kankan start` the question is answered once.
19. A comment on a card while every agent is answering and no agent can open, for two routing rounds, then a restart of `claude-qa`: the comment is answered once an agent is free, without a second comment.
20. A follow-up on a card whose cold agent has not polled yet, while another card needs an agent: the follow-up goes to that card's agent and the other card to another agent.
21. `kankan stop` while `claude-qa` is opening a new agent: after the stop no agent of the pool runs, and only `claude-qa` is still registered.
22. The pane of `claude-qa2` closed while it waits for the user on a card: `claude-qa2` is closed at the next routing round and a new comment on the card is answered by another agent; while its supervisor only restarts its launcher, `claude-qa2` keeps the card.
23. The launcher of `claude-qa` killed during `kankan stop` and restarted by its supervisor: the pool stays stopped, no agent opens, and `kankan start` starts it again.
24. A comment deleted before any agent took its card is not answered. A comment arriving while the card's agent answers an earlier one, followed by a failed turn and a restart of that agent, is answered once.
25. The card of `claude-qa2` moved to `Done` while another card needs an agent: `claude-qa2` takes that card in the same routing round instead of being closed.

## 9. Changes to the initial specification

How this feature changes the initial behaviour described in [`initial_spec_functional.md`](initial_spec_functional.md), by initial section number. The initial file is not edited; where the questions feature already replaced the same text, the replacement below includes its additions.

**§4 Glossary.** Replace the row of **Q&A seat** added by the questions feature with:

| **Q&A pool** | The Q&A agents of one type: `<type>-qa`, which routes the `Q&A` cards, and the agents kankan opens and closes, `<type>-qa2`, `<type>-qa3`, … ([§3](#3-the-pool)). |

**§7.1 Creation and removal.** Add:

> kankan creates and removes the seats `<type>-qa2`, `<type>-qa3`, … of the Q&A pool by itself ([§3](#3-the-pool), [§4.3](#43-closing-agents)); the user's seat commands refuse them.

**§9.1 Priority.** Replace the rule added by the questions feature with:

> The Q&A pool follows its own rule ([§4](#4-routing)): a card goes to the agent that holds its warm conversation, then to an available agent, then to a new agent; among cards needing an agent the oldest unanswered input goes first. The other seats never take `Q&A` cards.

**§9.2 Eligibility.** Replace the **Q&A** item added by the questions feature with:

- **Q&A:** card in `Q&A`, without `HOLD`, with no ref or with a ref in phase `parked` or `qa`, with input newer than its latest complete answer, or never answered ([§4.2](#42-a-card-that-needs-an-agent)); dependencies are not considered.

**§14.4 What the user does.** Replace the item added by the questions feature with:

- create cards in `Q&A` or drag cards into it to ask questions, comment on any of them at any time, and end a conversation by moving, archiving or deleting its card ([§6](#6-end-of-a-conversation));

**§14.5 Precedence of human changes.** Add:

> kankan writes one field the user also edits: the prefix of the title of a Q&A card ([§5](#5-what-the-user-sees)). It changes only the prefix it wrote and never a title that no longer starts with it, but a title edit made in the same moment as a prefix change can be overwritten, or can overwrite the prefix change. This is accepted for that prefix only.

**§16.1 User commands.** Replace the `kankan status` row as changed by the questions feature so that its last part reads “the Q&A pool: each agent with its card, state and prompt cache, and the Q&A cards waiting for an agent”.

**§18.3 Acceptance tests.** Add the tests of [§8](#8-acceptance-tests).

## 10. Changes to other feature specifications

**Questions** ([`questions_spec_functional.md`](questions_spec_functional.md)):
- **§1 Scope.** “one conversation at a time” becomes “one conversation per agent of the Q&A pool, as many at a time as cards need answering” ([§1](#1-scope)).
- **§2 Q&A list and Q&A seat.** The list is no longer a queue: every card in it can be answered at any time ([§4](#4-routing)). The Q&A seat becomes the Q&A pool ([§3](#3-the-pool)); replacing it by another type is [§7](#7-stop-start-and-seat-commands)'s type change, and the new agents no longer take over the old active conversation.
- **§3.1 Start.** Replaced by [§4.2](#42-a-card-that-needs-an-agent): there is no active card of the list, only the card of each agent.
- **§3.2 Answer.** The last part of the answer ends with the expiry note ([§5](#5-what-the-user-sees)).
- **§3.6 End of a conversation.** Add the ends of [§6](#6-end-of-a-conversation): the agent took another card, or closed.
- **§3.7 Restarts.** A restarted agent keeps its card only when the routing seat alone restarted ([§7](#7-stop-start-and-seat-commands)); after a stop and start every card is idle.
- **§3.8 Stop, usage limits and suspension.** `kankan stop` stops the whole pool ([§7](#7-stop-start-and-seat-commands)); during a usage limit no agent is opened ([§4.2](#42-a-card-that-needs-an-agent)).
- **§8 Acceptance tests.** Test 9 (“cards below the active one wait”) and test 13 (the new Q&A seat takes over the active conversation) are replaced by tests 1, 6 and 14 of [§8](#8-acceptance-tests).
- **§9 Out of scope.** “Several Q&A seats or several simultaneous conversations” is removed.

**Seat commands** ([`seat_spec_functional.md`](seat_spec_functional.md)): `kankan seat start`, `seat stop` and `seat remove` on `<type>-qa` act on the whole pool, and refuse the other seats of the pool ([§7](#7-stop-start-and-seat-commands)); §4 (a card taken again with nothing new is not prompted) applies to every agent of the pool.

**Stop and start all** ([`stopall_spec_functional.md`](stopall_spec_functional.md)): `kankan stop` stops the whole pool and `kankan start` restores only `<type>-qa` ([§7](#7-stop-start-and-seat-commands)).

**Board settings** ([`boardsettings_spec_functional.md`](boardsettings_spec_functional.md)) §3: the `Q&A Agents` card applies to every agent of the pool.

## 11. Out of scope

- Sending comments to an agent in the middle of an answer ([`proposals/steering.md`](proposals/steering.md)).
- A pool mixing agent types, and a limit on the number of agents.
- Resuming an agent's earlier session of a card after it was given another card: a card whose agent moved on starts a new session over its thread.
- Keeping a cache warm on purpose (sending requests only to refresh it).
