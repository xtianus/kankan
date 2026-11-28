# kankan steering — decision record

**Status:** not planned. Steering of Q&A agents was rejected as not worth the effort; steering of implementation and review agents is set aside until a real need for it is found.
**Date:** 2026-11-25
**Context:** raised while designing the Q&A agent pool (`qapool_spec_*`).

## Idea

A comment that a board member writes on a card held by an agent would be sent to the agent at once, also while it is working, so that it could change the course of the running task ("steer" it). Today a comment reaches a working agent only when its turn ends: on a Q&A card it is the next follow-up ([questions §3.3](../questions_spec_functional.md#33-follow-ups)), on an implementation or review card it is forwarded only while the agent waits after `ask` or `fail` ([initial §14.3](../initial_spec_functional.md#143-answers-through-comments)).

## Decision

**Q&A cards: rejected.** Steering is not worth the effort in Q&A. An answer is usually fast enough that the user can wait for it and then "steer" with a plain new comment, which kankan already delivers as a follow-up. The user also does not see what the agent is doing while it answers, so there is little to correct before the answer arrives.

**Implementation and review cards: set aside.** Not pursued for now; it can be reconsidered when a real need is found.

## What the design work found

Kept here so that a later reconsideration does not start from scratch:

- **Agent support.** Claude Code delivers input typed while it works to the running session. Codex 0.172 supports steering an active turn, but kankan's recorded Codex screens (an older version) show typed input left unsent in the composer; a live check would be needed. Agents may process steering input at once or only after their current response.
- **Q&A turns.** A comment delivered mid-turn would have to join the current turn, keeping its turn token, so that the answer being written is not refused for an old token; and `answer`/`handoff` would have to refuse with `needs_action` while a comment delivered in the turn has not been read through `kankan task`, so an answer never covers a comment the agent did not see.
- **Task cards.** `kankan task --json` would need `new` flags on the comments of implementation and review tasks, and `done`/`integrate` the same unread-comment refusal; every held card's comments would be read at a short freshness, which is cheap only with a board-wide comment read.
