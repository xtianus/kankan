# kankan

kankan coordinates AI coding agents (Claude Code and Codex CLI) working in parallel on one Git repository, taking
tasks from a Trello board: one agent implements a card, an agent of a different type reviews it, integrates it into
`main` and closes it. Ownership lives in Git refs changed only by compare-and-swap; Trello expresses human intent.

```bash
pipx install .            # Python ≥ 3.10, standard library only
kankan skill install
cd ~/coding/myproject
kankan init https://trello.com/b/<shortLink>/<name>
kankan up claude=2 codex=1
```

- User manual: [docs/manual.md](docs/manual.md)
- Specification: [aispecs/kankan_spec.md](aispecs/kankan_spec.md)
- Kanban backend requirements: [aispecs/kanban_requirements.md](aispecs/kanban_requirements.md)
- Adapter observations: [docs/adapter-notes.md](docs/adapter-notes.md)
- Implementation plan: [aiplans/2026-11-08-kankan.md](aiplans/2026-11-08-kankan.md)

Development: `python -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/python -m pytest -q`
(tests marked `tmux` need tmux; `-m trello_live` runs against a real test board, see the manual).

## License

MIT, see [LICENSE](LICENSE).
