# Adapter notes

Observations made on 2026-11-08 on the development server with Claude Code 2.1.319 and codex-cli 0.167.0, in a
scratch tmux server (120×40). The saved screens are in `tests/fixtures/screens/` (paths and host names sanitized);
the defaults are in `kankan/adapters_default.json`.

## Claude Code 2.1.319

- **Trust window:** "Accessing workspace … Quick safety check … ❯ No, exit / Yes, I trust this folder". The cursor
  starts on *No, exit*; digits do not select. Answer: `Down`, `Enter`.
- **Ready:** the input line starts with `❯` in column 0 (menus indent their `❯`).
- **Working:** a spinner line such as `✶ Proofing… (2s · ↓ 175 tokens · thinking with medium effort)`. This
  version does **not** print "esc to interrupt"; the default adapter matches the `… (<n>s` shape plus the
  airesumer busy patterns. When finished the line becomes `✻ Sautéed for 3s · done 7:43 PM` (not working).
- **New conversation:** `/new` is an alias shown as `/clear (new)`; the default sends `/clear` directly.
- **Queueing while working:** documented behaviour of Claude Code (messages typed while it works are queued);
  `queue_while_working: true`.

## Codex 0.167.0

- **Trust window:** "Folder access … Trust this folder? … › 1. Trust and continue". Digits do not select; the
  first option is preselected. Answer: `Enter`.
- **Ready:** `› Ask Codex to do anything` with the footer `? for shortcuts`. The same footer stays visible while
  Codex works, so *working* must take precedence over *ready* (it does, §8.6).
- **Working:** `• Working (3s • esc to interrupt)`.
- **New conversation:** `/new` opens the dialog "Where should the new conversation run? › 1. Use current Git
  worktree / 2. Create new Git worktree"; it is a known dialog answered with `Enter` (keeps the seat worktree).
- **Queueing while working:** a message typed and submitted while Codex was working stayed in the composer and was
  not sent, so `queue_while_working: false`: the launcher waits for *ready* before sending a suspension request.

## Not verified

- Real usage-limit screens of either agent (no limit was reached); the limit fixtures are synthetic, built from
  the airesumer patterns. Check them with `kankan adapter check <seat>` the first time a limit happens and add an
  override if needed.
- The Codex model-switch dialog (fixture built from the airesumer patterns).
- PDEATHSIG and terminal control with the real agents (Task 13 manual check), see below.
