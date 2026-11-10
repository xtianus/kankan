# Adapter notes

An *adapter* tells kankan how to drive one type of coding agent (`claude`, `codex`) running in a tmux pane: the command that starts it, the regex patterns that classify its screen (ready, working, usage limit, trust window, known dialogs), and the keys and commands kankan sends (trust answer, new conversation, submit key, whether input may be queued while the agent works). See `kankan/adapters.py` and the user manual, [section 11](user-manual.md#11-adapters).

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
- **Ready:** `› Ask Codex to do anything` with the footer `? for shortcuts`. The same footer stays visible while Codex works, so *working* must take precedence over *ready* (it does, [spec §8.6](../aispecs/initial_spec_technical.md#86-pane-observation-and-writes)).
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

## Codex runs commands outside the agent's environment

Observed on 2026-11-09 with codex-cli 0.167.0, in the first live run: `codex` hands shell commands to a shared, long-lived `codex app-server --listen unix:// --managed-daemon` (parent pid 1, started outside kankan). The commands get the daemon's environment: its own `PATH` (`…/codex-path:~/.local/bin:…`) and none of `KANKAN_SEAT`, `KANKAN_SESSION`, `KANKAN_PROJECT`. The agent reported `kankan: command not found` and stopped. kankan now requires `kankan` on the login `PATH` and the agent commands read seat and session from `<worktree git dir>/kankan-agent.json` when the variables are missing. Claude Code runs commands as its own children and inherits the environment.

## Full-access mode

Observed in the live run of 2026-11-09: Codex 0.167.0 with its default sandbox ran `kankan task --json` without network access, then stopped on an approval dialog ("May I let kankan access Trello outside the sandbox…") that kankan does not recognize; after 10 minutes the card got `Error`. kankan therefore starts every agent in full-access mode: `claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox` (both flags checked on Claude Code 2.1.319 and codex-cli 0.167.0). To verify on the next live run: the confirmation Claude Code may show the first time it starts with `--dangerously-skip-permissions` on an account that never accepted bypass mode.

## Model check without context files

Measured on 2026-11-10 with Claude Code 2.1.322 and codex-cli 0.167.0, from an empty temporary directory outside any Git repository, with a user-level `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` present. Method: the prompt asks the agent whether it was given any `CLAUDE.md`, `AGENTS.md`, memory or user instruction files and to quote the first words of each, or to reply `NONE`; the token counts come from `claude -p --output-format json` (input plus cache tokens) and from the `tokens used` line of `codex exec`.

- **Claude Code:** `claude -p --no-session-persistence --model haiku --effort low` alone loads `~/.claude/CLAUDE.md` and the auto-memory instructions (about 23,700 input tokens). Adding `--setting-sources "" --tools "" --strict-mcp-config --disable-slash-commands --exclude-dynamic-system-prompt-sections` removes `CLAUDE.md` but keeps the auto-memory block (about 3,600). Adding also `--settings '{"autoMemoryEnabled":false}'` (or the variable `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) removes it: the answer is `NONE`, about 2,800 input tokens. `--bare` would also skip `CLAUDE.md`, but it accepts only API-key authentication (`ANTHROPIC_API_KEY` or `apiKeyHelper`), not the subscription login.
- **Codex:** `codex exec --ephemeral --skip-git-repo-check` loads `~/.codex/AGENTS.md` even from an empty directory, and `--ignore-user-config --ignore-rules -c project_doc_max_bytes=0` does not stop it (about 5,800 tokens either way). With `CODEX_HOME` pointing to a temporary directory that holds only a symlink to `~/.codex/auth.json`, no `AGENTS.md` is loaded: about 2,450 tokens. Codex fills that directory with its state (SQLite files, `models_cache.json`, plugins); the symlink stays a symlink.
- **Codex model catalog:** `$CODEX_HOME/models_cache.json` lists the models (`slug`), their `supported_reasoning_levels` (e.g. `gpt-6-luna`: low…max; `gpt-6.1-sol`: low…ultra), `default_reasoning_level`, `priority` (the picker's order: `gpt-6.1-sol` 1, `gpt-6-astra` 2, …, `gpt-5.6-luna` 9) and `visibility` (`list` or `hide`). `codex debug models` prints the same catalog as JSON (`{"models": […]}`): same models, `priority`, `visibility` and levels, with the same field names plus `base_instructions`; it refreshes the catalog first. `codex debug models --bundled` prints the catalog built into the binary, which differs (it has `gpt-5.5`, not `gpt-reserve`).
