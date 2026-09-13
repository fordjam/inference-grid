"""Read-only board status: one row per task; no dispatch and no writes.

Answers what a board is doing without five ledger queries and a JSON loop: per task the
state, lanes, author family and bounded reason, the live review task for review_pending
work (resolving retry reviews through their recorded source link), and for a blocked
reason that names an attempt, the ledger state plus what the attempt's verdict and
artifacts show under its packet workspace. With suggest, transport-dead blocked tasks
also carry the pre-filled board-new retry JSON (retry_suggestion).
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


def attempt_detail(ledger, aid):
    """Ledger state plus verdict facts for one attempt; unknowns when nothing is readable.

    The verdict and artifact directory live under the attempt's recorded packet workspace;
    everything here is a read.
    """
    detail = {
        "id": aid,
        "state": "unknown",
        "refusal": None,
        "transport_timeout": None,
        "artifacts_present": False,
    }
    try:
        rows = {r["id"]: r for r in ledger.status()}
    except Exception:
        return detail
    row = rows.get(aid)
    if row is None:
        return detail
    detail["state"] = row["state"]
    workspace = row.get("workspace")
    if not workspace:
        return detail
    root = Path(workspace) / aid
    if (root / "verdict.json").is_file():
        try:
            document = json.loads((root / "verdict.json").read_text())
        except (OSError, ValueError):
            document = None
        if isinstance(document, dict):
            detail["refusal"] = document.get("refusal")
            detail["transport_timeout"] = document.get("transport_timeout")
    artifacts = root / "artifacts"
    if artifacts.is_dir():
        detail["artifacts_present"] = any(p.is_file() for p in artifacts.iterdir())
    return detail


def retry_suggestion(board_dir, task, detail):
    """The exact board-new --json payload that would retry a transport-dead blocked task.

    wall_seconds doubles up to the 900 s cap and the change string is pre-written from
    the refusal; board_dir follows the <project>/grid/board convention for project_root.
    A suggestion only — board-new does the authoring, the operator decides.
    """
    previous = task["budget"]["wall_seconds"]
    raised = min(900, 2 * previous)
    refusal = detail.get("refusal") or "transport_error"
    board_dir = Path(board_dir)
    return {
        "board_dir": str(board_dir),
        "project_root": str(board_dir.parent.parent),
        "retry": task["id"],
        "change": f"wall_seconds {previous} -> {raised} after {refusal}"[:300],
        "budget": {
            "wall_seconds": raised,
            "output_bytes": task["budget"]["output_bytes"],
            "thinking_tokens": task["budget"]["thinking_tokens"],
        },
    }


def board_status(ledger, board_dir, suggest=False):
    """One status row per board task; the ledger is only read for attempt states.

    With suggest, a blocked task whose attempt refusal starts with transport_error and
    whose artifacts are absent also carries the pre-filled board-new retry JSON
    (handoff-4 B3) — print only, nothing is authored here. A blocked task whose id is
    the predecessor of an existing task (`<id>-<n>` on the board) is never offered a
    retry: its chain already moved on, whatever the reason text says (old reasons
    predate the enforced `superseded:` convention); such tasks are named in the
    `skipped` list with their successor. The suggest shape is
    {"rows": [...], "skipped": [...]}; without suggest, the plain row list.
    """
    board = load_board(board_dir)
    needs_ledger = any(t["state"] == "blocked" for _, t in board.values())
    rows = []
    skipped = []
    for tid, (path, task) in board.items():
        row = {
            "id": tid,
            "state": task["state"],
            "lanes": task["lanes"],
            "author_family": task["author_family"],
            "blocked_reason": (task["blocked_reason"] or "")[:80] or None,
        }
        successors = sorted(
            other
            for other in board
            if other != tid and re.fullmatch(re.escape(tid) + r"-\d+", other)
        )
        moved_on = suggest and bool(successors)
        if moved_on:
            skipped.append({"task": tid, "successor": successors[0]})
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
                detail = attempt_detail(ledger, match.group(1)) if needs_ledger else None
                row["attempt"] = detail
                if (
                    suggest
                    and not moved_on
                    and isinstance(detail, dict)
                    and str(detail.get("refusal") or "").startswith("transport_error")
                    and not detail.get("artifacts_present")
                ):
                    row["suggest"] = retry_suggestion(board_dir, task, detail)
        rows.append(row)
    if suggest:
        return {"rows": rows, "skipped": skipped}
    return rows
