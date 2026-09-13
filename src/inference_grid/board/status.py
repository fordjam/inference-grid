"""Read-only board status: one row per task; no dispatch and no writes.

Answers what a board is doing without five ledger queries and a JSON loop: per task the
state, lanes, author family and bounded reason, the live review task for review_pending
work (resolving retry reviews through their recorded source link), and the ledger state
of any attempt a blocked reason names.
"""

import json
import re
from pathlib import Path

from .runner import load_board

ATTEMPT_IN_REASON = re.compile(
    r"attempt ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.IGNORECASE
)


def review_candidates(board_dir, source_id):
    """Review task ids that may judge source_id: named links first, then the prefix rule."""
    candidates = []
    review_dir = Path(board_dir) / "review"
    if review_dir.is_dir():
        for link_path in sorted(review_dir.glob("*/source.json")):
            try:
                link = json.loads(link_path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(link, dict) and link.get("task") == source_id:
                named = link.get("review_task") or link_path.parent.name
                if isinstance(named, str) and named.startswith("review-"):
                    candidates.append(named)
    candidates.append("review-" + source_id)
    return list(dict.fromkeys(candidates))


def board_status(ledger, board_dir):
    """One status row per board task; the ledger is only read for attempt states."""
    board = load_board(board_dir)
    attempts = {}
    if any(t["state"] == "blocked" for _, t in board.values()):
        try:
            attempts = {r["id"]: r for r in ledger.status()}
        except Exception:
            attempts = {}
    rows = []
    for tid, (path, task) in board.items():
        row = {
            "id": tid,
            "state": task["state"],
            "lanes": task["lanes"],
            "author_family": task["author_family"],
            "blocked_reason": (task["blocked_reason"] or "")[:80] or None,
        }
        if task["state"] == "review_pending":
            found = None
            for rid in review_candidates(board_dir, tid):
                entry = board.get(rid)
                if entry is not None:
                    found = {"task": rid, "state": entry[1]["state"]}
                    break
            row["review"] = found or {
                "task": review_candidates(board_dir, tid)[0],
                "state": "missing",
            }
        if task["state"] == "blocked":
            match = ATTEMPT_IN_REASON.search(task["blocked_reason"] or "")
            if match:
                aid = match.group(1)
                row["attempt"] = aid
                row["attempt_state"] = attempts.get(aid, {}).get("state", "unknown")
        rows.append(row)
    return rows
