# Project: specification, documents, conventions

## Specification and documents

- The specification of kankan, the CLI that coordinates Claude Code and Codex agents through a Trello board, is the set of files in `aispecs/`. The initial pair [`aispecs/initial_spec_functional.md`](../aispecs/initial_spec_functional.md) (what users and agents observe) and [`aispecs/initial_spec_technical.md`](../aispecs/initial_spec_technical.md) (how kankan implements it) share one section numbering; a "spec §n" reference points to whichever file holds section n. Each feature has its own pair `aispecs/<feature>_spec_{functional,technical}.md` with its own numbering and a chapter stating how it changes the initial behaviour; feature pairs are never merged into the initial files, and their status lines say whether they are implemented. The current feature list is in [`AGENTS.md`](../AGENTS.md); proposals not to be implemented until a need is found are in `aispecs/proposals/`.
- [`aispecs/kanban_requirements.md`](../aispecs/kanban_requirements.md) lists the Kanban API capabilities kankan needs, mapped to the Trello endpoints it uses. It is the reference for anyone writing an adapter for a Trello alternative.
- Implementation plans live in `aiplans/`. The user manual is [`docs/user-manual.md`](../docs/user-manual.md), the developer manual (tests, live Trello test board) is [`docs/developer-manual.md`](../docs/developer-manual.md); observed agent screens and behaviours are in [`docs/adapter-notes.md`](../docs/adapter-notes.md).

## Conventions

- Python code uses 4-space indentation (PEP 8), overriding the global tabs rule.
- Everything is in English: code, comments, specs, plans, user manual and user-facing messages. User-facing texts live in `kankan/messages.py`.
- Runtime code is standard library only, Python ≥ 3.10 (no `datetime.UTC`, `StrEnum`, `tomllib`...).

## Rules

- Every commit that adds, changes or removes something kankan requires from the board (a Trello endpoint call, or behaviour of the board product outside the API that kankan relies on, such as how its editor stores what users type) revises [`aispecs/kanban_requirements.md`](../aispecs/kanban_requirements.md) in the same commit, so that its capability tables and endpoint mappings match the code.
- [`kankan/VERSION`](../kankan/VERSION) is incremented in each commit that changes the installed program or its packaging ([version specification](../aispecs/version_spec_functional.md)).
