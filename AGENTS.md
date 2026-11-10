# kankan — agent instructions

- Always update the specification before writing a plan for a new feature. The specification is the set of files in `aispecs/`: the initial pair `initial_spec_functional.md` (what users and agents observe) and `initial_spec_technical.md` (how kankan implements it), which share one section numbering, plus one pair per feature, `<feature>_spec_functional.md` and `<feature>_spec_technical.md`.
- A new feature gets its own pair, with its own numbering and a chapter stating how it changes the initial behaviour (by initial section number); it is planned from its pair. A feature's pair stays its specification after implementation and is never merged into the initial files; its status line says whether it is implemented. Current features: `boardsettings_spec_*` (board settings, model and effort; implemented locally, with release observation gates in its status).
- Kanban backend requirements: `aispecs/kanban_requirements.md`.
- Implementation plans: `aiplans/`. Project knowledge base: `aiknowledge/notes.md`.
- Python code uses 4-space indentation (PEP 8), overriding the global tabs rule (see `.editorconfig`).
- Runtime code uses the Python standard library only (Python >= 3.10); pytest is a dev dependency.
- Every commit that adds, changes or removes a Trello endpoint call revises `aispecs/kanban_requirements.md` in the same commit.
