"""Agent sections in a card's description (card instructions F§2, T§2).

A description is the card's specification, followed by agent sections that each start with an agent heading
(``## agent: <target> [<phase>]``). Pure functions only: no I/O.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Optional

PHASES = ("implementation", "review")
RESERVED = "all"

_CANDIDATE = re.compile(r"^ {0,3}#{1,6}[ \t]+agent:(.*)$", re.IGNORECASE)
_CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+$")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Problem:
    line: int
    heading: str
    reason: str


@dataclass(frozen=True)
class Section:
    target: str
    phase: Optional[str]
    heading: str
    body: str
    line: int


@dataclass(frozen=True)
class Parsed:
    spec: str
    sections: tuple
    problems: tuple

    @property
    def valid(self) -> bool:
        return not self.problems


def _render(heading: str, body_lines: list) -> str:
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    return "\n".join([heading] + body_lines)


def _target_phase(rest: str, types: set):
    """The (target, phase) of a candidate's text after ``agent:``, or the reason it is invalid."""
    rest = _CLOSING_HASHES.sub("", rest.rstrip())
    words = rest.lower().split()
    if not words:
        return None, "no target after `agent:`"
    if words[0] not in types | {RESERVED}:
        return None, f"`{words[0]}` is not an agent type"
    if len(words) > 1 and words[1] not in PHASES:
        return None, f"`{words[1]}` is not a phase"
    if len(words) > 2:
        return None, "unexpected words after the phase"
    return (words[0], words[1] if len(words) > 1 else None), None


def parse(desc: str, agent_types: Iterable[str]) -> Parsed:
    types = {name.lower() for name in agent_types}
    spec_end = None
    offset = 0
    fence = None  # (character, length) of the open fenced code block
    sections, problems, seen = [], [], {}
    current = None  # [target, phase, heading, body lines, line number] of the open section
    def close():
        if current:
            sections.append(Section(current[0], current[1], current[2], _render(current[2], current[3]), current[4]))
    for number, raw in enumerate(desc.splitlines(keepends=True), start=1):
        text = raw.rstrip("\r\n")
        start = offset
        offset += len(raw)
        if fence:
            if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*$", text):
                fence = None
            if current:
                current[3].append(text)
            continue
        opening = _FENCE_OPEN.match(text)
        if opening:
            fence = (opening.group(1)[0], len(opening.group(1)))
            if current:
                current[3].append(text)
            continue
        candidate = _CANDIDATE.match(text)
        if not candidate:
            if current:
                current[3].append(text)
            continue
        if spec_end is None:
            spec_end = start
        close()
        current = None
        heading = text.rstrip()
        scope, reason = _target_phase(candidate.group(1), types)
        if scope and scope in seen:
            reason = f"duplicate heading (first at line {seen[scope]})"
        if reason:
            problems.append(Problem(number, heading.strip(), reason))
            continue
        seen[scope] = number
        current = [scope[0], scope[1], heading, [], number]
    close()
    return Parsed(desc if spec_end is None else desc[:spec_end], tuple(sections), tuple(problems))


def for_seat(parsed: Parsed, agent_type: str, phase: str) -> Optional[str]:
    """The sections for a seat of ``agent_type`` in ``phase``, in description order; None when none matches."""
    matching = [section.body for section in parsed.sections
                if section.target in (agent_type, RESERVED) and section.phase in (None, phase)]
    return "\n\n".join(matching) if matching else None


def fingerprint(desc: str, agent_types: Iterable[str]) -> str:
    normalized = desc.replace("\r\n", "\n").replace("\r", "\n")
    data = normalized + "\n\0" + ",".join(sorted(agent_types))
    return hashlib.sha1(data.encode()).hexdigest()[:12]


def problems_text(problems) -> str:
    return "; ".join(f"line {problem.line}: {problem.reason}" for problem in problems)
