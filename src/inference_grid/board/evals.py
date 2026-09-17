"""Eval freshness reporting for the operator digest (B4: the nightly authoring this
module used to do — `inference-grid evals` — is deleted; a `calibration_run` task is
no longer ever authored automatically. What is left reads the ledger's own recorded
`eval:<kind>` outcomes — whatever produced them, board/runner.py's existing
step_eval_case/step_calibration_run still score a calibration_run task if one exists —
and reports how stale each lane's evidence is, for the digest's Evals table and its
needs-you rows.
"""

import time

from .calibration.case import KINDS

EVAL_EVERY_DAYS = 7
DAY_SECONDS = 86400
EVAL_PREFIX = "eval:"


def lane_identity(lanes, lane_id):
    """The (family, model) the ledger records on every attempt for this lane."""
    lane = lanes.get(lane_id) or {}
    return (lane.get("family"), lane.get("model"))


def eval_rows(ledger):
    """Per (family, model, kind): accepted, cases and the newest outcome instant.

    The scorecard's `eval:<kind>` rows carry accepted and cases but no clock; the ledger's
    `outcome_recorded` events carry both. Reading the events and joining each attempt to its
    task's spec reproduces the scorecard's own identity — (family, model, category) — with
    the timestamp the due-check needs. The newest event for an attempt wins, so an eval
    outcome recorded after an attempt's own category row is the one counted.
    """
    from sqlalchemy import select

    from ..ledger import attempts as attempt_records
    from ..ledger import events as event_records
    from ..ledger import tasks as task_records

    with ledger.engine.connect() as con:
        specs = {t["id"]: t["spec"] for t in con.execute(select(task_records)).mappings()}
        attempts = list(con.execute(select(attempt_records)).mappings())
        outcomes = {}
        for event in con.execute(
            select(event_records).where(event_records.c.kind == "outcome_recorded")
        ).mappings():
            detail = event["detail"]
            if isinstance(detail, dict) and _eval_kind(detail.get("category")):
                outcomes[event["attempt"]] = (detail, event["at"])
    rows = {}
    for attempt in attempts:
        recorded = outcomes.get(attempt["id"])
        if recorded is None:
            continue
        detail, at = recorded
        spec = specs.get(attempt["task"], {})
        key = (spec.get("family", "?"), spec.get("model", "?"), _eval_kind(detail["category"]))
        entry = rows.setdefault(
            key,
            {
                "family": key[0],
                "model": key[1],
                "kind": key[2],
                "accepted": 0,
                "cases": 0,
                "newest_at": None,
            },
        )
        entry["cases"] += 1
        entry["accepted"] += bool(detail.get("accepted"))
        if isinstance(at, (int, float)) and (entry["newest_at"] is None or at > entry["newest_at"]):
            entry["newest_at"] = at
    return rows


def _eval_kind(category):
    """The case kind an `eval:<kind>` category names, or None for any other outcome."""
    if not isinstance(category, str) or not category.startswith(EVAL_PREFIX):
        return None
    kind = category[len(EVAL_PREFIX) :]
    return kind if kind in KINDS else None


def eval_summary(ledger, lanes, now=None, every_days=EVAL_EVERY_DAYS):
    """Per-lane, per-kind accepted/cases and the newest result's age, plus the needs-you lanes.

    A lane is needs-you when no kind has a result inside the window: evals the operator has
    stopped running (or never started), which is exactly the invitation the digest makes.
    """
    now = time.time() if now is None else now
    rows = eval_rows(ledger)
    lane_rows, needs_you = [], []
    for lane_id in sorted(lanes):
        kinds, newest = [], None
        for kind in KINDS:
            row = rows.get((*lane_identity(lanes, lane_id), kind))
            age = None
            if row is not None and row["newest_at"] is not None:
                age = (now - row["newest_at"]) / DAY_SECONDS
                newest = row["newest_at"] if newest is None else max(newest, row["newest_at"])
            kinds.append(
                {
                    "kind": kind,
                    "accepted": row["accepted"] if row else 0,
                    "cases": row["cases"] if row else 0,
                    "age_days": age,
                }
            )
        newest_age = None if newest is None else (now - newest) / DAY_SECONDS
        lane_rows.append(
            {
                "lane": lane_id,
                "family": (lanes[lane_id] or {}).get("family"),
                "model": (lanes[lane_id] or {}).get("model"),
                "kinds": kinds,
                "newest_age_days": newest_age,
            }
        )
        if newest_age is None or newest_age >= every_days:
            needs_you.append(
                {
                    "lane": lane_id,
                    "since_days": newest_age,
                    "reason": (
                        "no eval ever recorded"
                        if newest_age is None
                        else f"no eval in {every_days} days"
                    ),
                }
            )
    return {"lanes": lane_rows, "needs_you": needs_you}


def eval_markdown(summary):
    """The digest's Evals table: per lane, per kind, accepted / cases and the newest age."""
    lines = ["| lane | kind | accepted / cases | newest result |", "| --- | --- | --- | --- |"]
    for row in summary["lanes"]:
        for kind in row["kinds"]:
            age = kind["age_days"]
            newest = "never" if age is None else f"{age:.1f} d"
            lines.append(
                f"| {row['lane']} | {kind['kind']} | {kind['accepted']} / {kind['cases']} "
                f"| {newest} |"
            )
    return lines


def lanes_from_configs(configs):
    """The first readable `lanes_path` named by the board configs, as {lane_id: lane}.

    The digest reads boards, not the operator's configuration; a board config that names
    the lanes file is the only road from the digest to the lane set. An absent or
    unreadable file leaves the Evals section with no lanes, never an error.
    """
    for config in configs:
        path = config.get("lanes_path")
        if not path:
            continue
        try:
            from ..lanes.runner import load_lanes

            return load_lanes(path)
        except (OSError, ValueError):
            continue
    return {}
