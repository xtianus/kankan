# kankan unexpected agent dialogs and logged-out agents — technical specification

**Status:** specified, not implemented.
**Date:** 2026-11-26
**Base:** [`initial_spec_technical.md`](initial_spec_technical.md) v1.3, [`questions_spec_technical.md`](questions_spec_technical.md) and [`alerts_spec_technical.md`](alerts_spec_technical.md); this feature changes them as listed in [§12](#12-changes-to-the-initial-technical-specification).
**Companion:** [`dialogs_spec_functional.md`](dialogs_spec_functional.md) — the behaviour this file implements; read it first.

**Split rule.** The functional file describes what users observe; this file describes how kankan implements it. Section numbers are this feature's own.

## 1. Scope and current gap

Modules: `kankan/launcher.py`, `kankan/qa_launcher.py`, `kankan/adapters.py` with `adapters_default.json`, `kankan/forward.py`, `kankan/labels.py`, `kankan/messages.py`, `kankan/config.py`, `kankan/seatstate.py`, a new `kankan/login.py`, the `status` command, and the fake agent of the tests. Trello calls used: label add/remove, `add_comment` and the comment reads of the board reader, all already in [`kanban_requirements.md`](kanban_requirements.md), which is unchanged.

**Gap.** `Launcher.check_inactivity` is called only from `working()`. In `idle` with `pending_writes` (new-conversation command sent, prompt pending) or with `prepared` (claimed card, nothing sent), `flush_assignment` waits for `can_write` forever and no timer runs; `unknown_since` is reset by `assign()` and never set. In the live run of 2026-11-26 the audit log of `codex-1` ends with `pane_write "/new"` while the seat state is `launcher_state: idle` with a task, and `classify()` returns `unknown` on the saved pane.

## 2. Screen watch

A new method `Launcher.watch_screen(cls)` runs in `step()` on every pass after `answer_dialogs`, for every state except `starting` (startup keeps `start_deadline`), in both `Launcher` and `QaLauncher`:

- `cls.state == "unknown"`: `unknown_since = unknown_since or now`; also store `unknown_text = adapters.tail(screen, 15)` and its fingerprint `sha1(unknown_text)` at the start of the episode and whenever the fingerprint changes.
- any other state: if an episode is open, close it ([§4](#4-recovery)); `unknown_since = None`.
- `now - unknown_since >= cfg.screen_blocked_minutes * 60` and the episode is not reported: report it ([§3](#3-report)).

`check_inactivity` keeps only the ready-without-signal branch; its unknown branch is removed. The QA launcher's equivalent check is changed the same way.

**Holding a card** means `self.task` or `self.prepared` or `self.pending_writes` is set (for the QA launcher, a conversation card is held). Without a card, the episode follows [§5](#5-seat-without-a-card).

**Last write.** `paste()` and `tmux.send_keys` calls made by the launcher record `last_write = (monotonic, text)`. At the start of an episode, a `last_write` less than 60 s old is kept as `episode.suspect_write` and logged as `write_unconfirmed`.

## 3. Report

With a card: `labels.set_label(project, card, cfg.label_error, "screen", seat, comment=messages.SCREEN_BLOCKED.format(...), op_id=f"screen-{task_id}-{episode}")`, where `episode` is a counter kept in the seat state so a restarted launcher does not post twice. The comment holds the excerpt in a fenced block (at most 15 lines and 1500 characters, from `adapters.clean`, so colour codes and URLs are already removed; backticks in the excerpt are replaced by `'` so the fence cannot be closed), the suspect write when present, the tmux session name and the `keys:` syntax. The comment's action ID is saved as `episode.comment_id` and its date as `episode.comment_date`.

Seat state (`seatstate`), new optional field `screen_blocked = {episode, since (UTC ISO), card, fingerprint, excerpt, comment_id, comment_date, error_set, user_cleared}`, written when the episode is reported and cleared when it closes. `kankan status` reads it.

Audit events: `screen_unrecognized` (episode start: seat, card, excerpt), `write_unconfirmed`, `screen_blocked` (reported: seat, card, minutes), `screen_recovered`, `screen_keys_sent`, `screen_keys_refused` (reason `changed` or `invalid`).

**User removed the `Error`.** On each periodic board check while an episode is reported, if the card no longer carries `label_error` and `labels.label_cause` says the cause was `screen`, set `user_cleared`; the label is not set again in this episode.

## 4. Recovery

When a recognized classification closes a reported episode: if the card still carries `Error` with cause `screen`, `labels.clear_label(project, card, cfg.label_error, "screen", seat)` and comment `messages.SCREEN_RECOVERED` (op id `screen-ok-{task_id}-{episode}`); log `screen_recovered`; clear `screen_blocked`. The launcher state was never changed by the episode, so `flush_assignment`, `working()` and the other handlers simply continue on the next pass. An unreported episode closes silently except for the audit entry.

## 5. Seat without a card

The episode is logged (`screen_unrecognized`, `screen_blocked` with `card: null`) and saved in `screen_blocked`; no label. `idle()` claims no card while `cls.state == "unknown"` (the tick is skipped, `next_tick` unchanged), so no card moves to the seat's list behind a dialog. With the alerts feature, the launcher posts A7 through the alerts module (same alert key: seat + fingerprint) when the episode is reported.

## 6. Answering from the board

While an episode with a card is reported and not `user_cleared`, the launcher polls the card's comments at `qa_poll_seconds` pace through the board reader (comments after `episode.comment_date`, freshness as for `forward.poll`), skipping comments by kankan's own member. A comment whose first line matches `^\s*keys:\s*(.+)$` (case-insensitive) is parsed into tokens split on spaces:

- valid tokens: one printable non-space character (sent literally with `send-keys -l`), or one of `Enter Esc Tab Space Up Down Left Right BSpace` (case-insensitive, sent as tmux key names); at most 10;
- an invalid token or more than 10: reply `SCREEN_KEYS_INVALID`, log `screen_keys_refused reason=invalid`;
- before sending, capture the pane again: if it is not `unknown` or its fingerprint differs from `episode.fingerprint`, reply `SCREEN_KEYS_CHANGED` with the new excerpt (when still unknown) and log `screen_keys_refused reason=changed`;
- otherwise `tmux.send_keys` with the tokens, ignoring the quiet rule of `quiet()` (the user asked for these keys), reply `SCREEN_KEYS_SENT`, log `screen_keys_sent`.

Each comment is used once: its action ID is stored in `screen_blocked.keys_done` (list) before sending, so a crash between storing and sending loses the keys rather than sending them twice (the user can reply again). Replies use op ids `screen-keys-{action_id}`. `Esc` is accepted only on this path; `paste()` and `answer_dialogs()` keep refusing interrupt keys.

## 7. Settle before the first write

`Launcher` keeps `settle_until = None` after every agent (re)start; `can_write` returns False until the screen has been `ready` on consecutive passes covering `cfg.ready_settle_seconds` (it tracks `ready_since`, reset on any non-ready classification). This applies to the first write after `starting` → other state; later writes use the existing rule. The startup transition to `idle` itself is unchanged, so claiming a card is not delayed, only its first write.

## 8. Known dialog

`adapters_default.json`, Codex `known`, appended:

- `name`: `model_retirement`; `patterns`: `retires on`, `use existing model`; `all`: true; `answer`: `["2"]`.

The answer key is confirmed on the real pane (Codex v0.172.0, live run of 2026-11-26): the key `2` alone selects and confirms "Use existing model", as `3` does in `model_switch`; Codex then showed its prompt and the launcher's pending prompt went through unchanged. `answer_dialogs` logs `dialog_answered` with a new field `excerpt` (`adapters.tail(screen, 15)`) for every known dialog, so the retirement date stays in the audit log. A saved copy of the pane is the fixture of the classification test.

## 9. Configuration and messages

`config.py`, project configuration: `screen_blocked_minutes: int = 3` and `ready_settle_seconds: int = 10`, both positive integers, validated like `poll_seconds`.

`messages.py`: `SCREEN_BLOCKED`, `SCREEN_RECOVERED`, `SCREEN_KEYS_SENT`, `SCREEN_KEYS_CHANGED`, `SCREEN_KEYS_INVALID` with the texts of [functional §3–§4](dialogs_spec_functional.md#3-seat-blocked-by-an-unrecognized-screen). `UNKNOWN_SCREEN` is replaced by `SCREEN_BLOCKED`. `MODEL_START_FAILED` gets the screen excerpt when startup ends on an unknown screen.

## 10. Tests

- **Fake agent:** a scenario step `dialog_after(seconds, text, answers)` that shows a full-screen dialog after a delay (also after the first write) and returns to ready on one of `answers`.
- **Integration (real tmux, fake Trello):** acceptance tests 1–9 and 11 of [functional §12](dialogs_spec_functional.md#12-acceptance-tests), with `screen_blocked_minutes` and `ready_settle_seconds` lowered through the configuration.
- **Unit:** `keys:` parsing; excerpt cleaning and fence safety; `classify()` of the saved retirement pane gives `known/model_retirement`.
- **Live agents run** (`tests/live/agents_e2e.py`): no new step; a blocked seat is visible in the runner's card lines as `Error`, and diagnostics already save pane captures.

## 11. Logged-out agent

**Adapter.** New pattern group `logged_out: List[str]` (validated like the other groups). `classify()` returns `Classification("logged_out", pattern)` with the precedence known dialog, trust, limit, working, **logged out**, ready, unknown: a working screen is never logged out. Default patterns, to be confirmed on captured screens (below):
- Claude: `please run /login`, `oauth token (has )?expired`, `invalid api key`, `not logged in`; none of them may match the advance warning `⚠ Your login expires in 1 day · run /login to renew` (a unit test checks the default patterns against it);
- Codex: `sign in with chatgpt`, `please (sign|log) in again`, `you.?ve been signed out`, `(access|refresh) token.{0,30}(expired|revoked|invalid)`.

`kankan adapter check` shows the new state. **Fixtures:** the login screens are captured by starting each CLI with an empty configuration home (Codex `CODEX_HOME`, Claude `CLAUDE_CONFIG_DIR`, both temporary directories); the expired-token messages, which cannot be forced, come from the CLIs' documented messages and are confirmed when first seen (the audit entry keeps the screen, below). Findings go to [`docs/adapter-notes.md`](../docs/adapter-notes.md).

**Advance warning.** New adapter group `login_expiring: List[str]`, default for Claude `your login expires in (?P<left>[^·\n]+?)\s*·\s*run /login to renew` (the text Claude Code prints, confirmed by the user on 2026-11-26), empty for Codex until observed. It is matched separately from `classify()`, on every screen read, and never changes the classification. A match writes `expiring = {left, seen_at, host}` for the type into `agent-login.json` (cleared after an hour without a match, or when the type is logged out). Once per type and UTC day (`expiring.notified_day`, set under the lock before posting, so two seats do not both post): with the alerts module, A9 with key `(login-expiring, type)`; otherwise, when the seat holds a card, `add_comment` with `messages.AGENT_LOGIN_EXPIRING` and op id `login-expiring-{type}-{day}`, no label; without a card, only the audit entry `agent_login_expiring` (logged in every case). `kankan status` prints the warning per seat of the type.

**Confirmation.** On a `logged_out` classification the launcher asks `login.confirm(project, type)`, which runs the adapter's `login_check` (timeout `doctor.LOGIN_TIMEOUT`, 15 s) without blocking the launcher's loop (a worker thread, like the version and catalog preparations), and returns `out`, `in` or `unknown` (timeout, no `login_check`). `in`: log `login_pattern_unconfirmed` with the excerpt, treat the screen as `unknown` for this pass (the rules of [§2](#2-screen-watch) apply). `out` or `unknown`: the type is logged out.

**Record.** `<gcd>/kankan/agent-login.json`, written under `agent-login.lock`: per type `{since, host, last_check, last_check_result, checker_pid, recovered_at, resend_blocked, alert_posted}`. A type is logged out while it has `since`. Writing `since` when it is absent starts an episode (log `agent_logged_out` with seat, card, excerpt, pattern). Every launcher reads the record on each pass (cheap local read).

**Launcher.** New launcher state `logged_out` (no inactivity check, no screen watch timer):
- entered from any state when the own screen is logged out (confirmed) and, from `idle` without a task, also when the record says the own type is logged out, so idle seats stop claiming before they fail;
- with a task: `labels.set_label(..., cfg.label_error, "login", seat, comment=messages.AGENT_LOGGED_OUT, op_id=f"login-{task_id}-{since}")`;
- `step()` in `logged_out`: no tick, no claim, no write. The launcher whose `checker_pid` is itself, or whose `checker_pid` is dead (procutil liveness), runs the login check every `cfg.login_poll_minutes`, taking over `checker_pid` under the lock, so one check per type per project per interval;
- check `in`: write `recovered_at = now`, clear `since` (log `agent_logged_in`). Every launcher of that type in `logged_out` then clears its `Error` with cause `login` (`labels.clear_label`), comments `AGENT_LOGGED_IN` (op id `login-ok-{task_id}-{since}`), and, holding a task, queues `messages.PROMPT` as a pending write and returns to `idle` with that pending write (sent by `flush_assignment` on a ready, settled screen, then `working`); without a task it returns to `idle`;
- startup: `startup_step` treats a `logged_out` classification as not ready and not failed; the launcher sets the record and waits in `starting` without the start deadline (`start_deadline` is reset to now + `start_timeout_seconds` when the record is cleared).

**No loop.** On a new episode for a type whose `recovered_at` is less than 5 minutes old and whose last check said `in`, set `resend_blocked`; the comment is `AGENT_LOGIN_DISAGREES` instead of `AGENT_LOGGED_OUT`. While `resend_blocked`, a recovery clears nothing by itself; each launcher holding a task watches its card (periodic board check) and, when the user has removed `Error` with cause `login`, queues the prompt and returns to `idle` as above. `resend_blocked` is cleared when an episode ends without it being needed (an hour without a new episode).

**Q&A seat.** `QaLauncher` follows the same states; its conversation card gets the `Error`; the router of the questions feature skips a logged-out type when opening conversations.

**Status and doctor.** `kankan status` reads `agent-login.json`. `doctor` unchanged.

**Alerts.** A8 through the alerts module with key `(login, type)`, posted by the launcher that started the episode (`alert_posted` prevents doubles).

**Configuration and messages.** `login_poll_minutes: int = 2` (positive integer). `messages.py`: `AGENT_LOGGED_OUT`, `AGENT_LOGGED_IN`, `AGENT_LOGIN_DISAGREES`, `AGENT_LOGIN_EXPIRING` with the texts of [functional §8](dialogs_spec_functional.md#8-logged-out-agent).

**Tests.** The fake agent gets a `logged_out` scenario step (prints a login error and returns to its prompt) and a fake `login_check` command driven by a file the test writes (exit 0 or 1, or sleeps for a timeout); acceptance tests 12–17 of [functional §12](dialogs_spec_functional.md#12-acceptance-tests). Unit: the new classification precedence (a working screen with login words is working); the advance warning matches `login_expiring` and no `logged_out` pattern; acceptance test 18.

## 12. Changes to the initial technical specification

- **§8.3 Launcher startup, step 6:** a failure at the startup deadline on an unrecognized screen includes the screen's last lines.
- **§8.4 State machine:** an unrecognized screen is checked in every state holding a card, not only `working` and `suspending` ([§2](#2-screen-watch)); the column "Inactivity check" keeps its meaning for ready-without-signal.
- **§8.5 Cycle and signals, step 3:** the first write after an agent start waits `ready_settle_seconds` of ready screen ([§7](#7-settle-before-the-first-write)); a claim is not made while the screen is unrecognized ([§5](#5-seat-without-a-card)).
- **§8.6 Pane observation and writes:** a new state **logged out** between working and ready ([§11](#11-logged-out-agent)).
- **§8.6 Pane observation and writes, item 5:** "unknown → no writes; if it persists in `working`, `Error` with a comment" becomes "unknown → no writes by kankan; after `screen_blocked_minutes` in any state holding a card, `Error` with the screen's last lines, removed when the screen is recognized again; keys only from a user's `keys:` comment".
- **§8.4 State machine:** new state `logged_out` (no inactivity check) ([§11](#11-logged-out-agent)).
- **§8.7 Adapters:** new indicator groups *logged out* and *login expiring* (the latter informational, outside the classification).
- **§18.1 Strategy:** the fake agent also simulates a dialog that appears after the agent is ready, a login error, and a login check.
- **Questions technical:** `QaLauncher` runs the same screen watch; a `keys:` comment is filtered out before a comment can open a question turn.
- **Alerts technical:** alert A7 with key seat + fingerprint ([§5](#5-seat-without-a-card)); alert A8 with key login + type and A9 with key login-expiring + type ([§11](#11-logged-out-agent)).
