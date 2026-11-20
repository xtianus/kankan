# kankan takeover cleanup — technical specification

**Status:** implemented locally ([plan](../aiplans/2026-11-20-takeover.md)).
**Date:** 2026-11-20
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3; this feature changes it as listed in [§5](#5-changes-to-the-initial-technical-specification).
**Companion:** [`takeover_spec_functional.md`](takeover_spec_functional.md) — the behaviour this file implements; read it first.

**Split rule.** The functional file describes what users observe; this file describes how kankan implements it. Section numbers are this feature's own.

## 1. Scope

`kankan/init.py` (takeover check) and `kankan/diag.py` (reconcile). Trello calls used: the board snapshot, `move_card` and `add_comment`, all already in [`kanban_requirements.md`](kanban_requirements.md), which is unchanged.

## 2. Seat-named lists

`SEAT_LIST_RE = ^([a-z]+-[0-9]+) · (In Progress|Implemented|Review)$` on the list name (the `list_name` format of `seats.py`). `diag.stranded_lists(cfg, snap)` maps the ID of every open list that is a registered work seat's list or matches the pattern to its seat name.

## 3. Takeover check

In `_bind_registry`, when the block names another registry and `takeover` is set, before writing: `takeover_problems(paths, cfg_old, snapshot)` returns text lines for open cards in seat-named lists, open cards in `Blocked` (`cfg_old.blocked` when known, else the open lists named `Blocked`) and a missing integration branch (`git ls-remote --exit-code --heads origin <branch>` with `GIT_TERMINAL_PROMPT=0` and a 30 s timeout, branch = `cfg_old.integration_branch` or `kankan-agents`; no `origin` remote counts as missing; any other failure or the timeout gives a "could not check" line). Lines present → `say` them, then `prompt("Take over anyway? [y/N] ")`; anything but `y`/`yes`, or `EOFError`, raises `KankanError(NEEDS_ACTION)` before any write. When stranded cards were accepted, `init` appends a warning naming `kankan reconcile --fix`.

## 4. Reconcile

- **Report:** `card_without_ref` for open cards without a ref in `stranded_lists`, whatever the seat's liveness (as before); `blocked_without_ref` for open cards without a ref in `cfg.blocked` whose shortLink has no `blocked-<short>` worktree.
- **Fix** (`_fix_stranded`), under the project lock: re-read the card refs (`list_raw`: a malformed ref counts as present), the liveness of each seat (`seats._liveness`, the lock being held) and the board snapshot; skip a card that now has a ref or whose seat is `alive`; `trello.move_card(card, cfg.backlog, "top")`, then `add_comment` with op id `stranded-<card id>-<list id>`. Each fixed card is an `Issue("stranded_card_moved", card_id, …, fixed=True)`; a Trello error is logged and the card is left for the next run.
- **Doctor:** unchanged mechanics; its reconcile check message names `kankan reconcile --fix` when a `card_without_ref` or `blocked_without_ref` issue is present.

## 5. Changes to the initial technical specification

- **§6 Setup:** the takeover check of [§3](#3-takeover-check).
- **§15 Errors and recovery:** stranded cards are a certain case of `reconcile --fix` ([§4](#4-reconcile)).
