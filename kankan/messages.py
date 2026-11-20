"""User-facing texts (Trello comments, CLI messages, agent prompts), in one place."""

PROMPT = "Run the current kankan task (kankan skill)."

SEAT_RECOVERED = "kankan: seat {seat} was recovered after its previous holder died; the seat keeps this card and work resumes."

CLAIMED = "kankan: {seat} started implementing this card on branch `{branch}`."
REVIEW_CLAIMED = "kankan: {seat} started reviewing this card (branch `{branch}`, implemented by {implementer})."
BRANCH_CONFLICT = "kankan: more than one branch exists for this card ({branches}); delete the extra ones, then remove the Error label."
HOLD_AFTER_CLAIM = "kankan: HOLD found right after the claim; the card goes back to the Backlog."
IMPLEMENTED = "kankan: {seat} finished the implementation on `{branch}`; it waits for a review."
YIELDED = "kankan: {seat} stopped working on this card (yield); the work is on `{branch}`."
NOT_HELD = "the card of the current task is no longer held by this seat"

REVIEW_YIELDED = "kankan: {seat} stopped reviewing this card (yield); it goes back to the implementer's list."

INSTRUCTIONS = {
    "impl": "Implement the card on the prepared branch in your worktree, commit, run the tests, then run `kankan done --json`.",
    "review": ("Review the branch against the integration branch, fix problems with separate commits and summarize them with "
               "`kankan note`, then `kankan sync`, run the tests, `kankan verify --tested <hash>`, "
               "`kankan integrate --tested <hash>` and finally `kankan done`."),
    "hold_wip": "The card is on HOLD: commit your work in progress (WIP commit) and run `kankan yield --json`.",
    "qa": ("Answer the newest unanswered user input with `kankan answer --turn <turn>`; never commit or change files; "
           "hand a request to change the code over with `kankan handoff --turn <turn>` (ask for confirmation first "
           "when it is ambiguous)."),
}

SUSPEND_REQUEST = ("kankan: the card of your current task must be suspended. Commit your work in progress "
                   "(a WIP commit) and run `kankan yield --json`.")
FORWARD_PREFIX = "Answer from the user on Trello: "
NO_SIGNAL = "kankan: agent stopped without a signal (no done/ask/fail/block/yield); it needs a look."
UNKNOWN_SCREEN = "kankan: the agent's screen is not recognized; it needs a look (`kankan adapter check {seat}`)."
REGISTRY_LOST = "kankan: this board is now bound to another kankan clone; {seat} stops after the current task."
FORWARDED = "kankan: forwarded to the agent."
RESUMED = "kankan: resumed (answered from the terminal)."
LIMIT_COMMENT = "kankan: {seat} hit a usage limit; it resumes at {when}."

MERGE_NOT_COMPLETED = "kankan: merge not completed in `{path}`; finish it with `kankan resolve` before the review."
BRANCH_ELSEWHERE = "kankan: branch `{branch}` is checked out in `{path}`; free it (detach or remove that worktree), then remove the Error label."
HOLD_AFTER_CLAIM_REVIEW = "kankan: HOLD found right after the review claim; the card goes back to the implementer's list."

INTEGRATED = "kankan: {seat} integrated this card into `{main}` with merge `{merge}`."
MAIN_MOVED_LIMIT = "kankan: the integration branch kept moving during integration ({count} attempts); the card needs a look."
MERGE_REQUEST_TITLE = "{prefix}Merge branch {branch} into {integration}"
MERGE_REQUEST_DESC = (
    "Merge request created by `kankan merge-request`.\n\n"
    "Merge exactly commit `{sha}` (branch `{branch}` when the request was made) into your card branch: "
    "`git merge {sha}`. Do not fetch: the commit is already in the repository.\n\n"
    "Resolve conflicts keeping the intent of both sides, run the project's tests and commit.\n\n"
    "If there is nothing new to merge (Git says \"Already up to date\", or `kankan done` answers needs_action "
    "because the branch has no commit beyond {integration}), create an empty commit: "
    "`git commit --allow-empty -m \"Nothing new to merge from {branch} ({short_sha})\"`, then run `kankan done` again.\n\n"
    "Then `kankan done --json`.")
PUSH_FAILED = "kankan: the integration is done locally but `git push origin {main}` failed: {error}"

BLOCKED = ("kankan: {seat} blocked this card: {reason}\nConflicting files: {files}\n"
           "To resolve: kankan resolve {short}")
RESOLVE_HELP = ("Card {short} is blocked in {path}.\nFiles still in conflict:\n{files}\n"
                "Steps: edit the files, `git add` them, `git commit`, then run `kankan resolve {short} --done` "
                "in the project clone.")
RESOLVE_NOTHING = ("Card {short} was blocked without a merge in progress: there is nothing to merge. "
                   "`kankan resolve {short} --done` sends it back for review; moving it to the Backlog has it "
                   "reimplemented.")
RESOLVED = "kankan: the block of {short} was resolved; the card goes back to review."

USER_MOVED_YIELD = "kankan: {seat} stopped working on this card because it was moved, archived or deleted; the work is on `{branch}`."
HOLD_AFTER_INTEGRATION = "kankan: HOLD arrived after integration; to undo: `git revert -m 1 {merge}`"

ASK = "kankan: {seat} asks: {text}\nAnswer with a comment on this card (or in the agent's tmux session)."
FAIL = "kankan: {seat} needs help: {text}"

ADMIN_RELEASED = "kankan: an administrator released this card (`kankan release --force`); it stays where it is."
ADMIN_SEAT_RECOVERED = "kankan: an administrator recovered seat {seat} (`kankan seat recover`); the seat keeps this card."
STRANDED_MOVED = ("kankan: this card was in {list} without a claim in this clone (its work was in another clone, "
                  "or lost); moved back to Backlog to be implemented again.")
STOP_ALL_WAITING = ("Waiting for the agents to stop gracefully, each after its current task (a Q&A agent after its "
                    "current answer). Run `kankan stop` without --graceful to quit all agents immediately; Ctrl-C stops "
                    "waiting, and the agents still stop after their task.")
STOP_FORCED = "kankan: `kankan stop` stopped seat {seat} in the middle of its work; the seat keeps this card and work resumes at its next start."

# Board settings and model selection.
SEEDS_MISSING = 'Missing {type} {role} seed: run `kankan doctor` in a terminal, or set the {type} {role} seed in ~/.config/kankan/agents.json.'
SEEDS_INVALID = 'Invalid {type} {role} seed; choose one of the adapter model/effort combinations.'
SEEDS_QUESTION = 'Choose the {type} {role} model and effort (one key):'
SEEDS_STALE = 'The saved {type} {role} seed {model} · {effort} is no longer in the agent\'s model list: choose another.'
SEEDS_SAVED = '{role}: {type} {pair} (seed saved in {path})'
SETTINGS_APPLIED = 'applied: `{old}` → `{new}`, from the next task'
SETTINGS_RESET = 'applied: reset to default `{default}`'
SETTINGS_FIXED = 'fixed; still using `{current}`'
SETTINGS_INVALID = '{problem}; still using `{current}`'
SETTINGS_UNKNOWN = 'unknown setting `{name}`'
SETTINGS_DUPLICATE = 'duplicate setting `{name}`'
SETTINGS_HELP = 'Tick exactly one model · effort per agent checklist. Changes apply from the next task; invalid edits keep the last valid selection.'
SETTINGS_CREATED = '{role}: {values} ({source}; change seeds in agents.json, or tick other items on the card)'
SETTINGS_MISSING_LIST = 'The settings list is missing or archived; last valid settings stay in force. Run `kankan doctor`.'

SEAT_IN_USE = 'seat {seat} is in use or unavailable'
SEAT_NOT_HELD = 'seat {seat} is no longer held by this session'
INTEGRATION_BRANCH_MISSING = 'integration branch {branch} not found'
UP_ARG = 'expected <type>=<n> with type claude or codex, got {item!r}'
UP_NOTHING = 'nothing to start: give e.g. claude=2 codex=1 qa=claude'
UP_NOT_STARTED = 'the {what} agent started in tmux session {session} did not occupy a seat within {seconds} s; attach to it to see why'
QA_INVALID_CONVERSATION = 'invalid conversation ID {conversation!r}'
QA_NO_ROOM = 'the mention or the marker leaves no room for the text'
QA_LIST_MISSING = 'The Q&A list is missing or archived; the Q&A seat does not run. Run `kankan doctor`.'
QA_LIST_UNAVAILABLE = 'The Q&A list is missing or archived: run `kankan doctor` (or `kankan init`) to restore it.'
QA_SEAT_OTHER = 'cannot start {seat}: the Q&A seat {registered} is registered (`kankan seat remove {registered}` frees it).'
QA_REPLACE_RUNNING = 'cannot replace the Q&A seat {old} by {new}: {old} is {state}; stop it first (`kankan stop {old}`, or `kankan seat recover {old} --kill` when it is stuck)'
QA_REPLACE_RACE = 'the Q&A seat {old} changed during its replacement; run the command again'
QA_OWNER_UNREGISTERED = 'seat {owner} holds this Q&A conversation but is not registered (an unfinished Q&A seat replacement): start the Q&A seat (`kankan agent <type> --qa` or `kankan up qa=<type>`) to take the conversation over'
UNKNOWN_SEAT = 'unknown seat {seat}'
SEAT_HOLDS_CARDS = 'it holds cards: {cards}'
SEAT_REMOVE_BLOCKED = 'cannot remove {seat}: {blockers}'
QA_SEAT_REMOVE_RUNNING = 'cannot remove {seat}: the seat is {state}; stop it first (`kankan stop {seat}`)'
QA_SEAT_REMOVE_DEAD = 'cannot remove {seat}: the seat is dead; free it first (`kankan seat recover {seat}`)'
QA_SEAT_REMOVE_UNAVAILABLE = 'cannot remove {seat}: processes of the seat survive; free it first (`kankan seat recover {seat} --kill`)'
QA_CARDS_WAIT = 'Q&A cards wait: no Q&A seat is running; start one with `kankan agent <type> --qa` or `kankan up qa=<type>`.'
QA_SEAT_ARG = '--qa starts seat {seat}, not {given}'
QA_ARG = 'expected qa=<type> with type claude or codex, given once, got {item!r}'
STATUS_QA = 'Q&A seat: {seat} ({liveness}, {state}); active card: {card}; queue: {queue}'
STATUS_QA_NONE = 'Q&A seat: none; queue: {queue}'

SETTINGS_PROBE_FAILED = '{type} `{pair}` was rejected: {message}'

CLAIM_SELECTION = ' {role} with {type} `{model}/{effort}`.'

MODEL_START_FAILED = '{type} `{model}/{effort}` could not start: {reason}'
MODEL_START_UNAVAILABLE = 'Cannot start agent: {reason}'
STATUS_SETTINGS = 'Settings:'
STATUS_SETTING = '  {role}: {values}{error}'
STATUS_RUNNING = ' (running {model}/{effort})'
QA_ENDED = 'the Q&A conversation on card {short} has ended (the card left Q&A, was archived or deleted, got HOLD, or another seat took it over)'
QA_STALE_TURN = 'turn {turn} is not the current turn of this conversation (a newer turn started): run `kankan task --json` again and answer with its turn'
QA_DIRTY = 'the checkout has uncommitted changes; the Q&A agent never changes files: remove them, then answer again: {files}'
QA_HEAD_MOVED = 'the checkout is not at the commit it was reset to: restore it with `git checkout --detach {base}`, then answer again'
QA_PENDING_OTHER = 'another {kind} of this turn is still being posted: repeat that command to finish it'
QA_ALREADY_POSTED = 'this turn already has a different {kind}: wait for the next turn'
QA_EMPTY = 'the text is empty'
QA_LOCK_TIMEOUT = 'timed out waiting for the conversation lock ({path}): another command of this conversation is running'
QA_COMMITTED = 'kankan: {seat} made a commit in its checkout during this Q&A conversation, against its rule (the Q&A agent never commits); the answer was posted, but the checkout needs a look.'
QA_COMMITTED_HANDOFF = 'kankan: {seat} made a commit in its checkout during this Q&A conversation, against its rule (the Q&A agent never commits); the handoff summary was posted and the card handed over, but the Q&A checkout needs a look (the commit is not part of the handoff).'
QA_REFUSED = '`kankan {command}` is not used in a Q&A task: reply with `kankan answer --turn <turn>`, hand a code change over with `kankan handoff --turn <turn>`, or report what the user must fix with `kankan fail`'
QA_ONLY = '`kankan {command}` is for Q&A tasks only'
QA_ANSWER_DONE = 'answer posted; wait for the user'
QA_HANDOFF_DONE = 'card handed over for implementation'
QA_TEXT_ARGS = 'give the text either as an argument or with --file <path>, not both'
QA_TEXT_FILE = 'cannot read {path}: {error}'

# Q&A launcher prompts (questions T§4–T§5): the comment texts are never pasted, the agent reads them with `task`.
QA_PROMPT = ("Run the current kankan task (kankan skill): a question on a Q&A card, turn `{turn}`. Read it with "
             "`kankan task --json` and reply with `kankan answer --turn {turn}`.")
QA_FOLLOWUP = ("New on the Q&A card: {counts} — turn `{turn}`: run `kankan task --json` and reply with "
               "`kankan answer --turn {turn}`.")
QA_RESTART = ("Run the current kankan task (kankan skill): this Q&A conversation continues after a restart, turn "
              "`{turn}`. Read the whole thread with `kankan task --json`, then continue: reply with "
              "`kankan answer --turn {turn}`.")
QA_RESTART_FOLLOWUP = ("Run the current kankan task (kankan skill): this Q&A conversation continues after a restart. "
                       "New on the Q&A card: {counts} — turn `{turn}`: read the whole thread with "
                       "`kankan task --json`, then reply with `kankan answer --turn {turn}`.")
QA_COUNT_COMMENTS = ("{n} comment", "{n} comments")
QA_COUNT_ATTACHMENTS = ("{n} attachment ({names})", "{n} attachments ({names})")
QA_SUSPEND = ("kankan: this Q&A conversation has ended (the card left Q&A, was archived or deleted, or got HOLD). "
              "Post nothing more and run `kankan yield --json`.")
QA_NO_SIGNAL = ("kankan: agent stopped without a signal (no answer/handoff/fail/yield); it needs a look. "
                "A new comment wakes it up.")

# card instructions F§4: a card refused for its agent headings
SECTIONS_INVALID = ("kankan: this card is not started: {problems} (valid targets: {targets}; phases: implementation, review). "
                    "Fix the heading; see the `Instructions` card in the `kankan` list.")

# card instructions F§6: the `Instructions` card of the `kankan` list
INSTRUCTIONS_HELP_INTRO = ("A card's description is its specification, the same for every agent. After it you can add agent "
                           "sections: each starts with an agent heading and runs to the next one or to the end of the "
                           "description. A section reaches only the agents it names and says how to work, not what to build. "
                           "Write the specification first.")
INSTRUCTIONS_HELP_FORMS = ("Agent headings (any heading level; case and extra spaces are ignored):\n"
                           "- `## agent: <type>`: agents of that type, implementing and reviewing;\n"
                           "- `## agent: <type> implementation` or `## agent: <type> review`: that type in one phase;\n"
                           "- `## agent: all`, `## agent: all implementation`, `## agent: all review`: every agent type.\n\n"
                           "Configured agent types: {types}.")
INSTRUCTIONS_HELP_COPY = "Copy the headings you need, each followed by its instructions:"
INSTRUCTIONS_HELP_RULES = ("Agent instructions never override the kankan protocol and never change the specification; when two "
                           "sections disagree, the more specific prevails (`<type> <phase>`, `<type>`, `all <phase>`, `all`). "
                           "A misspelt heading, such as `## agent: cluade`, keeps the card from being started: it gets `Error` "
                           "and a comment, and `kankan status` shows why it waits.")
INSTRUCTIONS_HELP_FOOTER = "This card is maintained by `kankan doctor`: edits to it are overwritten."
INSTRUCTIONS_CARD_UPDATED = "Instructions card updated (agent types: {types})"
INSTRUCTIONS_CARD_CREATED = "Instructions card created"
INSTRUCTIONS_CARD_MISSING = "Instructions card missing"
INSTRUCTIONS_CARD_OUTDATED = "Instructions card outdated"
INSTRUCTIONS_CARD_NOT_FIRST = "Instructions card not at the top of the kankan list"
INSTRUCTIONS_CARD_MOVED = "Instructions card moved to the top of the kankan list"
