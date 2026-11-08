"""Card dependencies through Trello card attachments (spec §9.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from kankan.errors import KankanError
from kankan.trello import Card, Snapshot

_CARD_URL = re.compile(r"^https://trello\.com/c/([A-Za-z0-9]{8})(/.*)?$")


def attachment_short(url: str) -> Optional[str]:
    match = _CARD_URL.match(url or "")
    return match.group(1) if match else None


def dependencies(card: Card) -> List[str]:
    result = []
    for url in card.attachments:
        short = attachment_short(url)
        if short and short != card.short and short not in result:
            result.append(short)
    return result


@dataclass
class DepStatus:
    ok: bool
    waiting: List[str] = field(default_factory=list)
    problems: List[Tuple[str, str]] = field(default_factory=list)


def dep_status(project, snap: Snapshot, card: Card, fetch: Optional[Callable[[str], Optional[Card]]] = None
               ) -> DepStatus:
    """Satisfied only if every attached card is in this board's Done; missing, other-board,
    archived-outside-Done, unreadable or cyclic dependencies make the card ineligible."""
    fetch = fetch or project.trello.card
    done = project.cfg.done
    by_short: Dict[str, Optional[Card]] = {c.short: c for c in snap.cards.values()}

    def lookup(short: str) -> Optional[Card]:
        if short not in by_short:
            by_short[short] = fetch(short)
        return by_short[short]

    status = DepStatus(ok=True)
    for short in dependencies(card):
        try:
            dep = lookup(short)
        except KankanError as err:
            status.problems.append((short, f"unreadable: {err.status.value}"))
            continue
        if dep is None:
            status.problems.append((short, "missing"))
        elif dep.board_id and dep.board_id != snap.board_id:
            status.problems.append((short, "other_board"))
        elif dep.list_id == done:
            continue
        elif dep.closed:
            status.problems.append((short, "archived_not_done"))
        else:
            status.waiting.append(short)
    cycle = _find_cycle(card, lookup)
    if cycle:
        status.problems.append((cycle, "cycle"))
    status.ok = not status.waiting and not status.problems
    return status


def _find_cycle(start: Card, lookup) -> Optional[str]:
    """DFS over the dependency graph; returns a shortLink on a cycle through ``start``'s graph."""
    on_stack, finished = set(), set()

    def visit(card: Card) -> Optional[str]:
        on_stack.add(card.short)
        for short in dependencies(card):
            if short in on_stack:
                return short
            if short in finished:
                continue
            try:
                dep = lookup(short)
            except KankanError:
                continue
            if dep is not None and dep.list_id is not None:
                found = visit(dep)
                if found:
                    return found
        on_stack.discard(card.short)
        finished.add(card.short)
        return None

    return visit(start)
