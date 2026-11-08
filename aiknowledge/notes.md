# Project notes

## Specifications

- `aispecs/kankan_spec.md` is the specification of kankan, the CLI that coordinates Claude Code and Codex agents through a Trello board.
- `aispecs/kanban_requirements.md` lists the Kanban API capabilities kankan needs, mapped to the Trello endpoints it uses. It is the reference for anyone writing an adapter for a Trello alternative.
- Implementation plans live in `aiplans/`.

## Conventions

- Python code uses 4-space indentation (PEP 8), overriding the global tabs rule.
- Everything is in English: code, comments, specs, plans, user manual and user-facing messages.

## Rules

- Before every commit that adds a call to a Trello API endpoint, changes how an endpoint is used, or stops using one, revise `aispecs/kanban_requirements.md` so that its capability tables and endpoint mappings match the code. The commit must include that revision.
