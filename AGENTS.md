# kankan — agent instructions

- Always update the specification before writing a plan for a new feature. Specification: `aispecs/kankan_spec_functional.md` (what users and agents observe) and `aispecs/kankan_spec_technical.md` (how kankan implements it); both share one section numbering.
- Kanban backend requirements: `aispecs/kanban_requirements.md`.
- Implementation plans: `aiplans/`. Project knowledge base: `aiknowledge/notes.md`.
- Python code uses 4-space indentation (PEP 8), overriding the global tabs rule (see `.editorconfig`).
- Runtime code uses the Python standard library only (Python >= 3.10); pytest is a dev dependency.
- Every commit that adds, changes or removes a Trello endpoint call revises `aispecs/kanban_requirements.md` in the same commit.
