---
name: kankan
description: Carry out the current kankan task (implement or review a Trello card) with the kankan CLI. Use when the prompt says "Run the current kankan task", mentions a kankan task, or when a kankan suspension request or forwarded Trello answer arrives.
---

# kankan task

You run inside a kankan seat: a launcher assigned you one task on a Trello card. kankan decides **what** to do;
you decide **how**. Always talk to kankan through its CLI with `--json` and read the JSON envelope
`{"ok", "status", "message", "data"}`.

## 1. Read the task

Run `kankan task --json`. `data` contains:

| Field | Meaning |
|---|---|
| `task_id` | ID of this task; commands act only on it |
| `kind` | `impl` (implement), `review` (review and integrate) or `hold_wip` (commit WIP and yield) |
| `card_id`, `short`, `title`, `url`, `desc` | the Trello card: the description is the specification |
| `phase` | phase of the card's ref (`impl` or `review`) |
| `branch` | the branch prepared for you, checked out in your worktree |
| `worktree` | your working directory |
| `main` | the main branch name |
| `dirty_files` | uncommitted files left by a previous session: evaluate them before going on |
| `resumed` | true if the work was started by a previous session of this seat |
| `hold` | the user put `HOLD` on the card: commit WIP and `kankan yield --json` |
| `instructions` | one-line reminder of what to do |

## 2. Implementation (`kind: impl`)

Work on the prepared branch in your worktree, commit, run the project's tests, then `kankan done --json`.
`done` needs a clean worktree and at least one commit beyond `main`.

## 3. Review (`kind: review`)

1. Read the differences from main: `git diff <main>...HEAD` and `git log <main>..HEAD`.
2. Fix the problems you find with **separate commits** and summarize them with `kankan note "<summary>"`.
3. `kankan sync --json` merges the current main into the branch; resolve conflicts on the branch and commit.
4. Run the tests on the branch tip and note its hash (`git rev-parse HEAD`).
5. `kankan verify --tested <hash> --json` runs the project's test command (it may take long; run it like any
   long test command).
6. `kankan integrate --tested <hash> --json` merges into main. On `main_moved` repeat from step 3.
7. `kankan done --json`.

## 4. Progress

Publish useful progress with `kankan note "<text>"` (it becomes a card comment).

## 5. When you cannot go on

- `kankan ask "<question>"` before stopping for a question to the user; then wait: the answer arrives in this
  terminal.
- `kankan fail "<reason>"` for an error that needs the user.
- `kankan block "<reason>"` for an obstacle that prevents completing the card (for example a merge conflict you
  cannot resolve safely: leave the merge in progress, kankan moves it to a dedicated worktree).

## 6. Suspension

On a kankan suspension request, on `hold: true` or on status `on_hold`: make a WIP commit of your work and run
`kankan yield --json`.

## 7. Never

- Never move cards on Trello and never change their labels yourself.
- Never read, create, change or push `refs/agent-*` refs (`refs/agent-cards/*`, `refs/agent-seats/*`).
- Never check out `main` or push it; `integrate` does the merge.

## 8. Statuses

Always use `--json`. Exit codes and what to do:

| Exit | `status` | What to do |
|---|---|---|
| 0 | `success` | go on |
| 10 | `no_work` | no task: stop |
| 11 | `already_claimed` | stop |
| 12 | `ineligible` | the card is no longer yours: stop |
| 13 | `main_moved` | repeat `sync`, tests, `verify`, `integrate` |
| 14 | `needs_action` | fix what the message says, then retry |
| 15 | `on_hold` | WIP commit, then `kankan yield --json` |
| 20 | `network_error` | wait a minute and retry |
| 21 | `trello_error` | retry once; if it persists, `kankan fail` |
| 30 | `needs_reconciliation` | **stop** and report the message |
| 31 | `not_owner` | **stop**: this session no longer owns the seat |
| 40 | `config_error` | **stop** and report |
| 41 | `usage_error` | fix the command |
| 50 | `internal_error` | **stop** and report |
