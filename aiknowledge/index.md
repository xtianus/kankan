# kankan knowledge base

kankan is a CLI that coordinates Claude Code and Codex agents through a Trello board. These notes describe how the project is and works; read the file for the area you touch.

- [project.md](project.md): where the specification, plans and manuals are, conventions, commit rules.
- [architecture.md](architecture.md): module map, branches, processes and seats, code identity and stopping, card transitions, attachments.
- [trello.md](trello.md): observed Trello API behaviour and how kankan's client and bot account use it.
- [testing.md](testing.md): running the local suite (in parallel), markers, fakes, crash points, speed, rules for writing tests.
- [live-tests.md](live-tests.md): live Trello tests, cleanup, the live agents run, the pre-publishing check.
- [boardsettings.md](boardsettings.md): board settings (model and effort) evaluation, locks, model checks.
- [questions.md](questions.md): the Q&A seat, conversations, turns, locks, seat replacement.
- [cardinstructions.md](cardinstructions.md): agent sections in card descriptions and the `Instructions` card.
- [destroy.md](destroy.md): `kankan destroy` safety rules and re-check boundaries.
