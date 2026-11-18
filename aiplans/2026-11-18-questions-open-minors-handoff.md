# Q&A seat: open minor findings handoff

2026-11-18

The Q&A feature is on `feature/questions` (last commit `fecc728`, 859 tests passing). Six minor review findings were left open. After checking them against the current code: three need a small decision, two turn out to be already handled, and one is a refactor to schedule. None blocks a merge.

| # | Finding | Still open? | Choice needed | Recommendation |
| --- | --- | --- | --- | --- |
| 1 | `take_over` does not check the seat is still held | Yes | Add the check, or accept the window | Add it (one line and one test) |
| 2 | `complete_pending` can leave an empty conversation directory | Yes, self-healing | Clean up at once, or rely on `reconcile --fix` | Clean up at once (one line) |
| 3 | Turn context stores every comment ID, the plan says the newest only | Yes, harmless | Trim the code, or correct the plan's wording | Correct the plan's wording |
| 4 | `end_admin` runs several transactions in one lock hold | No, already one per hold | None | Close it |
| 5 | Launcher tests wait with fixed sleeps (about 20 s) | Yes | Keep, or wait on poll cycles | Wait on poll cycles, in a separate change |
| 6 | `qa_launcher.py` is 764 lines | Yes | Leave, split by mixins, or extract a pure turn planner | Extract the planner when the file is next changed |

The last section lists what else was left out and the rules any follow-up must keep.

## Resolution (2026-11-18)

All items except 6 were closed in one follow-up commit on `feature/questions`. The analysis below is kept as written.

| # | Outcome |
| --- | --- |
| 1 | Fixed (A): `qa.take_over` calls `check_seat` before the worktree reset and the seat task. |
| 2 | Fixed (A): `complete_pending` calls `qa.drop_orphan` when the lock finds no pending operation. The test wraps `qa.conversation_lock`, so no runtime fault point was needed. |
| 3 | Fixed in the code (A), not the plan: `qa.first_turn` keeps only the newest context comment ID, matching the plan. Coverage is unchanged, because it already took the newest. |
| 4 | Closed, already fixed. |
| 5 | Fixed (B): `wait_card_polls` and `wait_board_polls` count comment and board reads in the fake Trello request log. Two sleeps stay, in states that make no Trello poll: the `limited` state and an ended conversation. |
| 6 | Deferred: extract the turn planner when `qa_launcher.py` is next changed. |

Older review notes:

- **`_finish` seat check:** fixed. `check_seat` runs before the signal whenever no transaction verified the seat (an unchanged `covered`, or a handoff transition done earlier).
- **Fault point after the signal:** added `qa_after_signal`, with a test that recovery posts no second answer.
- **Idle gate during the version poll:** accepted and recorded in `aiknowledge/notes.md`.
- **`OSError` in `record_remove`:** the claim stands. The error is logged as `qa_record_remove_failed`.
- **Docs on `qa.turn` and `qa.warning`:** no document mentions them, so closed.
- **`seat remove` on a dead Q&A seat:** test added. The removal is refused until `seat recover` runs.

## 1. `take_over` does not check the seat

**What happens.** When a launcher restarts on a seat that still owns a conversation, `qa.take_over` (`kankan/qa.py:449`) checks that the card's ref is still this seat's. It does not check that this launcher still holds the seat (`qa.check_seat`). It may then reset the seat's worktree (`worktrees.reset_detached`) and write a new task into the seat state. The next step, `start_turn`, does check the seat, but only after the reset.

**When it matters.** Only if the launcher loses the seat between taking it and taking over the conversation. `seat recover` refuses a seat that is alive, so in practice this needs another kankan process to misbehave. The cost is a worktree reset under a launcher that is no longer the owner.

**Choice.**

- **A. Add the check** before the reset and the store. One line, plus a test that replaces the seat ref before `take_over` and expects `not_owner` with the worktree untouched.
- **B. Accept the window**, and record it in `aiknowledge/notes.md` next to the other accepted windows.

**Recommendation: A.** The fix is tiny and makes `take_over` consistent with `claim`, `start_turn` and `end`, which all check the seat.

## 2. `complete_pending` can leave an empty conversation directory

**What happens.** `qa_answer.complete_pending` (`kankan/qa_answer.py`) checks that `pending.json` exists, then takes the conversation lock. If the conversation ends in between, the end deletes the directory and taking the lock creates it again, holding only `op.lock`. The function then finds no pending operation and returns without removing the directory. The other paths that take this lock call `qa.drop_orphan` for this case.

**When it matters.** Never for behaviour: an empty directory is ignored. `reconcile --fix` already deletes conversation directories that no ref names (`diag._fix_conversation_dirs`). The cost is a stray directory until then.

**Choice.**

- **A. Call `qa.drop_orphan`** on the empty path, as the other lock holders do. One line, plus a test that ends the conversation between the existence check and the lock.
- **B. Rely on `reconcile --fix`** and close the finding.

**Recommendation: A**, for consistency. The test needs a fault hook between the check and the lock, which is most of the work.

The related review note "`start_turn`/`end` race leaves an orphan directory" is already fixed: `start_turn` checks the ref before the lock and drops the orphan inside it.

## 3. Turn context stores every comment ID

**What happens.** When a card is taken up again, the old thread is "context": it is shown to the agent but never delivered as new. The plan (`aiplans/2026-11-17-questions.md`, `turn.json` description) says `context` holds "the newest candidate comment ID and all attachment IDs". The code saves every candidate comment ID (`qa.start_turn`, `kankan/qa.py:495`).

**When it matters.** It does not change behaviour. Coverage keeps only the newest comment ID (`qa.coverage`, used by `qa.effective_covered`), so the extra IDs are ignored. They make `turn.json` slightly larger on cards with long threads. The technical spec (T§5) does not describe the field's contents, so only the plan disagrees with the code.

**Choice.**

- **A. Trim the code** to save only the newest comment ID. Needs care: `Inputs.from_dict` is shared, and the tests for re-taken cards assert on the context.
- **B. Correct the plan's wording** to say all candidate comment IDs, of which coverage uses the newest.

**Recommendation: B.** The plan is a record of intent, and storing all IDs is harmless. If the plan is treated as frozen history, close the finding with a line in `aiknowledge/notes.md` instead.

## 4. `end_admin` lock holds: already fixed

The review said `qa.end_admin` (`seat remove` on a free Q&A seat) ran several transactions in one project-lock hold. The current code takes one hold per conversation the seat owns. Each hold reads that ref, runs one transaction, deletes its local files and marks the seat task ended. That is within the bound the plan sets (one card read, one transaction).

**Choice:** none. Close the finding.

## 5. Launcher tests wait with fixed sleeps

**What happens.** Eight tests in `tests/test_qa_launcher.py` check that something does *not* happen (no second prompt, no new comment, no restart) by sleeping a fixed 1 to 3 seconds and then asserting. The sleeps add about 18 s to the run. The tests set `qa_poll_seconds=1`.

**When it matters.** A fixed sleep proves nothing happened in that time, not that nothing would happen. On a slow machine a wrong delivery could arrive after the sleep and the test would still pass. They do not fail spuriously, so they cost speed and strength, not stability.

**Choice.**

- **A. Keep them.** They are clear and have not been flaky.
- **B. Wait on poll cycles.** Wait until the launcher has polled the card two more times, then assert. The fake Trello server already records every request (`tests/fakes/fake_trello.py`, `requests`), so a helper can count card reads for the Q&A seat without changing runtime code.
- **C. Add a launcher event per poll** (for example `qa_polled` in the seat log) and wait on that. More direct, but it adds a log line every second to every real run.

**Recommendation: B**, as its own small change. It makes the "nothing happened" checks meaningful and usually faster. The one sleep in `tests/test_qa.py:571` (0.3 s in the lock-race test) is a thread hand-off and should stay.

## 6. `qa_launcher.py` is 764 lines

**What happens.** `kankan/qa_launcher.py` holds one class, `QaLauncher`, with about 40 methods. They cover start and restart, turns and signals, delivery and follow-ups, the card poll, ending conversations and the closing `/clear`. The other Q&A modules are 806 lines (`qa.py`) and 403 lines (`qa_answer.py`). The project has no file-size rule.

**When it matters.** For maintenance only. The methods share a lot of launcher state (`task`, `turn`, `delivered`, `ended`, `state`), which is why the file grew as one class.

**Choice.**

- **A. Leave it.**
- **B. Split by mixins**, for example delivery and poll in one module and restart and ending in another. This is mechanical, but the state stays shared, so the parts stay coupled.
- **C. Extract a pure "turn planner"**: the functions that decide what to deliver and with which wording (uncovered items, follow-ups, restart wording, attachment names). They take data and return data, so they can be tested without tmux.

**Recommendation: C, next time the file changes.** Do not do it as a standalone refactor: the launcher tests are slow tmux tests, and a move-only change gains little without a reason to touch the code.

## Also left out, and the rules for a follow-up

**Corrupted `qa.json`.** `qa.recorded()` crashes if `qa.json` is not a JSON object with a `cards` list. It was left as is on purpose: no other kankan state file guards against corruption, and every write is atomic. If it happens, Q&A commands fail with a crash message until the file is fixed or deleted. Deleting it is safe apart from noise: `reconcile` may then flag finished Q&A cards in `Done` as done without a merge.

**Older review notes not re-checked.** The per-task reviews also noted the items below. They were not checked against the current code, and some may have been fixed by later rounds:

- `_finish` does not check the seat ref on its paths without a transaction.
- There is no fault point between `write_signal` and `_record_completed`.
- The launcher's idle gate waits while the periodic version poll runs.
- An `OSError` in `record_remove` fails a claim that has already succeeded.
- The docs describe the `qa.turn` and `qa.warning` events.
- No test covers `seat remove` on a dead Q&A seat.

**Still outstanding for the feature.** One live run with real agents: a Q&A dialogue with a follow-up attachment, then a handoff implemented by another seat. It needs real agents and someone commenting on Trello.

**Rules any follow-up must keep** (from `AGENTS.md` and earlier instructions):

- Update the spec in `aispecs/questions_spec_*.md` before code that changes specified behaviour.
- Use the Python standard library only, with 4-space indentation, and put user-facing text in `messages.py`.
- Revise `aispecs/kanban_requirements.md` in any commit that changes a Trello endpoint call. None of the items above does.
- Make one commit at the end of the work, push it to `feature/questions`, and do not open a pull request.
- Run the full suite before committing: `setsid -w .venv/bin/python -m pytest -q -n auto`. `tests/test_launcher_settings.py::test_stop_is_handled_during_long_probe` is a known flaky test.
