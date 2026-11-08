# kankan — agent instructions

- Specification: `aispecs/kankan_spec.md`; Kanban backend requirements: `aispecs/kanban_requirements.md`.
- Implementation plans: `aiplans/`. Project knowledge base: `aiknowledge/notes.md`.
- Python code uses 4-space indentation (PEP 8), overriding the global tabs rule (see `.editorconfig`).
- Runtime code uses the Python standard library only (Python >= 3.10); pytest is a dev dependency.
- Run tests with `python -m pytest -q` (live Trello tests: `-m trello_live`).
- Every commit that adds, changes or removes a Trello endpoint call revises `aispecs/kanban_requirements.md` in the same commit.
