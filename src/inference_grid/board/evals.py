"""`inference-grid evals`: the nightly eval run, authored onto the board.

The calibration corpus (board/calibration) measures what a lane's acceptance rate does
not: whether a reviewer found the seeded defects, and whether a builder passed the hidden
reference suite. One measurement is a snapshot, so `evals` reads the ledger's own
`eval:<kind>` outcomes — the rows M4 writes and the scorecard aggregates — and authors one
`calibration_run` task per (lane, case) whose kind is not fresh for that lane (older than
`EVAL_EVERY_DAYS`, or never recorded). A nightly launchd job runs it, so every lane's evals
stay current without re-running what is fresh; a lane whose newest result is older than the
window is the needs-you row the digest shows.

Freshness is per lane and kind, because `eval:<kind>` is the row identity the ledger
records: one case of a kind keeps that kind current for the lane, and a stale (or never
recorded) kind authors a run for every case of that kind. The case itself rides in the
`calibration_run` task's spec, so the runner's existing step (board/runner.py) can score
just that case through board/calibration/score.py::score.
"""

import hashlib
import time

from .calibration import calibration_task
from .calibration.case import KINDS, load_corpus

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


def fresh(rows, lanes, lane_id, kind, now, every_days):
    """True when the lane's newest `eval:<kind>` result is inside the window."""
    row = rows.get((*lane_identity(lanes, lane_id), kind))
    if row is None or row["newest_at"] is None:
        return False
    return now - row["newest_at"] < every_days * DAY_SECONDS


def due_evals(corpus_dir, lanes, ledger, now=None, every_days=EVAL_EVERY_DAYS):
    """One entry per (lane, case) a run is due for: its kind is not fresh for the lane.

    The corpus decides the cases (and validates them); the ledger decides freshness. A kind
    that has never been scored is due for every case of that kind, so a lane the operator
    has just configured authors its whole corpus (its first run measures, it does not skip).
    """
    now = time.time() if now is None else now
    rows = eval_rows(ledger)
    due = []
    for case in load_corpus(corpus_dir):
        for lane_id in sorted(lanes):
            if fresh(rows, lanes, lane_id, case["kind"], now, every_days):
                continue
            due.append({"lane": lane_id, "case": case["case"], "kind": case["kind"]})
    return due


def eval_run_id(lane, case, now=None):
    """A day-stable run id for one (lane, case), bounded and `[a-z0-9-]`.

    Stable within a day, so a nightly job that fires again while the run is in flight
    re-returns the same task instead of authoring a second one; a new day's id lets a stale
    lane run again. The lane and case are carried in the spec, so the id needs no room for
    them — the digest's run grouping reads the spec.
    """
    stamp = time.strftime("%Y%m%d", time.gmtime(time.time() if now is None else now))
    tail = hashlib.sha1((lane + "/" + case).encode("utf-8")).hexdigest()[:8]
    return "e-" + stamp + "-" + tail


def author_evals(board_dir, corpus_dir, lanes, ledger, now=None, every_days=EVAL_EVERY_DAYS):
    """Author one `calibration_run` task per due (lane, case); idempotent per run id.

    Returns one row per authored (or already existing) run, with the lane, case and kind.
    """
    results = []
    for entry in due_evals(corpus_dir, lanes, ledger, now=now, every_days=every_days):
        run_id = eval_run_id(entry["lane"], entry["case"], now=now)
        created = calibration_task(
            board_dir, str(corpus_dir), [entry["lane"]], run_id, case=entry["case"]
        )
        results.append(dict(entry, run_id=run_id, task=created["id"], existing=created["existing"]))
    return results


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
