# kankan board reader — functional specification

**Status:** specified, not implemented.
**Date:** 2026-11-25
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3, [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md) and [`questions_spec_functional.md`](questions_spec_functional.md); this feature changes the initial one as listed in [§7](#7-changes-to-the-initial-specification); the reading of setting cards and of Q&A follow-ups changes only in how kankan implements it ([technical §13](boardreader_spec_technical.md#13-changes-to-other-technical-specifications)).
**Companion:** [`boardreader_spec_technical.md`](boardreader_spec_technical.md) — how kankan implements this feature.
**Related documents:** [`kanban_requirements.md`](kanban_requirements.md) (revised when the feature is implemented, [technical §9](boardreader_spec_technical.md#9-trello-usage)); the Q&A agent pool (`qapool_spec_*`, to be specified) builds on this feature.

**Split rule.** This file describes what users and agents observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

Today every kankan process reads the board on its own: each agent's launcher reads the whole board at every poll, the Q&A seat reads its card and its comments every 30 seconds, the setting cards are read every 30 seconds, and every command reads what it needs. With several agents the same data is requested several times, and the requests grow with the number of agents.

The feature gives kankan a single way of reading the board, the **board reader**, shared by every kankan process of the project on the machine. It keeps what it read together with the time it was read. Every read states how old the data it needs may be: when the kept copy is recent enough it is used, otherwise Trello is asked and the answer is kept for the others. Data read for one agent serves all the others.

Nothing the user does changes and commands print what they print today. The visible effects are fewer Trello requests ([§4](#4-trello-requests)), the meaning of the three poll settings ([§3](#3-poll-settings)) and a cache in the project's state directory ([§6](#6-local-state)).

## 2. Freshness

- **Every read states its freshness.** Agents' launchers, while they wait for work or watch a card, accept data as old as their poll setting ([§3](#3-poll-settings)).
- **Commands and decisions read live.** Every command the user or an agent runs (`status`, `reconcile`, `doctor`, `task`, `answer`, `done`, …) asks for live data, and so does kankan before every decision it takes under the project lock (claiming a card, checking a move, convergence). These see Trello as it is, as today.
- **Live means after the request.** A live read is always answered by a Trello read sent after the read was asked for. It is never answered by a read that started earlier, and never by a copy older than kankan's own last change to that data: after kankan moves a card, comments, sets a label or changes a title, no reader is ever given the state before that change as fresh.
- **One request for many pollers.** When several launchers poll for the same data at the same time, Trello is asked once and every waiting launcher gets that answer, as long as it is fresh enough for each of them. A live read never waits for another process's request: it asks Trello itself, so a decision under the project lock is never delayed by another agent's poll.
- **Errors.** When Trello cannot be reached, a reader that needs fresher data than the kept copy gets the error, as today; kankan never acts on data older than it asked for. Launchers retry at their next poll, as today.

## 3. Poll settings

`poll_seconds` (default 120), `qa_poll_seconds` (default 30) and `settings_poll_seconds` (default 30) keep their names and defaults. They now mean **how old the data an agent acts on may be**: each launcher still wakes at that interval, but it reaches Trello only when no other kankan process has read the same data recently enough.

The setting bounds the age of the data, not the delay of a reaction. A poll can use a read another process made just before the user's change, so the change is seen at the following poll: a reaction takes at most about twice the setting (one minute for a Q&A comment, four minutes for a work card), against at most once the setting today. Most changes are still seen within one interval, because the shared reads come more often than each launcher's own.

A smaller value still means faster reactions. It costs Trello requests only when no other agent reads the same data more often: while the Q&A agent watches the board every 30 seconds, the work agents' 120-second board checks are answered from its reads and never reach Trello.

## 4. Trello requests

The requests kankan makes for data its agents share no longer grow with the number of agents: the board is read at most once per interval of the most demanding agent, however many agents run. The new comments of every card are found by one board-wide read ([§5](#5-comments)), so watching comments costs the same for one card or many. Commands still make their live reads, as today.

## 5. Comments

kankan finds the new comments of all cards with one request for the whole board, instead of one request per watched card. Answers to `ask` ([initial §14.3](initial_spec_functional.md#143-answers-through-comments)) and Q&A follow-ups ([questions §3.3](questions_spec_functional.md#33-follow-ups)) are delivered as today, within at most about twice the poll setting of the agent concerned ([§3](#3-poll-settings)). A card with no recent comment costs no request of its own once kankan has caught up with it.

Trello does not report edits or deletions of comments in that read: an edited or deleted comment is seen when the card's whole thread is read, and every `kankan task` reads it live, so an agent always gets the current thread. An edited comment never counts as a new comment, as today.

## 6. Local state

The cache is kept in the project's state directory (`<gcd>/kankan/board-cache/`), which is private to the clone and never committed, like the downloaded attachments. It holds copies of board data: card titles, descriptions, labels, attachment names and comments. Deleting it is harmless: kankan reads Trello again. `kankan destroy` removes it with the rest of the local state.

## 7. Changes to the initial specification

How this feature changes the initial behaviour described in [`initial_spec_functional.md`](initial_spec_functional.md), by initial section number. The initial file is not edited.

**§2 Scope and assumptions.** Add:

- kankan reads the board through one shared reader per project and machine, which keeps the data it read and the time of the read; each read states how old its data may be ([board reader §2](#2-freshness)).

**§14.3 Answers through comments.** Add:

> The new comments are found by one board-wide read shared by every agent ([board reader §5](#5-comments)); the launcher in `waiting_human` accepts them as old as `poll_seconds`.

**§15.2 Situations and behaviour.** Add:

| Trello unreachable while a launcher polls | The poll fails as today and is retried at the next one; no decision is taken on data older than the poll setting. |
| A user's change made just after another process read the board | Seen at the launcher's following poll: at most about twice the poll setting after the change ([board reader §3](#3-poll-settings)). |
| Board cache deleted or unreadable | kankan reads Trello again; nothing else changes. |

**§18.3 Acceptance tests.** Add the tests of [§8](#8-acceptance-tests).

## 8. Acceptance tests

1. Two work agents and a Q&A agent idle on a board with no work: the board is read from Trello about once per `qa_poll_seconds`, not once per agent per interval; the work agents still take a card added to `Backlog` within twice `poll_seconds`.
2. A user comment answering an `ask` and a Q&A follow-up on another card are both found by one board-wide comment read and delivered as today.
3. A card moved by the user is seen by a command (`status`, `task`, a claim) at once, even when a launcher read the board a moment before the move.
4. After kankan moves a card, no launcher reading the board with a 120-second freshness is given the card's former list.
5. Two launchers polling the board at the same moment make one Trello request; a claim under the project lock while another launcher's board read is slow does not wait for it.
6. With Trello unreachable, a launcher's poll fails and is retried; with the cache deleted while agents run, they keep working.
7. An edited comment is shown edited by the next `kankan task` and is not delivered as a new comment.
8. `kankan destroy` leaves no cache behind.
9. `kankan doctor` reports revoked credentials as invalid, even after they were valid and cached.
10. A card's attachments are given to the agent in the card's order, also when they were reordered after the upload and the card was read from the cache.
11. Ten quiet Q&A cards and a quiet `waiting_human` card cost no request of their own per poll once the comment log covers them, also a day after their last comment.

## 9. Out of scope

- Sharing the cache between machines or between clones of the same project.
- Trello webhooks ([`kanban_requirements.md`](kanban_requirements.md) G9), which would replace polling but need an endpoint Trello can reach.
- Serving older data when Trello is unreachable.
- Caching the content of attachment files, which the attachment download already keeps ([initial §13.3](initial_spec_functional.md#133-card-attachments)).
