"""Diagnostics and administration: status, claims, reconcile, release, seat recover/remove, purge-remote."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from kankan import clock, seats, workflow
from kankan.seatstate import load_state


def _age(created: str) -> int:
    try:
        then = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=clock.now().tzinfo)
    except (TypeError, ValueError):
        return -1
    return int((clock.now() - then).total_seconds())


def status(project) -> dict:
    cfg = project.reload_config()
    seat_rows = []
    for name in sorted(cfg.seats, key=lambda n: (n.rsplit("-", 1)[0], int(n.rsplit("-", 1)[1]))):
        state = load_state(project.paths, name)
        seat_rows.append({"seat": name, "type": cfg.seats[name].type, "liveness": seats.liveness(project, name),
                          "launcher_state": state.launcher_state, "task": state.task,
                          "stop_requested": state.stop_requested})
    types = {row["type"] for row in seat_rows if row["liveness"] == "alive"}
    snap = project.trello.snapshot(cfg.board_id)
    refs = workflow.card_refs(project)
    reasons = workflow.waiting_reasons(project, snap, refs, types=types)
    waiting = [{"card": card_id, "short": snap.cards[card_id].short, "title": snap.cards[card_id].name,
                "reason": reason} for card_id, reason in sorted(reasons.items(), key=lambda i: snap.cards[i[0]].pos)]
    return {"seats": seat_rows, "waiting": waiting}


def status_text(data: dict) -> str:
    lines = ["Seats:"]
    if not data["seats"]:
        lines.append("  (none)")
    for row in data["seats"]:
        task = row["task"]
        task_text = f"{task['kind']} {task['short']}" if task else "-"
        stop = " (stop requested)" if row["stop_requested"] else ""
        lines.append(f"  {row['seat']:<10} {row['liveness']:<11} {row['launcher_state']:<13} {task_text}{stop}")
    lines.append("Waiting cards:")
    if not data["waiting"]:
        lines.append("  (none)")
    for row in data["waiting"]:
        lines.append(f"  {row['short']}  {row['reason']:<16} {row['title']}")
    return "\n".join(lines)


def claims(project) -> List[Dict]:
    rows = []
    for card_id, value in sorted(workflow.card_refs(project).items()):
        meta = value.meta
        rows.append({"card": card_id, "short": meta.get("short"), "phase": meta.get("phase"),
                     "owner": meta.get("owner"), "age_s": _age(meta.get("created", "")), "branch": meta.get("branch")})
    return rows


def claims_text(rows: List[Dict]) -> str:
    if not rows:
        return "No card refs."
    return "\n".join(f"{r['short']}  {r['phase']:<12} {r['owner'] or '-':<10} {r['age_s']:>7}s  {r['branch']}"
                     for r in rows)
