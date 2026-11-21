# kankan destroy — functional specification

**Status:** implemented locally; live check on a disposable board passed 2026-11-21; final review fixes (2026-11-21) implemented locally, not yet re-run live.
**Date:** 2026-11-20
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3, [`boardsettings_spec_functional.md`](boardsettings_spec_functional.md), [`questions_spec_functional.md`](questions_spec_functional.md), [`cardinstructions_spec_functional.md`](cardinstructions_spec_functional.md) and [`stopall_spec_functional.md`](stopall_spec_functional.md); this feature changes them as listed in [§8](#8-changes-to-the-initial-specification).
**Companion:** [`destroy_spec_technical.md`](destroy_spec_technical.md) — how kankan implements this feature.

**Split rule.** This file describes what users observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

`kankan destroy` brings a project back to the state before `kankan init`, so the user can start the whole process again: it deletes every card of kankan's lists, archives the seat lists and removes kankan's local state from the clone. It is irreversible and therefore hard to run: the agents must already be stopped, and the user must type the board's name.

There is no reverse command. The report ends with the `kankan init <board-url>` command that starts again.

## 2. What is removed and what is kept

**On the board** (only when the board is this clone's: its `kankan-registry` block carries this clone's `registry-id`, or it has no block at all, which happens when an interrupted `destroy` already removed it; otherwise see [§5](#5-errors)):

| Item | Outcome |
|---|---|
| Cards of `Backlog`, `Done`, `Blocked`, `Q&A`, `kankan` and of every seat list, archived cards of those lists included | **deleted** (irreversible: they do not go to Trello's archive) |
| Seat lists (`<seat> · In Progress`, `· Implemented`, `· Review`) | emptied, then **archived**: the API cannot delete lists; seats may differ after the next `init` |
| Lists `Backlog`, `Done`, `Blocked`, `Q&A`, `kankan` | **kept**, empty; the next `init` offers to adopt them **by name**. The report lists each one with its current name; a list the user renamed is not found by the next `init`, which creates a new one unless the user renames it back first |
| Labels `HOLD`, `Question`, `Error` | **kept**; the next `init` adopts them by name |
| Dynamic labels named exactly `Resume YYYY-MM-DD hh:mm` | **deleted**, unless a card outside kankan's lists still carries one: that label is kept and reported. Labels that only start with `Resume` (e.g. `Resume hiring`) are not kankan's and are untouched |
| The `kankan-registry` block of the board description | **removed**; the rest of the description is kept |
| Lists, cards and labels kankan does not know (cards moved to other lists included) | untouched |

**In the clone and the user's account:**

| Item | Outcome |
|---|---|
| The project's seat worktrees (`<worktree root>/<seat>`, for every seat kankan knows) and `blocked-<shortLink>` worktrees | **removed**, ignored files included (with work at risk only after the user accepted it, [§3](#3-kankan-destroy---confirm-phrase)). Any other directory or git worktree under the worktree root, and the main checkout, are never touched |
| Card and seat refs (`refs/agent-*`) | **deleted** |
| kankan's project state (`<gcd>/kankan/`: configuration, seat state, audit log, downloaded attachments, block snapshots, Q&A records, board settings state, stop record) | **deleted**, except kankan's own lock files, which stay empty (a downloaded attachment named like a lock file, e.g. `yarn.lock`, is deleted) |
| kankan's `pre-push` hook | **removed** only when its content is exactly the one kankan installs **and** it is this repository's own: in `<gcd>/hooks`, or in a `core.hooksPath` set in the repository's own configuration that points inside the repository. Any other hook, a modified kankan hook included, is kept and reported; so is kankan's exact hook found through a global or system `core.hooksPath`, since other repositories share that directory |
| `git config kankan.integrationBranch` and the block `# kankan` + `/.kankan/` in `<gcd>/info/exclude` | **removed**; a `/.kankan/` line without kankan's `# kankan` line above it is the user's and is kept, as are all other lines |
| A custom worktree root (`worktree_root`) inside `<gcd>/kankan/`, equal to it or containing it | **nothing** is removed: `destroy` refuses ([§5](#5-errors)), since removing the state would remove what is kept under the root |
| Git branches (the integration branch, `YYkankan-<shortLink>` branches, user branches) | **kept**: they hold code. The next `init` reuses the integration branch as it is, local or adopted from `origin` ([§3](#3-kankan-destroy---confirm-phrase), step 7) |
| Credentials (`~/.config/kankan/credentials` or the `.properties` file), `~/.config/kankan/agents.json` (adapters, seeds) and `~/.cache/kankan/` (model checks, Codex model catalog) | **kept**: they are shared with other projects |
| The project's `agents.json` override (`<gcd>/kankan/agents.json`) | **deleted** with the project state |

## 3. `kankan destroy [--confirm <phrase>]…`

1. **Agents must be stopped.** If a seat of the project runs or has leftover processes (every seat kankan finds: in the configuration, in the seat refs and in the seat state files), a tmux session of the project is open, or the autostart unit is installed, `destroy` refuses (`needs_action`), lists what is running and says to run `kankan stop` first. It never stops agents itself.
2. **Inventory.** kankan reads the board and the local state and prints what it will delete, archive, remove and keep ([§2](#2-what-is-removed-and-what-is-kept)): the number of cards per list, the seat lists, the `Resume` labels, the registry block, each worktree, the refs, the hook, the git configuration and exclude entries, the project state directory, and the branches that stay.
3. **Work at risk.** Removing a worktree loses what no kept branch holds. For each worktree kankan checks:
   - uncommitted changes: modified, staged or untracked files not ignored;
   - an operation in progress: merge, rebase or cherry-pick;
   - checked-out submodules (git removes a worktree with submodules only by force, and their work is not held by a kept branch);
   - commits on a detached HEAD that no branch reaches (seat worktrees normally sit at a detached HEAD);
   - for a worktree whose directory is already gone but which git still registers: what its administrative directory under `<gcd>/worktrees/` still holds, namely an operation in progress, commits only its HEAD names, changes staged in its index that its HEAD does not hold, and submodule repositories (`modules/`, which can hold commits no branch reaches); when the index cannot be read, `destroy` refuses ([§5](#5-errors));

   and, outside the worktrees, the snapshot of a `kankan block` that was interrupted (its files exist only in kankan's state). For each finding it prints the path, the branch (or detached HEAD), the files or commits, and the commands to deal with it:
   - inspect: `git -C <path> status`, `git -C <path> diff`, `git -C <path> diff --staged`, `git -C <path> log --oneline <branch-or-upstream>..HEAD`;
   - revert: `git -C <path> merge --abort` / `rebase --abort` / `cherry-pick --abort` (only for the operation in progress), `git -C <path> restore --staged --worktree .`, `git -C <path> clean -fd`;
   - accept: `git -C <path> switch -c <name>` (only on a detached HEAD; the branch is kept by `destroy`), `git -C <path> add -A`, `git -C <path> commit`; for unreachable commits alone, `git -C <path> branch <name>`;
   - interrupted block: the snapshot's path and files, and `kankan agent <type> --seat <seat>`: the seat's launcher completes the block when it starts (it restores the work in the `blocked-<shortLink>` worktree, where the next `destroy` shows it as uncommitted work); then `kankan stop` and `kankan destroy` again.

   It then asks to type **`discard uncommitted work`** to destroy that work together with the worktrees, or to press Ctrl-C to abort, deal with it and run `kankan destroy` again. Without work at risk this step is skipped. **Ignored files** (build output, `node_modules`, `.env`…) inside the worktrees are deleted too, but do not trigger the question by themselves: the inventory prints their count per worktree with a few examples.
4. **Confirmation.** kankan prints the board URL without its name part (`https://trello.com/b/<shortLink>`, since the part after it is derived from the name) and asks to type **`destroy <board name>`**. It does not print the name: it explains that the name is the title shown at the top of the board in Trello, and that the name in the board URL can be an older one after a rename. The phrase is compared with the board's current name read from Trello, exactly (case included) after trimming spaces at both ends of the answer. When the board is not touched ([§5](#5-errors)) the phrase is **`remove local kankan state`**, which cannot be mistaken for a board phrase because it does not start with `destroy`.
5. **Any wrong phrase, an empty answer, Ctrl-C or end of input** at either question cancels: "nothing changed", success. kankan does not ask again.
6. **Destruction**, in this order, each step printed as it completes:
   1. the board: cards deleted (each card is read again just before its deletion: one a user moved out of kankan's lists meanwhile is left where it is and reported as skipped), seat lists archived, `Resume` labels deleted (the board is read again first: a label a card outside kankan's lists carries by then is kept and reported), registry block removed (the board is read again first: when a card arrived in kankan's lists during the run, the block and the local state are kept and `destroy` stops, `needs_action`, so the next run sees that card);
   2. the worktrees, then the refs;
   3. the hook, the git configuration entry and the exclude lines;
   4. the project state directory, **last**.
7. **Report.** What was deleted, archived and removed, what was kept on purpose ([§2](#2-what-is-removed-and-what-is-kept)), and the next step: `kankan init <board-url>`. When the integration branch exists locally or on `origin`, the report (and the inventory of step 2) says: "The integration branch `<name>` is kept (local, on `origin`, or both). The next `kankan init` will reuse it, with all the work merged so far." Whether it exists on `origin` is asked to `origin` itself, without credential helpers and without any prompt, so the question cannot store or erase credentials; when `origin` cannot be reached or asks for credentials it says "on `origin`: unknown". When the name is not the default `kankan-agents`, the report adds that the next `init` proposes `kankan-agents` and that `<name>` must be typed to reuse it. Unless the integration branch is also the user branch (then it says only that it is kept, being the user's branch too), it prints the two ways to start from a clean branch, without running them:
   - answer `init`'s integration branch question with a new name (e.g. `<name>-2`), which `init` creates from the user branch;
   - or rename the old one: `git branch -m <name> <name>-old` (only when it exists locally), `git push origin <name>-old` and `git push origin --delete <name>` (only when it exists on `origin`; for a branch only on `origin`, `git push origin origin/<name>:refs/heads/<name>-old` replaces the first push). The kankan hook that guards pushes to it is already removed.

**Without a terminal** (scripts, tests) both phrases must be given with `--confirm`, once each, in any order; a missing or wrong one is a `usage_error` and nothing changes. With a terminal, `--confirm` answers the matching question without asking it. There is no `--yes`. Each phrase must be quoted as one argument (`--confirm 'destroy My Board'`); arguments `destroy` cannot parse give a `usage_error` with a fixed message that never repeats them, since an unquoted phrase holds the board name.

## 4. Interruptions and repetition

Running `kankan destroy` again is always safe: it inventories only what is left, asks again for the phrases, and treats what is already gone as done, never as an error. Because the project configuration is deleted last, a run interrupted during destruction (Ctrl-C, network error, a crash) leaves the configuration in place, and the next run still knows the board and its lists.

A run interrupted after the registry block was removed finds the board without a block; since the board ID matches the configuration and no other clone has bound it, the board is still this clone's ([§2](#2-what-is-removed-and-what-is-kept)) and the run continues.

When nothing is left (no configuration, no worktree, no ref, no kankan hook, no kankan exclude block or git configuration entry, nothing in `<gcd>/kankan/` but kankan's lock files), `destroy` says "nothing to destroy" and succeeds without asking anything.

## 5. Errors

| Situation | Result |
|---|---|
| An agent, a project tmux session or the autostart unit is still active | `needs_action`, nothing changed; "run `kankan stop` first" |
| No terminal and a phrase missing from `--confirm`, or wrong | `usage_error`, nothing changed |
| Wrong phrase, Ctrl-C or end of input at a question | success, "nothing changed" |
| The board carries another clone's `registry-id` | the board is not touched (another clone owns it); kankan offers to remove only the local state, with the phrase `remove local kankan state`, and the report says the board was left alone because another clone uses it |
| No configuration (never initialized, or a previous run finished the local side), or one kankan cannot use (malformed, a field of the wrong type, a list ID that is not a string) | nothing on the board is known: only the local state is removed, with the phrase `remove local kankan state` |
| The board cannot be read: deleted board, no access, invalid credentials (401, 403, 404) | the board is not touched; only the local state is removed, with the phrase `remove local kankan state`. The report gives the reason and says that the board, if it still exists, must be cleaned by hand: kankan's cards, the seat lists, the `Resume` labels and the `kankan-registry` block in the board description |
| The board description holds a damaged `kankan-registry` block (start without end, more than one block, no `registry-id`, more than one `registry-id` line) | `needs_action`, nothing changed: the block must be fixed or removed by hand, since kankan cannot tell whose board it is |
| Trello returns two different rows for one archived card (another list or other labels) | `needs_action`, nothing changed, before any question: "the board changed while kankan destroy was reading it; run kankan destroy again" |
| An agent ref (`refs/agent-*`) is a symbolic ref | `needs_action`, nothing changed: it is listed with `git update-ref --no-deref -d <ref>`; kankan never writes refs that way, and deleting it could reach the branch it points to |
| The repository stores its refs in the reftable format (or another format than files) | `needs_action`, nothing changed: kankan's state must be cleaned by hand |
| `<gcd>/kankan` or `<gcd>/kankan/seats` is a symbolic link or not a directory, or a file the run opens (`project.lock`, a seat's lock file or process record, the audit log) is a symbolic link or not a regular file (the audit log also when hard-linked) | `needs_action`, nothing changed, before any question (and again under the project lock): kankan never follows it, the configuration behind a linked `<gcd>/kankan` is not even read; the message gives the `rm` command for the link itself, and the run is repeated |
| The worktree root and `<gcd>/kankan` overlap (one inside the other, or equal; a root holding the repository counts), or any git worktree lies inside `<gcd>/kankan` whatever the root | `needs_action`, nothing changed, before any question: move the worktrees and the user's files out of `<gcd>/kankan` first (`git worktree move`, `mv`), then set `worktree_root` elsewhere, or clean by hand |
| Between the inventory and the writes, a card was added or moved in kankan's lists, a seat list was reopened, or the board's name or binding changed; or locally the configuration changed (another binding, other paths or lists, a seat created, a configuration written by `init` during a local-only run), an agent ref was added, removed or changed, or an interrupted block appeared or changed (even when `discard uncommitted work` was typed: it covers only the work shown) | `needs_action`, nothing changed: "run `kankan destroy` again" (the configuration, the board and the local state are re-read under the project lock just before the first write) |
| During the board step, a card a user moved out of kankan's lists | not deleted, reported as skipped; the run continues |
| During the board step, a card arriving in kankan's lists | `needs_action` before the registry block is removed: the block and the local state are kept, and the next run deletes that card |
| 1000 or more archived cards on the board | `needs_action`, nothing changed: delete or restore archived cards in Trello and run again |
| Trello does not answer: timeout, rate limit, server error | `needs_action`, nothing changed: "try `kankan destroy` again later"; never a fall back to the local state only |
| A Trello call fails during destruction | the run stops before any local removal: `needs_action` listing what was done and what is left, and "run `kankan destroy` again" |
| A worktree cannot be removed (permission, a process holding it), or one that gained work at risk after the re-check under the lock (found by `destroy`'s own re-inspection right before removing it, or by git's final check) | the run stops before the refs: `needs_action` with the path and the error |
| A `pre-push` hook not written by kankan, or kankan's hook in a shared hooks directory | kept and reported; not an error |
| Arguments `destroy` cannot parse (e.g. `--confirm destroy My Board` without quotes) | `usage_error`, nothing changed, with a fixed message that does not repeat the arguments |

## 6. Example

```
$ kankan destroy
kankan destroy will:
Board https://trello.com/b/s5C4vWbG
  delete 37 cards (Backlog 12, Done 20, …), archived cards included
  archive 6 seat lists: claude-1 · In Progress, …
  remove the kankan-registry block of the board description (the rest is kept)
  …
Work at risk, lost with the worktrees:
Uncommitted work in /home/me/coding/.kankan/proj/claude-1 (detached HEAD): …
  inspect: …
Type 'discard uncommitted work' to destroy it, or Ctrl-C to abort: discard uncommitted work
Type 'destroy <board name>' (the title shown at the top of the board in Trello; the URL may show an older name): destroy test1
```

## 7. Acceptance tests

1. With a seat running, a project session open or the autostart unit installed, `destroy` refuses, names `kankan stop`, and nothing changes.
2. A wrong board name, the URL slug of a renamed board, an empty answer and Ctrl-C each cancel with nothing changed.
3. After `destroy` with the right phrase: kankan's lists hold no cards, open or archived; seat lists are archived; `Resume` labels are gone; `HOLD`, `Question`, `Error` and the common lists remain; the registry block is gone and the rest of the description is intact; lists and cards kankan does not know are untouched.
4. Locally: no seat or blocked worktree, no `refs/agent-*`, no `<gcd>/kankan/` content except kankan's lock files (a downloaded `yarn.lock` is gone), no kankan hook, git configuration entry or exclude block; the branches, a foreign or modified `pre-push` hook, the user's own `/.kankan/` exclude line, a worktree the user created under the worktree root, the credentials, `~/.config/kankan/agents.json` and `~/.cache/kankan/` remain.
5. A dirty worktree, a rebase in progress, an unreachable commit on a detached HEAD and an interrupted block snapshot each print their commands and need `discard uncommitted work`; declining changes nothing. Ignored files alone are counted but ask nothing.
6. A run interrupted after the board step, and one interrupted right after the registry block was removed, keep the configuration; the next run finishes, reporting the board items as already gone.
7. A board bound to another clone is left alone; only the local state is removed, after `remove local kankan state`. An unreadable board (401/404) gives the same, with the manual cleaning note; a timeout changes nothing.
8. A `Resume hiring` label and a `Resume` timestamp label used by a card outside kankan's lists survive. A renamed `Backlog` is reported with its current name.
9. With an integration branch equal to the user branch, no rename or delete command is printed.
10. Without a terminal, `destroy` refuses unless both needed phrases are given with `--confirm`.
11. `kankan init <board-url>` after `destroy` adopts the kept lists and labels and creates new setting cards and the `Instructions` card.
12. A worktree root inside `<gcd>/kankan/` with a directory of the user under it: `destroy` refuses before any question and the directory survives.
13. A worktree whose directory was deleted but whose index holds a staged file, or whose administrative directory holds submodule repositories, needs `discard uncommitted work`.
14. A seat created, a configuration written, a ref added or a block snapshot left while the questions are open stops the run with nothing changed; a card moved out of kankan's lists during the deletion survives; a kankan hook in a global `core.hooksPath` survives.

## 8. Changes to the initial specification

- **§5.1 Trello board:** `kankan destroy` deletes the cards of the workflow lists and of the `kankan` and `Q&A` lists, archives the seat lists and removes the registry block ([§2](#2-what-is-removed-and-what-is-kept)).
- **§6.1 `kankan init`:** after `destroy`, `init` finds the kept lists and labels by name, as for any existing list or label.
- **§11 Branches and worktrees:** "Branches are not deleted by kankan" still holds for `destroy`; it removes worktrees and refs only.
- **§15.4 Administration commands:** add `kankan destroy` ([§3](#3-kankan-destroy---confirm-phrase)).
- **§16.1 User commands:** add `kankan destroy [--confirm <phrase>]…`.
- **§18.3 Acceptance tests:** add the tests of [§7](#7-acceptance-tests).
- **[stop all §3](stopall_spec_functional.md#3-kankan-start):** `destroy` deletes the stop record; `kankan start` afterwards has no record and prints its help.
