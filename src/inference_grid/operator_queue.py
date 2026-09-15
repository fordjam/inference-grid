"""The "needs you" overlay: work only the operator can unblock, and the goal's metric.

Two lists for the capacity dashboard overlay:

- `operator` — every item waiting on a human: a board task `blocked` whose reason names
  an operator decision (`operator`, `owner`, `resolve with evidence`; `superseded`
  excluded), a ledger attempt `held` for resolution, an alarm currently raised in the
  watch state file (packet A1's `watch` CLI; absent until that packet lands), and every
  row of the operator's hand-kept `owner-decisions.json`. Rows are
  `{kind, id, reason, since}`.
- `accepted_work` — per subscription per ISO week, from the ledger: attempts completed
  and accepted (including operator-recorded external work), and reviews whose rejected
  verdict named a real finding. Rows are `{week, account, accepted, attempts}`.

Nothing here reads credentials, `lanes.json` or a home directory: every path is passed
in by the caller (the overlay builder, or the local deployment once packet A2 lands).
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .ledger import attempts as attempts_table
from .ledger import events as events_table

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


def _alarm_rows(watch_state):
    """Alarms currently raised in A1's state file.

    The `watch` packet has not landed, so its state format is taken from the brief:
    alarms are `{kind, key, since, detail}` under `"alarms"` (a bare list is also
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


def build_overlay(ledger, board_dirs=(), watch_state=None, owner_decisions=None, now=None):
    """The overlay lists the dashboard consumes, ready for the operator's overlay file."""
    return {
        "operator": operator_rows(ledger, board_dirs, watch_state, owner_decisions),
        "accepted_work": accepted_work(ledger, now),
    }
