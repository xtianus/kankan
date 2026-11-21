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
INIT_POPULATING_BOARD = 'Populating board...'
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

CONFIG_INVALID = "invalid {path}: {error}"
CONFIG_NOT_INITIALIZED = "kankan is not initialized in this repository"
CONFIG_NO_BOARD = "no board"
CONFIG_NOT_OBJECT = "not a JSON object"
CONFIG_BOARD_ID_TYPE = "board_id must be a string"
CONFIG_FIELD_INVALID = "{field} has an invalid value"

RUNNING_LOCK_HELD = "lock held"
RUNNING_LOCK_UNREADABLE = "lock unreadable"
RUNNING_RECORD_UNREADABLE = "record unreadable"
RUNNING_PID_PREFIX = "pid "

DESTROY_GATE_SEAT = "seat {seat} is running ({reasons})"
DESTROY_GATE_SEAT_UNKNOWN = "seat {seat}: liveness cannot be established ({reasons})"
DESTROY_GATE_SESSION = "tmux session {name} of this project"
DESTROY_GATE_UNIT = "autostart unit {unit} is installed"
DESTROY_REMEDY_STOP = "kankan stop"
DESTROY_REMEDY_KILL = "kill {pid}"
DESTROY_REMEDY_LOCK = "kill the process holding {path} (see `fuser {path}`)"
DESTROY_REMEDY_UNREADABLE = "make {path} readable by this user, or remove it once no agent of the seat runs"
DESTROY_REMEDY_LOCK_UNREADABLE = ("restore read and write access to the existing file with `chmod u+rw {path}` (never delete a "
                                  "lock file), after checking with `ls -l {path}` that it belongs to this user")
DESTROY_REMEDY_SESSION = "tmux kill-session -t {name}"
DESTROY_REMEDY_UNIT = "systemctl --user disable --now {unit} && rm {path}"
DESTROY_INTEGRATION_RENAME_LOCAL = "git branch -m {name} {old}"
DESTROY_INTEGRATION_PUSH_OLD = "git push origin {old}"
DESTROY_INTEGRATION_FETCH = "git fetch origin refs/heads/{name}"
DESTROY_INTEGRATION_PUSH_FETCHED = "git push origin FETCH_HEAD:refs/heads/{old}"
DESTROY_INTEGRATION_DELETE_REMOTE = "git push origin --delete {name}"
DESTROY_INTEGRATION_HOOK_NOTE = ("note: the pre-push hook {path} is not kankan's and is kept; it may refuse these "
                                 "pushes")
DESTROY_TRELLO_TRANSIENT = "Trello did not answer; try kankan destroy again later"
DESTROY_BOARD_INCOMPLETE = "Trello returned an incomplete board read; try kankan destroy again later"
DESTROY_BOARD_CHANGED = "the board changed while kankan destroy was reading it; run kankan destroy again"
DESTROY_REGISTRY_MALFORMED = ("the kankan-registry block of the board description is damaged (a start without an end, "
                              "more than one block, no registry-id or more than one registry-id line), so kankan cannot tell whose board it is: fix or "
                              "remove the block by hand in the board description and run kankan destroy again")
DESTROY_UNREADABLE = "cannot read {path} ({error}); make it readable by this user and run kankan destroy again"
DESTROY_STATE_SYMLINK = ("{path} is a symbolic link; kankan destroy does not follow it: remove the link itself with "
                         "`rm {quoted}` (no trailing slash; what it points to stays in place) and run kankan destroy "
                         "again")
DESTROY_REFTABLE = ("this repository stores its refs in the {format} format; kankan destroy supports only the files "
                    "format: clean kankan's state by hand")
DESTROY_GIT_FAILED = "git {command} failed in {path} ({error}); fix it and run kankan destroy again"
DESTROY_INTEGRATION_BRANCH_NOT_UTF8 = ("kankan.integrationBranch in {path} is not valid UTF-8, so kankan destroy "
                                       "cannot name the integration branch safely; set it to a UTF-8 name with "
                                       "git config kankan.integrationBranch <name> and run kankan destroy again")
DESTROY_SEAT_STATE_INVALID = ("{path} cannot be read as seat state, so an interrupted kankan block cannot be ruled "
                              "out; repair or remove the file and run kankan destroy again")
DESTROY_JOURNAL_INVALID = ("{path} holds a block journal kankan cannot read, so the work of an interrupted kankan block "
                           "cannot be located; repair or remove the file and run kankan destroy again")
DESTROY_ADMIN_HEAD_INVALID = ("{path} does not name a commit or a branch, so the work of the missing worktree cannot be "
                              "checked; repair it and run kankan destroy again")
DESTROY_INTEGRATION_BRANCH_CHANGED = ("kankan.integrationBranch changed while kankan destroy was reading it; nothing "
                                      "changed: run kankan destroy again")

# kankan destroy: questions (destroy spec F§3.3-3.4); the board name is never printed
DESTROY_ASK_DISCARD = "Type '{phrase}' to destroy it, or Ctrl-C to abort: "
DESTROY_ASK_BOARD = ("Type 'destroy <board name>' (the title shown at the top of the board in Trello; the URL may show "
                     "an older name): ")
DESTROY_ASK_LOCAL = "Type '{phrase}' to remove kankan's local state, or Ctrl-C to abort: "
DESTROY_CONFIRM_UNMATCHED = ("--confirm value {index} matches no question kankan destroy asks here (it asks: "
                             "{questions}); nothing changed")
DESTROY_CONFIRM_DUPLICATE = "--confirm value {index} answers a question already answered; nothing changed"
DESTROY_COMMAND_HELP = "remove everything kankan created for this project, on the board and locally"
DESTROY_CONFIRM_HELP = "answer a question without a terminal (repeat for each question)"
DESTROY_NEEDS_TERMINAL = "kankan destroy needs the phrase '{phrase}': run it in a terminal or pass --confirm"
DESTROY_QUESTION_BOARD = "destroy <board name>"

# kankan destroy: refusals and stops (destroy spec F§5)
DESTROY_GATE_REFUSED = "agents of this project are still active; nothing changed: run `kankan stop` first"
DESTROY_GATE_LINE = "  - {what} -> {remedy}"
DESTROY_SYMBOLIC_REFS = ("symbolic agent refs found; kankan never writes refs that way, and deleting them could reach the "
                         "branch they point to; nothing changed: remove each one and run kankan destroy again")
DESTROY_SYMBOLIC_LINE = "  git update-ref --no-deref -d {ref}"
DESTROY_BOARD_CHANGED_AFTER = ("the board changed since the inventory (renamed, or its kankan-registry block changed); "
                               "nothing changed: run kankan destroy again")
DESTROY_LOCAL_CHANGED = ("{path} changed since the inventory (new work at risk, or a worktree added, removed or "
                         "replaced); nothing changed: run kankan destroy again")
DESTROY_TRELLO_FAILED = ("a Trello call failed during destruction ({step}: {error}); nothing local was removed: run "
                         "kankan destroy again")
DESTROY_WORKTREE_FAILED = ("cannot remove the worktree {path} ({error}); the refs and the local state are kept: fix it "
                           "and run kankan destroy again")
DESTROY_REFS_CHANGED = ("refs changed since the inventory ({error}); they are kept, as is the local state: run kankan "
                        "destroy again")
DESTROY_REMOVE_FAILED = "cannot remove {path} ({error}); fix it and run kankan destroy again"
DESTROY_PARTIAL_DONE = "Done so far:"
DESTROY_PARTIAL_LEFT = "Left:"

# kankan destroy: inventory (destroy spec F§3.2-3.3); `describe`
DESTROY_TITLE = "kankan destroy will:"
DESTROY_BOARD_LINE = "Board {url}"
DESTROY_BOARD_CARDS = "  delete {count} cards ({lists}), archived cards included"
DESTROY_BOARD_NO_CARDS = "  delete no card (kankan's lists are already empty)"
DESTROY_BOARD_SEAT_LISTS = "  archive {count} seat lists: {names}"
DESTROY_BOARD_LABELS = "  delete {count} Resume labels: {names}"
DESTROY_BOARD_REGISTRY = "  remove the kankan-registry block of the board description (the rest is kept)"
DESTROY_BOARD_NO_REGISTRY = "  the kankan-registry block is already gone"
DESTROY_BOARD_KEEPS = "  keep, empty: the lists {names} and the labels HOLD, Question, Error"
DESTROY_BOARD_KEEPS_LABEL = "  keep the label {name}: a card outside kankan's lists carries it"
DESTROY_SKIP_NO_CONFIG = "Board: none known (no kankan configuration); only the local state is removed"
DESTROY_SKIP_OTHER_CLONE = "Board: left alone, another clone uses it; only the local state is removed"
DESTROY_SKIP_UNREADABLE = ("Board: cannot be read ({status}); only the local state is removed. If the board still "
                           "exists, clean it by hand: kankan's cards, the seat lists, only the labels named exactly "
                           "`Resume YYYY-MM-DD hh:mm` that no card outside kankan's lists carries (archived cards "
                           "included), and the kankan-registry block in the board description")
DESTROY_WORKTREE = "  remove the worktree {path} ({head}){ignored}"
DESTROY_WORKTREE_MISSING = "  unregister the worktree {path} (its directory is already gone)"
DESTROY_WORKTREE_IGNORED = "; {count} ignored files deleted with it, e.g. {examples}"
DESTROY_WORKTREE_KEPT = "  keep {path} (not kankan's)"
DESTROY_HEAD_BRANCH = "branch {branch}"
DESTROY_HEAD_DETACHED = "detached HEAD"
DESTROY_REFS = "  delete {count} agent refs ({cards} card refs, {seats} seat refs)"
DESTROY_HOOK_REMOVE = "  remove kankan's pre-push hook {path}"
DESTROY_HOOK_KEEP = "  keep the pre-push hook {path} (not kankan's exact hook)"
DESTROY_GIT_CONFIG = "  remove git config kankan.integrationBranch ({value})"
DESTROY_EXCLUDE = "  remove the lines '# kankan' and '/.kankan/' from {path}"
DESTROY_STATE = "  delete the project state in {path} ({count} entries; kankan's lock files stay, empty)"
DESTROY_KEEPS_BRANCHES = "  keep every git branch, the credentials, ~/.config/kankan/ and ~/.cache/kankan/"

# kankan destroy: work at risk (destroy spec F§3.3)
DESTROY_RISK_TITLE = "Work at risk, lost with the worktrees:"
DESTROY_RISK_DIRTY = "Uncommitted work in {path} ({head}): {files}"
DESTROY_RISK_OPERATION = "A {operation} in progress in {path} ({head})"
DESTROY_RISK_UNREACHABLE = "Commits no branch reaches in {path} (detached HEAD): {commits}"
DESTROY_RISK_MISSING = ("The worktree {path} is gone, but {admin} still records {what}")
DESTROY_RISK_MISSING_OPERATION = "a {operation} in progress"
DESTROY_RISK_MISSING_COMMITS = "commits no branch reaches: {commits}"
DESTROY_RISK_MORE = " (and {count} more)"
DESTROY_RISK_INSPECT = "  inspect:"
DESTROY_RISK_REVERT = "  revert:"
DESTROY_RISK_ACCEPT = "  accept:"
DESTROY_RISK_BLOCK = "Interrupted kankan block {short}{seat}: {snapshot}; files: {files}"
DESTROY_RISK_BLOCK_SEAT = " (seat {seat})"
DESTROY_RISK_BLOCK_SNAPSHOT = "snapshot {path}"
DESTROY_RISK_BLOCK_NO_SNAPSHOT = "no snapshot yet"
DESTROY_RISK_BLOCK_FILES_LINK = "<symlink: files not listed>"
DESTROY_RISK_BLOCK_LAUNCH = ("  complete it: {command} (the launcher restores the work in the blocked-{short} "
                             "worktree), then kankan stop and kankan destroy again")

# kankan destroy: progress and report (destroy spec F§3.6-3.7)
DESTROY_PROGRESS_CARDS = "deleted {done} of {total} cards"
DESTROY_STEP_BOARD = ("board: {cards} cards deleted, {lists} seat lists archived, {labels} Resume labels deleted, "
                      "registry block {registry}")
DESTROY_STEP_WORKTREES = "worktrees removed: {count}"
DESTROY_STEP_REFS = "agent refs deleted: {count}"
DESTROY_STEP_GIT = "pre-push hook: {hook}; git config entry removed: {config}; exclude lines removed: {exclude}"
DESTROY_STEP_STATE = "project state removed: {path}"
DESTROY_REPORT_CANCELLED = "cancelled: nothing changed"
DESTROY_REPORT_NOTHING = "nothing to destroy: kankan has no state, worktree, ref, hook or board item left here"
DESTROY_REPORT_BOARD = "Board {url}: {cards} cards deleted{per_list}; seat lists archived: {lists}; Resume labels deleted: {labels}; registry block {registry}"
DESTROY_REPORT_WORKTREES = "Worktrees removed: {paths}"
DESTROY_REPORT_REFS = "Agent refs deleted: {count}"
DESTROY_REPORT_HOOK = "pre-push hook: {hook}"
DESTROY_REPORT_GIT_CONFIG = "git config kankan.integrationBranch removed: {value}"
DESTROY_REPORT_EXCLUDE = "exclude lines removed: {value}"
DESTROY_REPORT_STATE = "Project state removed: {value}"
DESTROY_REPORT_KEPT = "Kept on purpose:"
DESTROY_REPORT_KEPT_LIST = "the list {name} (empty; the next kankan init adopts it by name)"
DESTROY_REPORT_KEPT_LIST_RENAMED = ("the list {name} (empty; renamed from {default}: the next kankan init does not find "
                                    "it unless you rename it back to {default})")
DESTROY_REPORT_KEPT_LABELS = "the labels HOLD, Question and Error"
DESTROY_REPORT_KEPT_RESUME = "the label {name} (a card outside kankan's lists carries it)"
DESTROY_REPORT_KEPT_WORKTREE = "{path} (not kankan's)"
DESTROY_REPORT_KEPT_HOOK = "the pre-push hook {path} (not kankan's exact hook)"
DESTROY_REPORT_KEPT_LOCKS = "kankan's lock files in {path} (locks are never deleted)"
DESTROY_REPORT_KEPT_USER = ("every git branch, the credentials, ~/.config/kankan/ and ~/.cache/kankan/ (shared with "
                            "other projects)")
DESTROY_REPORT_OTHER_CLONE = "The board was left alone: another clone uses it."
DESTROY_REPORT_NO_CONFIG = "No board is known (no kankan configuration): only the local state was removed."
DESTROY_REPORT_MANUAL = ("The board could not be read ({status}) and was not touched. If it still exists, clean it by "
                         "hand: kankan's cards, the seat lists, only the labels named exactly `Resume YYYY-MM-DD hh:mm` "
                         "that no card outside kankan's lists carries (archived cards included), and the "
                         "kankan-registry block in the board description.")
DESTROY_REPORT_NEXT = "Next: {command}"
DESTROY_INTEGRATION_KEPT = ("The integration branch `{name}` is kept ({where}). The next `kankan init` will reuse it, "
                            "with all the work merged so far.")
DESTROY_INTEGRATION_KEPT_USER = "The integration branch `{name}` is kept ({where}), being your branch too."
DESTROY_INTEGRATION_NOT_DEFAULT = ("The next `kankan init` proposes `kankan-agents`: type `{name}` to reuse this "
                                   "branch.")
DESTROY_INTEGRATION_CLEAN = ("To start from a clean branch instead (kankan destroy runs none of this): answer `kankan "
                             "init`'s integration branch question with a new name (e.g. `{name}-2`), which init creates "
                             "from your branch")
DESTROY_INTEGRATION_RENAME = "; or rename the old one: {commands}"
DESTROY_INTEGRATION_HOOK_GONE = " (kankan's hook that guards pushes to it is already removed)"
DESTROY_WHERE_LOCAL = "local"
DESTROY_WHERE_ORIGIN = "on `origin`"
DESTROY_WHERE_BOTH = "local and on `origin`"
DESTROY_WHERE_LOCAL_UNKNOWN = "local; on `origin`: unknown"
DESTROY_WHERE_UNKNOWN = "on `origin`: unknown"
DESTROY_CLONE_LINE = "In this clone ({path}):"
DESTROY_LEFT_CARDS = "{count} cards to delete"
DESTROY_LEFT_LISTS = "seat lists to archive: {names}"
DESTROY_LEFT_LABELS = "Resume labels to delete: {names}"
DESTROY_LEFT_REGISTRY = "the kankan-registry block of the board description"
DESTROY_LEFT_WORKTREES = "worktrees to remove: {paths}"
DESTROY_LEFT_REFS = "{count} agent refs to delete"
DESTROY_LEFT_LOCAL = ("kankan's pre-push hook, git configuration entry and exclude lines (where present), and the "
                      "project state, configuration included")
DESTROY_LEFT_STATE = "the project state, configuration included"
DESTROY_RISK_MISSING_BRANCH = "  accept:\n    git -C {repo} branch <name> {tip}"
DESTROY_LOG_UNSAFE = ("{path} is not a regular file with a single link (a symbolic or hard link, a directory or a "
                      "FIFO); kankan destroy would "
                      "write its audit record through it: remove it with `rm {quoted}` (no trailing slash; what a link "
                      "points to stays in place) and run kankan destroy again")
DESTROY_REFS_FAILED = ("cannot run git update-ref ({error}); the refs and the local state are kept: fix it and run "
                       "kankan destroy again")
TOO_MANY_ARCHIVED_CARDS = ("the board has too many archived cards to inventory safely ({limit} or more); nothing changed: "
                           "delete or restore some archived cards in Trello and run kankan destroy again")
DESTROY_BOARD_NO_SHORT_LINK = ("neither Trello nor the configuration gives a valid short link for the board, so kankan "
                               "destroy cannot show which board it is; nothing changed: check the board in Trello and "
                               "run kankan destroy again")
DESTROY_CONFIRM_NOTHING = ("--confirm value {index} answers no question: there is nothing to destroy here; nothing "
                           "changed")
DESTROY_SHOWN_YES = "yes"
DESTROY_SHOWN_NO = "no"
DESTROY_SHOWN_NONE = "none"
DESTROY_SHOWN_NOT_SET = "not set"
DESTROY_SHOWN_NONE_FOUND = "none found"
DESTROY_SHOWN_REMOVED = "removed"
DESTROY_SHOWN_NOT_REMOVED = "not removed"
DESTROY_SHOWN_ALREADY_ABSENT = "already absent"
DESTROY_SHOWN_HOOK = {"removed": "removed", "foreign, kept": "kept (not kankan's exact hook)",
                      "shared, kept": "kept (kankan's, in a shared hooks directory)", "absent": "none"}
DESTROY_LOCAL_CHANGED_DURING = ("{path} gained work at risk during kankan destroy and was not removed; the refs and the "
                                "local state are kept: deal with that work and run kankan destroy again")
DESTROY_LEFT_ROOT = "the worktree root {path} (empty)"
DESTROY_RISK_SUBMODULES = ("Submodules checked out in {path} ({head}): {paths}; their own work goes with the worktree "
                           "(git removes such a worktree only when forced)")

# kankan destroy: final review fixes (destroy spec F§5, T§3-§4)
DESTROY_USAGE = ("kankan destroy: invalid arguments; usage: kankan destroy [--json] [--confirm PHRASE]...; each "
                 "--confirm phrase must be quoted as one argument (e.g. --confirm 'destroy <board name>')")
DESTROY_STATE_NOT_DIR = ("{path} is not a directory; kankan destroy does not read or remove it: move it aside by hand and "
                         "run kankan destroy again")
DESTROY_PATH_UNSAFE = ("{path} is a symbolic link or not a regular file; kankan destroy would open it: remove it with "
                       "`rm {quoted}` (no trailing slash; what a link points to stays in place) and run kankan destroy "
                       "again")
DESTROY_LOCK_UNSAFE = ("cannot open the project lock {path} safely ({error}); nothing changed: if it is a symbolic link "
                       "or not a regular file, remove it with `rm {quoted}` and run kankan destroy again")
DESTROY_ROOT_OVERLAP = ("the worktree root {root} and kankan's state directory {state} overlap (one is inside the "
                        "other), so removing the state could remove worktrees kankan keeps; nothing changed: first move "
                        "every worktree and every file of yours out of {state} (`git worktree move <path> <new path>`, "
                        "`mv`), then set worktree_root in {config} to a directory outside {state} (and not containing "
                        "it), or clean kankan's state by hand")
DESTROY_WORKTREE_IN_STATE = ("the git worktree {path} is inside kankan's state directory {state}, which kankan destroy "
                             "deletes; nothing changed: move it out first with `git worktree move {quoted} <new path>` "
                             "and run kankan destroy again")
DESTROY_CONFIG_CHANGED = ("kankan's configuration changed since the inventory (the board binding, the paths, the lists "
                          "or the seats); nothing changed: run kankan destroy again")
DESTROY_REFS_CHANGED_BEFORE = ("the agent refs changed since the inventory; nothing changed: run kankan destroy again")
DESTROY_BLOCKS_CHANGED = ("an interrupted kankan block appeared or changed since the inventory ({path}); nothing "
                          "changed: run kankan destroy again to see it")
DESTROY_MISSING_INDEX_UNSAFE = ("{path} is not a regular file, so the staged work of the missing worktree cannot be "
                                "checked; nothing changed: repair or remove it and run kankan destroy again")
DESTROY_RISK_MISSING_STAGED = "staged changes no commit holds ({index}): {files}"
DESTROY_RISK_MISSING_MODULES = "submodule repositories in {path}, which can hold commits no branch reaches"
DESTROY_RISK_MISSING_INSPECT = "  inspect:\n    GIT_INDEX_FILE={index} git -C {repo} diff --cached {rev}"
DESTROY_BOARD_REREAD_LOST = ("the board can no longer be read as this clone's ({reason}) during destruction; the "
                             "registry block and the local state are kept: run kankan destroy again")
DESTROY_BOARD_CARDS_REMAIN = ("{count} cards are in kankan's lists again (added or moved there during kankan destroy); "
                              "the registry block and the local state are kept: run kankan destroy again")
DESTROY_LEFT_CARDS_REMAIN = "{count} cards that arrived in kankan's lists during the run"
DESTROY_REPORT_SKIPPED = ("{count} card(s) left kankan's lists during the run and were not deleted (they are where the "
                          "user moved them)")
DESTROY_REPORT_LABELS_KEPT = ("Resume labels kept because a card outside kankan's lists carries them now (or they are "
                              "gone): {names}")
DESTROY_HOOK_KEEP_SHARED = ("  keep kankan's pre-push hook {path}: it is in a hooks directory shared with other "
                            "repositories (core.hooksPath outside this repository's own configuration)")
DESTROY_REPORT_KEPT_HOOK_SHARED = ("kankan's pre-push hook {path} (a shared hooks directory: other repositories may use "
                                   "it)")
DESTROY_INTEGRATION_HOOK_NOTE_SHARED = ("note: kankan's pre-push hook {path} is in a shared hooks directory and is kept; "
                                        "it may refuse these pushes")
