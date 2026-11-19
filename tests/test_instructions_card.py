"""The `Instructions` help card of the `kankan` list (card instructions F§6, T§6)."""

from kankan import boardsettings, settings_setup
from kankan.cardsections import INSTRUCTIONS_TITLE, help_text, parse

TYPES = ["claude", "codex"]
FOOTER = "This card is maintained by `kankan doctor`: edits to it are overwritten."


def _help_cards(project):
    read = project.trello.settings_cards(project.reload_config().settings_list)
    return [card for card in read.cards if card.name == INSTRUCTIONS_TITLE]


def _ensure(project):
    return settings_setup.ensure(project, boardsettings.adapters_for(project.paths), say=lambda text: None)


def test_help_text_deterministic_and_complete():
    text = help_text(["codex", "claude"])
    assert text == help_text(TYPES)
    blocks = text.split("```")[1::2]
    headings = [line for block in blocks for line in block.splitlines() if line.startswith("## agent:")]
    for target in ("claude", "codex", "all"):
        for suffix in ("", " implementation", " review"):
            assert f"## agent: {target}{suffix}" in headings
    assert "never override" in text and "cluade" in text and text.rstrip().endswith(FOOTER)
    assert parse(text, TYPES).valid and parse(text, TYPES).sections == ()


def test_init_creates_card(kproject):
    reports = _ensure(kproject)
    cards = _help_cards(kproject)
    assert len(cards) == 1 and cards[0].desc == help_text(TYPES)
    assert "Instructions card created" in reports


def test_no_rewrite_when_unchanged(kproject, fake_trello):
    _ensure(kproject)
    card = _help_cards(kproject)[0]
    fake_trello.state["cards"][card.id]["desc"] = help_text(TYPES).replace("\n", "  \r\n") + "\n\n"
    before = len(fake_trello.requests)
    assert not any("Instructions" in r for r in _ensure(kproject))
    assert not [r for r in fake_trello.requests[before:] if r[0] == "PUT" and card.id in r[1]]


def test_doctor_rewrites_after_user_edit_and_new_type(kproject, fake_trello, monkeypatch):
    _ensure(kproject)
    card = _help_cards(kproject)[0]
    fake_trello.state["cards"][card.id]["desc"] = "my notes"
    reports = _ensure(kproject)
    assert "Instructions card updated (agent types: claude, codex)" in reports
    assert _help_cards(kproject)[0].desc == help_text(TYPES)
    monkeypatch.setattr(settings_setup, "adapter_types", lambda paths=None: ["claude", "codex", "gemini"])
    reports = _ensure(kproject)
    assert "Instructions card updated (agent types: claude, codex, gemini)" in reports
    assert "## agent: gemini review" in _help_cards(kproject)[0].desc


def test_doctor_no_fix_reports(kproject, fake_trello):
    _ensure(kproject)
    card = _help_cards(kproject)[0]
    assert settings_setup.ensure_instructions_card(kproject, TYPES, write=False) is None
    fake_trello.state["cards"][card.id]["desc"] = "edited"
    assert settings_setup.ensure_instructions_card(kproject, TYPES, write=False) == "Instructions card outdated"
    assert _help_cards(kproject)[0].desc == "edited"
    del fake_trello.state["cards"][card.id]
    assert settings_setup.ensure_instructions_card(kproject, TYPES, write=False) == "Instructions card missing"


def test_deleted_card_recreated(kproject, fake_trello):
    _ensure(kproject)
    del fake_trello.state["cards"][_help_cards(kproject)[0].id]
    _ensure(kproject)
    assert len(_help_cards(kproject)) == 1


def test_duplicates_not_written(kproject, fake_trello):
    _ensure(kproject)
    extra = fake_trello.add_card(kproject.cfg.settings_list, INSTRUCTIONS_TITLE, desc="mine")
    assert settings_setup.ensure_instructions_card(kproject, TYPES, write=True) is None
    assert fake_trello.state["cards"][extra["id"]]["desc"] == "mine"
    first = [c for c in _help_cards(kproject) if c.id != extra["id"]][0]
    del fake_trello.state["cards"][first.id]
    assert settings_setup.ensure_instructions_card(kproject, TYPES, write=True).startswith("Instructions card updated")
    assert fake_trello.state["cards"][extra["id"]]["desc"] == help_text(TYPES)
