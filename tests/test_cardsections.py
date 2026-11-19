"""Agent sections in card descriptions (card instructions F§2, T§2)."""

from kankan.cardsections import fingerprint, for_seat, parse, problems_text

TYPES = ("claude", "codex")

EXAMPLE = ("Implement feature X: … (the specification)\n"
           "## agent: claude review\n"
           "Consult your advisor before `kankan integrate`.\n"
           "## agent: all implementation\n"
           "Write the commit messages in Italian.\n")


def test_no_heading_spec_is_whole_input():
    parsed = parse("a\r\nb  \n", TYPES)
    assert parsed.spec == "a\r\nb  \n"
    assert parsed.sections == () and parsed.valid


def test_example_routing():
    parsed = parse(EXAMPLE, TYPES)
    assert parsed.valid
    assert parsed.spec == "Implement feature X: … (the specification)\n"
    assert for_seat(parsed, "claude", "review") == "## agent: claude review\nConsult your advisor before `kankan integrate`."
    assert for_seat(parsed, "codex", "review") is None
    for agent_type in TYPES:
        assert for_seat(parsed, agent_type, "implementation") == "## agent: all implementation\nWrite the commit messages in Italian."


def test_order_and_specificity_inputs():
    desc = ("S\n## agent: all\nA\n## agent: claude review\nCR\n## agent: claude\nC\n## agent: all review\nAR\n"
            "## agent: codex\nX\n## agent: claude implementation\nCI\n")
    parsed = parse(desc, TYPES)
    assert for_seat(parsed, "claude", "review") == (
        "## agent: all\nA\n\n## agent: claude review\nCR\n\n## agent: claude\nC\n\n## agent: all review\nAR")


def test_case_spacing_closing_hashes():
    parsed = parse("S\n   ### Agent:  Claude  Review ##  \nbody\n", TYPES)
    assert parsed.valid
    section = parsed.sections[0]
    assert (section.target, section.phase) == ("claude", "review")
    assert for_seat(parsed, "claude", "review") == "   ### Agent:  Claude  Review ##\nbody"


def test_other_headings_are_text():
    parsed = parse("S\n## Tests\nrun them\n## agent: claude\n### Style\nshort\n", TYPES)
    assert parsed.spec == "S\n## Tests\nrun them\n"
    assert for_seat(parsed, "claude", "review") == "## agent: claude\n### Style\nshort"


def test_fenced_heading_is_text():
    backtick = "S\n```\n## agent: cluade\n```\n"
    tilde = "S\n~~~~\n## agent: cluade\n~~~~\n"
    nested = "S\n````\n```\n## agent: cluade\n```\n````\n"
    unclosed = "S\n```\ncode\n## agent: claude\nbody\n"
    for desc in (backtick, tilde, nested, unclosed):
        parsed = parse(desc, TYPES)
        assert parsed.valid and parsed.sections == () and parsed.spec == desc, desc
    after = parse(backtick + "## agent: claude\nx\n", TYPES)
    assert after.spec == backtick and for_seat(after, "claude", "review") == "## agent: claude\nx"


def test_four_space_indent_is_not_a_heading():
    desc = "S\n    ## agent: cluade\n"
    parsed = parse(desc, TYPES)
    assert parsed.valid and parsed.spec == desc and parsed.sections == ()


def test_problems_collected():
    desc = ("S\n## agent:\n## agent: cluade\n## agent: claude deploy\n## agent: claude review now\n"
            "## agent: claude review\nok\n## agent: Claude review\n")
    parsed = parse(desc, TYPES)
    assert not parsed.valid
    assert [p.line for p in parsed.problems] == [2, 3, 4, 5, 8]
    assert "cluade" in parsed.problems[1].reason
    assert "deploy" in parsed.problems[2].reason
    assert parsed.spec == "S\n"


def test_problem_reasons_never_contain_bodies():
    parsed = parse("S\n## agent: cluade\nsecret body\n## agent: codex\nother secret\n", TYPES)
    assert "secret" not in problems_text(parsed.problems)
    assert problems_text(parsed.problems).startswith("line 2: ")


def test_heading_first_line_empty_spec():
    assert parse("## agent: claude\nbody\n", TYPES).spec == ""


def test_empty_body_delivers_heading():
    parsed = parse("S\n## agent: claude\n\n## agent: codex\nx\n", TYPES)
    assert for_seat(parsed, "claude", "review") == "## agent: claude"


def test_editor_blank_lines():
    parsed = parse("S\n\n## agent: claude\n\nbody\n\n", TYPES)
    assert parsed.spec == "S\n\n"
    assert for_seat(parsed, "claude", "review") == "## agent: claude\nbody"


def test_crlf_sections_rendered_lf():
    parsed = parse("S\r\n## agent: claude\r\n  line one\r\nline two\r\n", TYPES)
    assert parsed.spec == "S\r\n"
    assert for_seat(parsed, "claude", "review") == "## agent: claude\n  line one\nline two"


def test_fingerprint():
    assert fingerprint("a\r\nb", TYPES) == fingerprint("a\nb", TYPES) == fingerprint("a\rb", ("codex", "claude"))
    assert fingerprint("a\nb", TYPES) != fingerprint("a\nb", ("claude",))
    assert len(fingerprint("x", TYPES)) == 12


def test_long_token_quoted_short():
    parsed = parse("S\n## agent: " + "x" * 2000 + "\n", TYPES)
    assert len(parsed.problems[0].reason) < 80 and "…" in parsed.problems[0].reason
