# Board settings implementation review

**Date:** 2026-11-10. **Branch:** `feature/board-settings`.
**Scope:** [implementation plan](2026-11-10-boardsettings.md), its feature specifications, all changed files and new runtime modules.

The requested `xtian-codex-advisor` skill ran GPT-6 Astra with medium reasoning in a separate ephemeral read-only process. File hashes before and after confirmed no mutations. It reported seven Important findings and one Minor finding, with no Critical finding. The author treated checklist duplication in the fake as Important because it invalidated snapshot-based settings evidence. All eight findings were addressed in one fix pass, with failing regressions before the fixes.

| Finding | Resolution and evidence |
|---|---|
| Interrupted restart cleanup could release a seat with detached descendants | Shutdown uses the same agent-tree cleanup invariant as restart, records survivors and retains the seat ref. Real-process detached-child stop and injected-survivor tests cover both outcomes. |
| Older CLI/catalog completion could overwrite newer observations | Version and catalog cache writes carry observation ordering and reject superseded writes. Barrier-controlled subprocess tests finish v2 before v1 and retain v2. Malformed ordering metadata recovers. |
| Discarded reads and late human labels could change Error ownership | Only trustworthy, non-stale reads update observed labels. Dispatch checks fresh labels before establishing ownership and journals the first write. Regression tests preserve human Error through failed and passing probes and through structural correction. |
| Nested state corruption could crash consumers | Role maps, probe causes and construction intents are validated before consumption; malformed state falls back to seeds and is rebuilt. Parameterized corruption regressions pass. |
| Ref identity disappeared at activation | Activated tasks retain their pinned ref OID. A changed ref with the same owner/phase cancels an unsent prompt. |
| TMPDIR inside a checkout leaked project context | Probe work/home directories use explicitly validated external roots and sanitize repository/temp environment variables. A fresh subprocess with TMPDIR inside a checkout runs outside it. |
| Timeout/cancel performed an unbounded wait | Child termination is polled with a bounded cleanup deadline. Retired Popen objects retain exit-status ownership until reapable. Controlled unreapable-child tests cover versions, probes and catalogs, on timeout and cancellation. |
| Fake snapshot duplicated checklists | Fake board reads return checklists at board level, matching Trello. Normalization also deduplicates by checklist ID. A regression asserts exactly one checklist per adapter. |

Additional process acceptance tests establish fallback to the other role's verified pair and preservation of a shared detached service through restart while it serves another seat. Existing owned-child/adopted-orphan tests protect nonzero model-check exit statuses.

## Cloud ultrareview (second pass)

A cloud ultrareview of `feature/board-settings` against `main` saw only tracked changes, so its first finding (imports of the new modules) was a false positive: the six runtime modules are untracked and must be added with the implementation commit. The other nine findings were fixed, with regressions in `tests/test_boardsettings_robustness.py`:

| Finding | Resolution |
|---|---|
| Per-step config reload and settings poll raised out of `step()`, killing a running agent | `Launcher.refresh_settings` catches `KankanError`, logs once, keeps the old configuration and stops after the task on `CONFIG_ERROR`; malformed `config.json` is a `CONFIG_ERROR`. |
| `activate_task` ran outside tick error handling (direct and restart paths) | `Launcher.activate` keeps `prepared` and retries transient errors at `poll_seconds`, exits normally on `NOT_OWNER`, stops on `CONFIG_ERROR`. |
| A catalog change dropping a seeded pair made the Codex adapter unloadable | Catalog-derived models move such seeds to `stale_seeds` (missing seed; the question replaces it). Explicit `models` overrides stay strict. Specs and user manual updated. |
| Stop between claim and prompt stranded the card | `workflow.release_unsent` yields a fresh, clean, unchanged claim; other work stays held. T§3 Restart step 6 updated. |
| `unavailable` entries with no start time allowed signalling a reused pid | Writers drop such entries; readers never treat them as alive. |
| Unpaced pre-prompt recheck | Failed rechecks retry at `poll_seconds`. |
| Declined `kankan` list adopted after a failed create | `create_list(exclude=...)` never returns a list that existed before the call. |
| Stale running selection in status | Cleared at shutdown; status shows it only for live seats. |
| Per-second I/O | Lock-free poll pre-check, memoized cached-result recording, `waitid(WNOWAIT)` gate before the `/proc` scan. |

Also hardened during the soundness pass: `failed_start` records survivors before its Trello calls and tolerates their failure; `agent_ready` tolerates a failed startup-label clear. `test_e2e.py::test_card_goes_from_backlog_to_done` failed 1 in 4 runs because Codex sometimes claimed the implementation card scripted for Claude (both seats became idle in the same second; that model-check gating causes this is likely but unverified). It starts Claude first and waits for its claim.

A follow-up advisor review found that the stale-seed fix missed a missing catalog cache (shipped list standing in) and a stale seed in the project's `agents.json` (the answer now replaces it in that file), and that a model check that cannot be spawned was cached as a rejection (it is now unverified with a retry deadline). All three have regressions. The requested `xtian-codex-advisor` second review did not run: the provider's usage limit was reached, and it produced no findings.

## Verification

- Final local suite: 538 passed (`setsid -w .venv/bin/python -m pytest -q -n auto`) after the ultrareview fix pass above. `test_stop_is_handled_during_long_probe` failed once under parallel load (5-second wait) and passed in every other run.
- Live Trello: three marker-gated tests passed, including checklist creation/read/movement/deletion, comment deletion, and list archive/unarchive. Only the dedicated test board was used.
- Codex 0.167.0: reached ready and exited with status 0 on `/exit`, without a model request. A subreaper adopted `codex app-server --listen unix:// --analytics-default-enabled --managed-daemon`; the shipped keep pattern matched. The disposable daemon/server were removed.
- Python 3.10 grammar: all runtime modules parse. A Python 3.10 interpreter is unavailable, so execution with it is unverified.
- Release observation gates: Claude's real exit/confirmation behavior and UI-originated Trello tick/untick activity. This environment has no Chrome executable. Previously documented isolation measurements support the shipped probe flags; no additional billable provider probes were submitted.

The advisor declined to certify independently executed tests, live-provider behavior or production readiness. Test outcomes are established by the author's command results; the outstanding observations remain release gates. Its missing sibling AGENTS-CORE.md was resolved in the parent session by reading the global instructions from the global `AGENTS-CORE.md`.

## Execution decisions

Work used the existing clean feature branch instead of a linked worktree (less workspace isolation). Non-executable workflow helpers were replaced by equivalent manual ledger/test records (manual bookkeeping). Dependency verification for Tasks 2–4 and the launcher seam for Tasks 8–9 were grouped (coarser intermediate regression gates); the whole suite passed before review, and runs again after the fix pass. The Codex subreaper observation was performed at final validation; it confirmed the recorded keep pattern. No functional requirement was deliberately deferred, and no minor review finding remains deferred.
