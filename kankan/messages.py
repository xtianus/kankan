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
NO_SIGNAL = "kankan: the agent stopped without closing its task (no done/ask/fail/block/yield signal)."
UNKNOWN_SCREEN = "kankan: the agent's screen is not recognized; it needs a look (`kankan adapter check {seat}`)."
REGISTRY_LOST = "kankan: this board is now bound to another kankan clone; {seat} stops after the current task."
FORWARDED = "kankan: forwarded to the agent."
RESUMED = "kankan: resumed (answered from the terminal)."
LIMIT_COMMENT = "kankan: {seat} hit a usage limit; it resumes at {when}."
