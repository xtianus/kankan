"""Who kankan notifies and whose comments it forwards (spec §14.2, §14.3).

kankan acts through its own Trello account (the token owner). It mentions the human behind a card: the card's
creator, or the board's admins when kankan itself created the card (or the creator is unknown). Every comment not
written by kankan's account is an answer for the agent.
"""

from __future__ import annotations

from typing import List, Set, Tuple

from kankan.errors import KankanError


def kankan_member_id(project) -> str:
    """The id of kankan's own Trello account (recorded by init; read from Trello for older configurations)."""
    if not project.cfg.kankan_member_id:
        me = project.trello.me()
        project.cfg.kankan_member_id, project.cfg.kankan_username = me["id"], me["username"]
    return project.cfg.kankan_member_id


def recipients(project, card_id: str) -> List[Tuple[str, str]]:
    """(id, username) of the people to notify about a card."""
    me = kankan_member_id(project)
    creator = project.trello.card_creator(card_id)
    if creator and creator[0] != me:
        return [creator]
    return [a for a in project.trello.board_admins(project.cfg.board_id) if a[0] != me]


def mention(project, card_id: str) -> str:
    """``@user`` mentions for a card ('' if nobody can be found: the comment is still written)."""
    try:
        people = recipients(project, card_id)
    except KankanError as err:
        project.log("mention_failed", card=card_id, error=err.message)
        return ""
    return " ".join(f"@{username}" for _, username in people if username)


def input_members(project) -> Set[str]:
    """Ids of the board members whose comments and card attachments reach the agent: every member but kankan's
    own account (spec §13.3, §14.3)."""
    members = {member_id for member_id, _, _ in project.trello.board_members(project.cfg.board_id)}
    return members - {kankan_member_id(project)}


def answers(project, comments) -> list:
    """Comments the agent should receive: written by a board member other than kankan's account, without kankan's
    marker. Comments by people outside the board are never forwarded (open commenting would let anyone type into
    the agent's terminal)."""
    from kankan.trello import MARKER_PREFIX
    candidates = [c for c in comments if c.member_id != kankan_member_id(project) and f"`{MARKER_PREFIX}" not in c.text]
    if not candidates:
        return []
    members = input_members(project)
    return [c for c in candidates if c.member_id in members]
