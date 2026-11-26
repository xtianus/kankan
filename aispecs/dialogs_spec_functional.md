# kankan unexpected agent dialogs and logged-out agents — functional specification

**Status:** specified, not implemented.
**Date:** 2026-11-26
**Base:** [`initial_spec_functional.md`](initial_spec_functional.md) v1.3, [`questions_spec_functional.md`](questions_spec_functional.md) and [`alerts_spec_functional.md`](alerts_spec_functional.md) (specified, not implemented); this feature changes them as listed in [§9](#9-changes-to-the-initial-specification), [§10](#10-changes-to-the-questions-specification) and [§11](#11-changes-to-the-alerts-specification).
**Companion:** [`dialogs_spec_technical.md`](dialogs_spec_technical.md) — how kankan implements this feature.

**Split rule.** This file describes what users and agents observe; the technical file describes how kankan implements it. Section numbers are this feature's own; references to the initial specification name its file.

## 1. Scope

Agent CLIs sometimes show a dialog that waits for a key: a model retirement notice, a new terms screen, an update offer, a survey. kankan answers the dialogs it knows ([initial technical §8.6](initial_spec_technical.md#86-pane-observation-and-writes)); any other screen is *unrecognized* and kankan never writes to it.

**Observed case (live agents run of 2026-11-26).** Codex, started with `gpt-5.6-sol`, became ready, kankan assigned it a card and sent `/new`; Codex then showed "GPT-5.6 Sol retires on December 23, 2026. Switch to GPT-6.2 Sol to continue working in Codex. › 1. Try new model 2. Use existing model". The prompt was never sent, the card stayed in `codex-1 · In Progress`, and nothing appeared on the board: an unrecognized screen gets `Error` only in the `working` state, after the inactivity time, and the seat had not reached `working` because its prompt was still pending. The task would have waited forever.

This feature:
1. never lets a seat that holds a card wait silently on an unrecognized screen: the card gets `Error` with the screen's last lines after a short time, in every launcher state ([§3](#3-seat-blocked-by-an-unrecognized-screen));
2. lets the user unblock it from the board by replying with the keys to press ([§4](#4-answering-from-the-board)), and clears the `Error` by itself when the screen is recognized again;
3. reports an unrecognized screen of a seat with no card ([§5](#5-seat-without-a-card));
4. waits for the screen to settle after an agent starts before writing to it, so a dialog that opens a moment after startup does not receive kankan's text ([§6](#6-settled-screen-before-the-first-write));
5. adds Codex's model retirement notice to the known dialogs ([§7](#7-new-known-dialog-codex-model-retirement));
6. stops an agent type whose login expired from failing card after card, tells the user to log in again, and resumes by itself after the login ([§8](#8-logged-out-agent)).

## 2. Terms

- **Recognized screen:** a screen the agent's adapter classifies as ready, working, usage limit, trust window or known dialog ([initial technical §8.6](initial_spec_technical.md#86-pane-observation-and-writes)).
- **Unrecognized screen:** any other screen. kankan still never writes to it on its own ([initial §18.3](initial_spec_functional.md#183-acceptance-tests), test 32).
- **Blocked seat:** a seat whose agent has shown an unrecognized screen without interruption for `screen_blocked_minutes` (project configuration, default 3).

## 3. Seat blocked by an unrecognized screen

**When.** A seat is checked for a blocked screen whenever it holds a card, whatever the launcher's state: a claimed card whose new-conversation command or prompt is not yet sent, `working`, `suspending`, `closing`, `waiting_human`, and the Q&A seat's states ([§10](#10-changes-to-the-questions-specification)). Startup keeps its own deadline ([§3.1](#31-startup)).

**What the user sees.** The card gets `Error` with one comment:

> kankan: codex-1's agent shows a screen kankan does not recognize, and its task cannot go on. Last lines of the screen:
> ```
> (up to the last 15 non-blank lines, colours and URLs removed)
> ```
> kankan's last write to the agent was `/new` 4 minutes before the screen appeared. Answer it in the agent's session (`tmux attach -t ai-<project>-codex-1`), or reply on this card with `keys:` and the keys to press, for example `keys: 2` or `keys: Down Enter`. To answer this screen automatically next time, add it as a known dialog (`kankan adapter check codex-1` shows how kankan classifies it).

The mention follows [initial §14.2](initial_spec_functional.md#142-notifications). The line about kankan's last write appears only when a write was sent less than one minute before the screen appeared, since that write may have gone to the dialog. One comment per blocked episode; a screen that changes into another unrecognized screen keeps the same episode.

**Recovery.** When the screen is recognized again (the user answered in tmux or from the board, or the dialog closed by itself), kankan removes the `Error` it set for this episode and comments "kankan: the agent's screen is recognized again; the task goes on." Then the launcher continues where it stopped: it sends the pending new-conversation command or prompt when the screen is ready, or keeps observing the working agent. An `Error` the user removes by hand while the screen is still unrecognized is not set again for the same episode.

**What does not change.** A screen that is ready without a signal for the inactivity time still gets "agent stopped without a signal" ([initial technical §8.6](initial_spec_technical.md#86-pane-observation-and-writes)); a blocked screen is reported only by this rule, after `screen_blocked_minutes` instead of the inactivity time. The card stays with the seat and in its list; no work is lost.

### 3.1 Startup

While an agent starts (or restarts between roles), an unrecognized screen is covered by the startup deadline as today ([initial technical §8.3](initial_spec_technical.md#83-launcher-startup)). The failure comment on the card, and the audit log, add the screen's last lines, as in [§3](#3-seat-blocked-by-an-unrecognized-screen).

## 4. Answering from the board

While a card has the `Error` of a blocked seat, a comment from a board member other than kankan's own account whose first line is `keys:` followed by one or more key names, separated by spaces, is sent to the agent as those keys, in order.

- **Key names:** a single letter, digit or punctuation character; `Enter`, `Esc`, `Tab`, `Space`, `Up`, `Down`, `Left`, `Right`, `BSpace`. Names are case-insensitive. At most 10 keys.
- **Same screen only.** kankan sends the keys only when the agent still shows the screen reported in the comment; otherwise it sends nothing and comments "kankan: the screen changed since it was reported; keys not sent." With the new screen's last lines when it is still unrecognized.
- **Reply.** After sending, kankan comments "kankan: sent the keys `2` to the agent." Each `keys:` comment is used once. A comment with invalid key names gets "kankan: not sent: unknown key `F13`; use …" listing the key names.
- **Esc.** kankan itself never sends `Esc` or other interrupt keys ([initial technical §8.6](initial_spec_technical.md#86-pane-observation-and-writes)); a user's `keys:` comment may, because the user chose it.
- Other comments on the card are not forwarded while the seat is blocked, as before.

## 5. Seat without a card

An agent with no card (an idle seat, or the Q&A seat between conversations) can show an unrecognized screen too; it blocks the next task. After `screen_blocked_minutes`:
- `kankan status` shows the seat as `blocked: unrecognized screen since <time>`, with the screen's last lines under `--verbose`;
- the audit log records it once per episode;
- the seat takes no new card while its screen is unrecognized: a task is claimed only on a recognized screen, so no card is moved to its `In Progress` list meanwhile;
- with the alerts feature, the `Alerts` card gets alert A7 ([§11](#11-changes-to-the-alerts-specification)).

## 6. Settled screen before the first write

Some dialogs open a few seconds after the agent first looks ready (Codex shows its retirement notice after it has loaded its model list). After an agent starts or restarts, kankan writes the first text only once the screen has been ready for `ready_settle_seconds` (project configuration, default 10) without interruption. Later writes keep the rule of [initial technical §8.6](initial_spec_technical.md#86-pane-observation-and-writes): each one needs a ready screen at the time of the write.

## 7. New known dialog: Codex model retirement

Codex's model retirement notice ("… retires on …", "Use existing model") becomes a known dialog of the default Codex adapter, answered with **Use existing model**. The model of a task is chosen on the board ([board settings](boardsettings_spec_functional.md)); switching silently would run the task with a model nobody chose. kankan records the notice in the audit log with its text, so the date of the retirement is not lost. When the model is withdrawn, the model check of the board settings reports it.

## 8. Logged-out agent

**Problem.** An agent's login can end while agents run: an expired or revoked token, a password change, an ended subscription. Today only `kankan doctor` checks the login, once, and only warns. Afterwards the agent answers each prompt with an error and returns to its prompt; after the inactivity time the card gets "agent stopped without a signal", and the seat claims the next card and fails it the same way: one card every few minutes, until `Backlog` is full of `Error`. An agent that starts logged out shows a login screen, and its start fails at the deadline with a reason that does not mention the login.

**Detection.** kankan recognizes a logged-out agent by its screen: a login screen, or a login error message such as "Please run /login" or "OAuth token has expired" in the last lines, when the agent is not working. Since an agent's own output can contain such words (for example while it works on login code), kankan confirms with the agent type's login check (`claude auth status`, `codex login status`, as `kankan doctor` uses): when the check says logged in, the screen is not treated as logged out and the other rules apply. When the check does not answer in time, the screen alone decides.

**Per agent type.** All seats of one agent type on this machine share one login, so a logged-out agent stops its whole type in the project: no seat of that type claims a card, and a Q&A seat of that type opens no conversation, until the login is back. The other agent types go on working.

**What the user sees.**
- The card of each seat of that type that holds a task, once that seat shows the logged-out screen, gets `Error` with the comment: "kankan: codex is logged out on `<host>`, so its tasks wait. Log in again: `codex login` in a terminal of `<host>`, or `/login` in the agent's session (`tmux attach -t ai-<project>-codex-1`). kankan resumes the task by itself after the login." The mention follows [initial §14.2](initial_spec_functional.md#142-notifications).
- `kankan status` shows `logged out since <time>` on every seat of that type, and `kankan doctor` reports the login check as failed.
- With the alerts feature, the `Alerts` card gets alert A8 once for the type ([§11](#11-changes-to-the-alerts-specification)), also when no seat holds a card.

**Recovery.** While a type is logged out, kankan runs its login check every `login_poll_minutes` (project configuration, default 2), once for the whole project. When the check passes:
- kankan removes the `Error` it set for the login and comments "kankan: codex is logged in again; the task goes on.";
- each seat of that type that held a task sends the task prompt again (`Run the current kankan task`), in the same conversation, when its screen is ready; the agent reads the task's state with `kankan task` as usual, so work already committed on the branch is kept;
- the seats of that type claim cards again.

**No loop.** If an agent shows the logged-out screen again within 5 minutes of a recovery while the login check says logged in (the check and the agent disagree), kankan does not resend by itself again: it comments "kankan: the login check says codex is logged in, but the agent still refuses; log in in the agent's session (`/login`), then remove `Error` from this card." Removing `Error` from the card then makes kankan send the prompt again.

**Startup.** An agent that starts on a login screen is not a failed start: its launcher stays, with the agent running, and the type is logged out as above, so the user can log in from the agent's session. The startup deadline applies again from the moment the login is back.

**Advance warning.** Claude Code announces the end of its login in advance, at the bottom of its screen, with exactly: `⚠ Your login expires in 1 day · run /login to renew` (the time varies). The agent still works normally, so this line never makes the agent logged out, never changes the screen's classification and never stops a task. kankan passes it on instead, so the user can renew before the tasks stop:
- `kankan status` shows on every seat of that type `login expires in 1 day (seen <time>)`, until the warning is no longer on any of its screens for an hour or the type is logged out;
- the audit log records it once per type and day;
- with the alerts feature, the `Alerts` card gets alert A9 once per type and day ([§11](#11-changes-to-the-alerts-specification));
- without the alerts feature, kankan comments the warning once per type and day on the card the seat is working on, if any, without a label: "kankan: claude's login on `<host>` expires in 1 day; run `/login` in an agent's session (`tmux attach -t ai-<project>-claude-1`) or `claude /login` in a terminal of `<host>` to renew it, or the tasks of claude stop when it expires."

kankan never types `/login` itself: the login needs the user's browser.

**Usage limits** stay as they are ([initial technical §8.6](initial_spec_technical.md#86-pane-observation-and-writes)): a usage limit is not a logout.

## 9. Changes to the initial specification

- **§14.1 Labels, row `Error`:** the causes include "unrecognized screen while holding a card ([§3](#3-seat-blocked-by-an-unrecognized-screen))"; kankan removes this `Error` itself when the screen is recognized again.
- **§14.1 Labels, row `Error`:** the causes also include "agent type logged out ([§8](#8-logged-out-agent))", removed by kankan after the login.
- **§14.3 Answers through comments:** a `keys:` comment on a card blocked by an unrecognized screen is sent to the agent as keys ([§4](#4-answering-from-the-board)).
- **§15.2 Situations and behaviour, row "Agent alive but stuck":** an unrecognized screen gets `Error` with the screen's last lines after `screen_blocked_minutes` in any state holding a card, and is unblocked from tmux or with `keys:`; other stuck agents are unchanged. New row "Agent logged out": its type takes no card; `Error` with the login steps on each card held; resumed after the login check passes ([§8](#8-logged-out-agent)).
- **§18.3 test 32:** "Unknown screen: no writes" becomes "Unknown screen: no writes by kankan; `Error` with the screen after `screen_blocked_minutes` in any state holding a card, removed when the screen is recognized again".
- **§18.3 test 35:** known dialogs include Codex's model retirement notice, answered with "Use existing model".
- **§15.5 `kankan doctor`:** unchanged; the login check it runs is also used while agents run ([§8](#8-logged-out-agent)).
- **§18.3 new test:** a logged-out agent type takes no card and resumes after the login.
- **Technical §8.3, §8.4, §8.5 step 3, §8.6, §8.7, §18.1:** see [technical §12](dialogs_spec_technical.md#12-changes-to-the-initial-technical-specification).

## 10. Changes to the questions specification

The Q&A seat follows [§3](#3-seat-blocked-by-an-unrecognized-screen) while it holds a conversation's card (the `Error` goes on the Q&A card) and [§5](#5-seat-without-a-card) between conversations. A `keys:` comment on a blocked Q&A card is a key answer, never a new question turn. A logged-out Q&A seat ([§8](#8-logged-out-agent)) opens no conversation; a conversation it held gets `Error` and is resumed after the login; new comments on Q&A cards wait as they do while the seat is busy.

## 11. Changes to the alerts specification

Add to the table of alerts: **A7 Agent screen not recognized** — when a seat with no card shows an unrecognized screen for `screen_blocked_minutes`; posted by the launcher; the comment gives the seat, the screen's last lines, `tmux attach -t …` and how to add a known dialog. The same alert is the same seat with the same screen. Add **A8 Agent logged out** — when an agent type is logged out ([§8](#8-logged-out-agent)); posted by the launcher that detects it, once per type and episode (the other seats of the type find it already posted); the comment gives the type, the host, the login commands and that the type's tasks wait. Add **A9 Agent login expires soon** — when an agent shows its advance login warning ([§8](#8-logged-out-agent)); posted once per type and day; the comment gives the type, the host, the time left as the agent printed it and how to renew. Until the alerts feature is implemented, [§5](#5-seat-without-a-card) and [§8](#8-logged-out-agent) apply without A7, A8 and A9 (the advance warning is then commented on the working card).

## 12. Acceptance tests

1. A fake agent that turns unrecognized after kankan sent the new-conversation command, before the prompt: after `screen_blocked_minutes` the card gets `Error` with the screen's last lines and the "last write" line; the prompt is not sent.
2. The same screen in `working`, `suspending`, `closing` and `waiting_human`: `Error` after `screen_blocked_minutes`, not after the inactivity time.
3. The screen becomes ready again: kankan removes its `Error`, comments, and sends the pending prompt.
4. `keys: 2` on the blocked card: the key reaches the pane once; kankan comments; a second read of the same comment sends nothing.
5. `keys: 2` after the screen changed: nothing is sent; kankan comments with the new screen.
6. `keys: F13`: nothing is sent; the reply lists the key names.
7. The user removes the `Error` while the screen is still unrecognized: it is not set again in the same episode.
8. An idle seat with an unrecognized screen: no card is claimed; `kankan status` shows `blocked`; with alerts, A7 is posted once.
9. A dialog that appears 5 s after the agent first looks ready receives no text from kankan.
10. Codex's retirement notice is answered with "Use existing model" and recorded in the audit log; the task runs with the board's model.
11. The Q&A seat blocked during a conversation: `Error` on the Q&A card; `keys:` is not a question turn.
12. A fake agent shows a login error after the prompt and the fake login check fails: the card gets `Error` with the login comment within one poll; no seat of that type claims a card; a seat of another type still does.
13. The login check passes again: the `Error` is removed, kankan comments, the prompt is sent once more to the seat, and the type's seats claim cards again.
14. The login error text on the screen while the login check passes: not treated as logged out.
15. The login error again within 5 minutes of a recovery while the check passes: no automatic resend; the comment asks for `/login`; removing `Error` sends the prompt.
16. An agent that starts on a login screen: the launcher stays, the type is logged out; after the login the start completes.
17. Two seats of the logged-out type: one login check per `login_poll_minutes` for the project.
18. A working Claude screen ending with `⚠ Your login expires in 1 day · run /login to renew`: classified as working (or ready), not logged out; `kankan status` shows the warning; one comment (or A9) per type and day, also with two seats showing it.
