# kankan takeover cleanup — functional specification

**Status:** implemented locally ([plan](../aiplans/2026-11-20-takeover.md)).
**Date:** 2026-11-20
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3 and [`stopall_spec_functional.md`](stopall_spec_functional.md); this feature changes them as listed in [§5](#5-changes-to-the-initial-specification).
**Companion:** [`takeover_spec_technical.md`](takeover_spec_technical.md) — how kankan implements this feature.

**Split rule.** This file describes what users observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

`kankan init --takeover` binds a board to a new clone ([initial §6.3](initial_spec_functional.md#63-boardregistry-binding)). Everything the previous clone's agents kept locally stays behind: card claims (`refs/agent-*`), implementation branches, `blocked-…` worktrees and, unless it was pushed, the integration branch. Cards the old agents held are then left in seat lists or in `Blocked` with no claim, and no agent ever takes them again.

This feature (1) warns before a takeover about what would be stranded ([§2](#2-takeover-check)) and (2) lets `kankan reconcile --fix` give stranded cards back to `Backlog` ([§3](#3-stranded-cards)). Card contents (description, comments, attachments) are never lost; the work done on a stranded card is, unless it is recovered from the old clone.

## 2. Takeover check

Before `kankan init --takeover` binds the board, it checks:

- **Cards in seat lists:** open cards in any list named like a seat list (`<type>-<n> · In Progress`, `· Implemented`, `· Review`). Their work is in the other clone.
- **Cards in `Blocked`:** their unfinished merge is a worktree of the other clone.
- **Integration branch not on `origin`:** the clone has no `origin` remote, or `origin` has no branch named like the integration branch (the one of this clone's previous binding, else `kankan-agents`). The cards the other clone integrated would then be missing from this clone's integration branch.

When any check finds something, `init` lists it with what to do (finish the cards with `kankan stop --graceful` in the other clone; push the other clone's integration branch to `origin`) and asks `Take over anyway? [y/N]`. Declining, or no terminal to answer, stops `init` with `needs_action` and the board unchanged. When nothing is found, the takeover proceeds without asking. After a takeover that went ahead with stranded cards, `init` prints a warning recommending `kankan reconcile --fix`.

## 3. Stranded cards

A **stranded card** is an open card with no card ref in this clone that is in:
- a list of a registered work seat, or a list named like a seat list, when that seat is not alive; or
- the `Blocked` list, when this clone has no `blocked-<shortLink>` worktree for it.

`kankan reconcile` reports it (`card_without_ref`, as today for seat lists, which it also reports while the seat is alive; `blocked_without_ref` for `Blocked`). `kankan reconcile --fix` moves it to the **top** of `Backlog`, where it keeps its labels, attachments and dependencies, and comments: "kankan: this card was in *list* without a claim in this clone (its work was in another clone, or lost); moved back to Backlog to be implemented again." It is then implemented from scratch on a new branch. A stranded card on `HOLD` is moved too and stays on `HOLD`.

`kankan doctor` reports stranded cards in its reconcile check and names `kankan reconcile --fix` as the step that returns them to `Backlog`.

## 4. Acceptance tests

1. `init --takeover` on a board with a card in `claude-1 · Implemented` lists it and asks; declining leaves the registry block unchanged.
2. `init --takeover` with nothing stranded and the integration branch on `origin` asks nothing.
3. `init --takeover` without `origin/kankan-agents` warns about the integrated work.
4. `reconcile` reports a card in a seat-named list of an unregistered seat, and a card in `Blocked` without a ref or worktree; `reconcile --fix` moves both to the top of `Backlog` with the comment.
5. `reconcile --fix` leaves alone a card in the list of an alive seat, and a `Blocked` card whose `blocked-` worktree exists.

## 5. Changes to the initial specification

- **§6.3 Board↔registry binding:** `--takeover` runs the check of [§2](#2-takeover-check) first.
- **§15.3 `kankan reconcile [--fix]`:** reports `blocked_without_ref`; `card_without_ref` also covers seat-named lists of unregistered seats; `--fix` moves stranded cards to the top of `Backlog` ([§3](#3-stranded-cards)).
