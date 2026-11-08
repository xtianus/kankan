"""User-facing texts (Trello comments, CLI messages, agent prompts), in one place."""

PROMPT = "Run the current kankan task (kankan skill)."

SEAT_RECOVERED = "kankan: seat {seat} was recovered after its previous holder died; the seat keeps this card and work resumes."

CLAIMED = "kankan: {seat} started implementing this card on branch `{branch}`."
REVIEW_CLAIMED = "kankan: {seat} started reviewing this card (branch `{branch}`, implemented by {implementer})."
BRANCH_CONFLICT = "kankan: more than one branch exists for this card ({branches}); delete the extra ones, then remove the Error label."
HOLD_AFTER_CLAIM = "kankan: HOLD found right after the claim; the card goes back to the Backlog."
IMPLEMENTED = "kankan: {seat} finished the implementation on `{branch}`; it waits for a review."
YIELDED = "kankan: {seat} stopped working on this card (yield); the work is on `{branch}`."
REVIEW_YIELDED = "kankan: {seat} stopped reviewing this card (yield); it goes back to the implementer's list."

INSTRUCTIONS = {
    "impl": "Implement the card on the prepared branch in your worktree, commit, run the tests, then run `kankan done --json`.",
    "review": ("Review the branch against main, fix problems with separate commits and summarize them with "
               "`kankan note`, then `kankan sync`, run the tests, `kankan verify --tested <hash>`, "
               "`kankan integrate --tested <hash>` and finally `kankan done`."),
    "hold_wip": "The card is on HOLD: commit your work in progress (WIP commit) and run `kankan yield --json`.",
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
MAIN_MOVED_LIMIT = "kankan: main kept moving during integration ({count} attempts); the card needs a look."
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
