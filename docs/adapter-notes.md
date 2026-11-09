# Adapter notes

An *adapter* tells kankan how to drive one type of coding agent (`claude`, `codex`) running in a tmux pane: the command that starts it, the regex patterns that classify its screen (ready, working, usage limit, trust window, known dialogs), and the keys and commands kankan sends (trust answer, new conversation, submit key, whether input may be queued while the agent works). See `kankan/adapters.py` and [the user manual](user-manual.md), section 11.

The observations below are the evidence behind the default adapters: the patterns, answers and flags in `kankan/adapters_default.json` were set from them, and the captured screens became the test fixtures that check the screen classification. The *Not verified* section lists what was not observed on a real agent and should be checked when it first happens.

Observations made on 2026-11-08 on the development server with Claude Code 2.1.319 and codex-cli 0.167.0, in a scratch tmux server (120×40). The saved screens are in `tests/fixtures/screens/` (paths and host names sanitized); the defaults are in `kankan/adapters_default.json`.

## Claude Code 2.1.319

- **Trust window:** "Accessing workspace … Quick safety check … ❯ No, exit / Yes, I trust this folder". The cursor starts on *No, exit*; digits do not select. Answer: `Down`, `Enter`.
- **Ready:** the input line starts with `❯` in column 0 (menus indent their `❯`).
- **Working:** a spinner line such as `✶ Proofing… (2s · ↓ 175 tokens · thinking with medium effort)`. This version does **not** print "esc to interrupt"; the default adapter matches the `… (<n>s` shape plus the airesumer busy patterns. When finished the line becomes `✻ Sautéed for 3s · done 7:43 PM` (not working).
- **New conversation:** `/new` is an alias shown as `/clear (new)`; the default sends `/clear` directly.
- **Queueing while working:** documented behaviour of Claude Code (messages typed while it works are queued); `queue_while_working: true`.

## Codex 0.167.0

- **Trust window:** "Folder access … Trust this folder? … › 1. Trust and continue". Digits do not select; the first option is preselected. Answer: `Enter`.
- **Ready:** `› Ask Codex to do anything` with the footer `? for shortcuts`. The same footer stays visible while Codex works, so *working* must take precedence over *ready* (it does, §8.6).
- **Working:** `• Working (3s • esc to interrupt)`.
- **New conversation:** `/new` opens the dialog "Where should the new conversation run? › 1. Use current Git worktree / 2. Create new Git worktree"; it is a known dialog answered with `Enter` (keeps the seat worktree).
- **Queueing while working:** a message typed and submitted while Codex was working stayed in the composer and was not sent, so `queue_while_working: false`: the launcher waits for *ready* before sending a suspension request.

## Process chain with the real agents

Checked on 2026-11-08 with `kankan up` against a fake Trello board, a scratch repository and a private tmux server:

- Claude Code and Codex both reached *ready* through the kankan chain; the trust window was answered automatically (worktree under the kankan root).
- Ctrl-C typed in the pane reaches Claude Code ("Press Ctrl-C again to exit"); the launcher and the supervisor ignore it. A second Ctrl-C made Claude exit: the launcher released the seat and the tmux session closed.
- `kill -9` of the launcher: the agent (Claude Code, Codex) died through PDEATHSIG; the supervisor started a new launcher in the same pane that recovered the same seat (`seat_recovered`) and started a new agent.
- The installed `codex` is a native binary (no wrapper child process). With an npm wrapper, the native child would be terminated by the pane-session cleanup of the next launcher, not by PDEATHSIG.
- Ctrl-Z was not tried (it would stop the agent, which is the expected job-control behaviour).

## Not verified

- Real usage-limit screens of either agent (no limit was reached); the limit fixtures are synthetic, built from the airesumer patterns. Check them with `kankan adapter check <seat>` the first time a limit happens and add an override if needed.
- The Codex model-switch dialog (fixture built from the airesumer patterns).

## Login checks (`login_check`, used by `kankan doctor`)

- Claude Code 2.1.319: `claude auth status` prints a JSON object with `"loggedIn": true` and exits 0 when logged in, without starting a session.
- Codex CLI 0.167.0: `codex login status` prints `Logged in using ChatGPT` and exits 0 when logged in.
- The logged-out exit codes were not observed; doctor treats any non-zero exit as a warning, never as a blocking error.
