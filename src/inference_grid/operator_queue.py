"""The "needs you" overlay: work only the operator can unblock, and the goal's metric.

Two lists for the capacity dashboard overlay:

- `operator` — every item waiting on a human: a board task `blocked` whose reason names
  an operator decision (`operator`, `owner`, `resolve with evidence`; `superseded`
  excluded), a ledger attempt `held` for resolution, an alarm currently raised in the
  watch state file (the `watch` CLI's own state, when `watch_state` is configured), a
  packet the plan node drafted and nobody has released (`draft`, brief 20 M1's fix packets
  among them), and every row of the operator's hand-kept `owner-decisions.json`. Rows
  are `{kind, id, reason, since}`.
- `accepted_work` — per subscription per ISO week, from the ledger: attempts completed
  and accepted (including operator-recorded external work), and reviews whose rejected
  verdict named a real finding. Rows are `{week, account, accepted, attempts}`.
- `reviewer_recall` — the newest scored calibration run per lane, from each board's
  calibration reports: `{run_id, lane, recall, precision, scored_at}`. Recall is the
  number that says what an approval is worth.

Nothing here reads credentials, `lanes.json` or a home directory: every path is passed
in by the caller (the overlay builder, or the local deployment once packet A2 lands).
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .lanes.brief import PACKET_HEADING
from .ledger import attempts as attempts_table
from .ledger import events as events_table

# The plan node's drafts list (`board/plan_task.py`): packet task ids it authored and
# nobody has released yet.
DRAFTS_FILE = "drafts.json"

# A blocked reason needs the operator when it names one of these (case-insensitive),
# unless it says the block was superseded.
OPERATOR_REASON_WORDS = ("operator", "owner", "resolve with evidence")
EXCLUDED_REASON_WORDS = ("superseded",)

# The review-rejection note the board runner records; advisory-only findings rest on
# the brief's size hint, not a defect, so they do not count as real findings.
_REVIEW_REJECTED = re.compile(r"review rejected with (\d+) finding")
_ADVISORY_ONLY = re.compile(r"\(finding ([0-9, ]+) advisory-only:")

# The dashboard keeps a bounded window: the most recent ISO weeks emitted per account.
WEEKS_KEPT = 8


def _iso(ts):
    """An absolute UTC instant from an epoch second; empty string when unknown."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _blocked_task_rows(board_dirs):
    rows = []
    for board_dir in board_dirs:
        board = Path(board_dir)
        if not board.is_dir():
            continue
        for path in sorted(board.glob("*.json")):
            try:
                task = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(task, dict) or task.get("state") != "blocked":
                continue
            reason = task.get("blocked_reason")
            if not isinstance(reason, str):
                continue
            lowered = reason.lower()
            if not any(word in lowered for word in OPERATOR_REASON_WORDS):
                continue
            if any(word in lowered for word in EXCLUDED_REASON_WORDS):
                continue
            try:
                since = path.stat().st_mtime
            except OSError:
                since = None
            rows.append(
                {
                    "kind": "blocked_task",
                    "id": str(task.get("id") or path.stem),
                    "reason": reason,
                    "since": _iso(since),
                }
            )
    return rows


def _held_attempt_rows(ledger):
    rows = []
    for row in ledger.status():
        if row.get("state") != "held":
            continue
        rows.append(
            {
                "kind": "held_attempt",
                "id": str(row.get("id") or ""),
                "reason": str(row.get("reason") or ""),
                "since": _iso(row.get("updated")),
            }
        )
    return rows


def _drafted_title(board, packet_id):
    """The drafted packet's heading text from its brief, else the task id.

    The brief is project-relative and the board sits at `<project>/grid/board` (the same
    layout `board/runner.py` resolves a board through), so the project root is the
    board's grandparent. An unreadable brief is the id — never an invented title.
    """
    try:
        task = json.loads((board / (packet_id + ".json")).read_text())
        brief = (board.parent.parent / task["brief"]).read_text()
    except (OSError, KeyError, TypeError, ValueError):
        return packet_id
    match = PACKET_HEADING.search(brief)
    return match.group(2).strip() if match else packet_id


def _draft_rows(board_dirs):
    """Packets the plan node drafted and the operator has not released.

    A draft is valid `ready` work the board deliberately holds out of dispatch until the
    operator releases it or the board sets `auto_dispatch` (brief J1); brief 20 M1's fix
    packets arrive here on their own, so they need to be visible where the operator looks.
    """
    rows = []
    for board_dir in board_dirs:
        board = Path(board_dir)
        try:
            drafts = json.loads((board / DRAFTS_FILE).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(drafts, dict):
            drafts = drafts.get("drafts")
        if not isinstance(drafts, list):
            continue
        try:
            since = (board / DRAFTS_FILE).stat().st_mtime
        except OSError:
            since = None
        for packet_id in drafts:
            if not isinstance(packet_id, str) or not packet_id:
                continue
            rows.append(
                {
                    "kind": "draft",
                    "id": packet_id,
                    "reason": _drafted_title(board, packet_id),
                    "since": _iso(since),
                }
            )
    return rows


def _alarm_rows(watch_state):
    """Alarms currently raised in the `watch` CLI's state file (src/inference_grid/watch.py).

    Alarms are `{kind, key, since, detail}` under `"alarms"` (a bare list is also
    accepted). If the file is missing or unreadable there are no alarm rows — a missing
    state file must never invent operator work.
    """
    if not watch_state:
        return []
    try:
        state = json.loads(Path(watch_state).read_text())
    except (OSError, ValueError):
        return []
    alarms = state.get("alarms") if isinstance(state, dict) else state
    if not isinstance(alarms, list):
        return []
    rows = []
    for alarm in alarms:
        if not isinstance(alarm, dict):
            continue
        kind, key = alarm.get("kind"), alarm.get("key")
        if not isinstance(kind, str) or not kind or not isinstance(key, str) or not key:
            continue
        detail = alarm.get("detail")
        rows.append(
            {
                "kind": "alarm",
                "id": kind + ":" + key,
                "reason": detail if isinstance(detail, str) else "",
                "since": alarm.get("since") if isinstance(alarm.get("since"), str) else "",
            }
        )
    return rows


def _decision_rows(owner_decisions):
    if not owner_decisions:
        return []
    try:
        decisions = json.loads(Path(owner_decisions).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(decisions, list):
        return []
    rows = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        rid, question = decision.get("id"), decision.get("question")
        if not isinstance(rid, str) or not rid:
            continue
        rows.append(
            {
                "kind": "decision",
                "id": rid,
                "reason": question if isinstance(question, str) else "",
                "since": decision.get("since") if isinstance(decision.get("since"), str) else "",
            }
        )
    return rows


def operator_rows(ledger, board_dirs=(), watch_state=None, owner_decisions=None):
    """Every item that needs the operator, as `{kind, id, reason, since}` rows."""
    rows = [
        *_blocked_task_rows(board_dirs),
        *_draft_rows(board_dirs),
        *_held_attempt_rows(ledger),
        *_alarm_rows(watch_state),
        *_decision_rows(owner_decisions),
    ]
    return sorted(rows, key=lambda r: (r["kind"], r["id"]))


def _real_findings(note):
    """Findings in a review-rejection note that are not advisory-only size hints."""
    match = _REVIEW_REJECTED.match(note or "")
    if not match:
        return 0
    total = int(match.group(1))
    advisory = _ADVISORY_ONLY.search(note)
    skipped = 0
    if advisory:
        skipped = len([part for part in advisory.group(1).split(",") if part.strip()])
    return max(0, total - skipped)


def _week(ts):
    """The ISO week of an epoch second, as `YYYY-Www`."""
    year, week, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def accepted_work(ledger, now=None):
    """Per subscription per ISO week: `{week, account, accepted, attempts}` rows.

    An attempt counts as accepted when its recorded outcome says so (a completed
    attempt accepted by independent review, or operator-recorded external work with
    `accepted: true`), when it reached the `accepted` state, or — for a review — when
    its rejected verdict named a real finding. Weeks bucket on the attempt's last
    update; only the most recent `WEEKS_KEPT` weeks are emitted, which is what the
    dashboard's this-week-against-last comparison and its 50-row bound need.
    """
    now = time.time() if now is None else now
    oldest = _week(now - (WEEKS_KEPT - 1) * 7 * 86400)
    with ledger.engine.connect() as con:
        rows = list(con.execute(select(attempts_table)).mappings())
        outcomes = {}
        for event in con.execute(
            select(events_table)
            .where(events_table.c.kind == "outcome_recorded")
            .order_by(events_table.c.at)
        ).mappings():
            outcomes[event["attempt"]] = event["detail"]
    buckets = {}
    for row in rows:
        updated = row["updated"]
        if not updated:
            continue
        week = _week(updated)
        if week < oldest:
            continue
        outcome = outcomes.get(row["id"], {})
        accepted = bool(outcome.get("accepted")) or row["state"] == "accepted"
        if not accepted and outcome.get("category") == "independent_review":
            # A rejected review is itself the work product: it named a real finding.
            accepted = _real_findings(outcome.get("note")) > 0
        key = (week, row["account"])
        bucket = buckets.setdefault(
            key, {"week": week, "account": row["account"], "accepted": 0, "attempts": 0}
        )
        bucket["attempts"] += 1
        bucket["accepted"] += accepted
    return [buckets[key] for key in sorted(buckets)]


def reviewer_recall(board_dirs):
    """The newest scored calibration run per lane, as `{run_id, lane, recall, precision, scored_at}`.

    Each run's `score_calibration` report lands under `<board_dir>/calibration/<run_id>/report.json`
    and carries `scored_at` plus the per-lane recall/precision. One row per lane, the newest
    `scored_at` winning; a newer run of a lane supersedes the older one entirely, so a lane
    never shows a mix of runs. Unknown or unreadable reports are skipped, never invented.
    """
    newest = {}
    for board_dir in board_dirs:
        reports = Path(board_dir) / "calibration"
        for path in sorted(reports.glob("*/report.json")):
            try:
                report = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(report, dict):
                continue
            run_id = report.get("run_id") or path.parent.name
            scored_at = report.get("scored_at") if isinstance(report.get("scored_at"), str) else ""
            lanes = report.get("lanes")
            if not isinstance(lanes, dict):
                continue
            for lane, entry in lanes.items():
                if not isinstance(entry, dict):
                    continue
                row = {
                    "run_id": run_id,
                    "lane": lane,
                    "recall": entry.get("recall"),
                    "precision": entry.get("precision"),
                    "scored_at": scored_at,
                }
                current = newest.get(lane)
                if current is None or (row["scored_at"], str(row["run_id"])) > (
                    current["scored_at"],
                    str(current["run_id"]),
                ):
                    newest[lane] = row
    return [newest[lane] for lane in sorted(newest)]


def build_overlay(ledger, board_dirs=(), watch_state=None, owner_decisions=None, now=None):
    """The overlay lists the dashboard consumes, ready for the operator's overlay file."""
    return {
        "operator": operator_rows(ledger, board_dirs, watch_state, owner_decisions),
        "accepted_work": accepted_work(ledger, now),
        "reviewer_recall": reviewer_recall(board_dirs),
    }
