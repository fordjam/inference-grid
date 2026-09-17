"""`inference-grid report --week`: the ledger's own outcome counts, one page a week.

B4 replaced the bandit router's learned score with a static tier table; what the router
used to spend on estimating quality, this reads back afterward, from what actually
happened — no estimate, the ledger's own terminal states.

Per lane (family/model, or the lane id when `lanes` and `accounts_by_lane` are supplied
— see `_lane_label`): attempts that completed, failed or were abandoned this window.
Reviews: independent_review attempts performed this window vs those not (yet) accepted.
That "rejected" count is a live read, not a window-stable one — an attempt that completed
inside the window but is accepted after it closes will show first as unaccepted, then as
accepted, if the report is regenerated later; the ledger records no separate "rejected"
event, only the eventual accept. Holds: attempts held right now, and every
`operator_resolved` event this window with its resolver — B8 requires that string be a
human operator, never "coordinator" or "board-runner". Cost: an account's operator-
supplied monthly subscription price divided by the attempts that landed (state ==
"accepted") this window; an account with no price on file reports cost as None rather
than a fabricated number. `prices` may be keyed by an account's primary id or by any of
its aliases — resolved the same way `quota_context` resolves `accounts_by_lane`.
"""

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .ledger import aliases as alias_records
from .ledger import attempts as attempt_records
from .ledger import events as event_records
from .ledger import tasks as task_records

WEEK_SECONDS = 7 * 86400
TERMINAL_STATES = ("completed", "accepted", "failed", "abandoned")


def week_bounds(week, now):
    """(start, end) Unix seconds for an ISO week ("2026-W38") or, absent one, the seven
    days ending now."""
    if week is None:
        return now - WEEK_SECONDS, now
    year, _, index = week.partition("-W")
    if not year or not index:
        raise ValueError("week must look like 2026-W38")
    start = datetime.fromisocalendar(int(year), int(index), 1).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    return start.timestamp(), end.timestamp()


def _lane_index(lanes, accounts_by_lane, alias_map):
    """(family, model, primary_account) -> lane_id, for every lane whose account is known.

    Two lanes can share a family/model (the exact case B4's quota tiebreak exists for —
    the same model on two accounts); only the account disambiguates them, so a lane
    without a resolvable account is not indexed here and falls back to the (family,
    model) label instead of silently merging with another lane's counts.
    """
    index = {}
    for lane_id, lane in (lanes or {}).items():
        alias = (accounts_by_lane or {}).get(lane_id)
        if alias is None:
            continue
        account = alias_map.get(alias, alias)
        index[(lane.get("family"), lane.get("model"), account)] = lane_id
    return index


def _lane_label(lane_index, family, model, account):
    lane_id = lane_index.get((family, model, account))
    if lane_id is not None:
        return lane_id
    return f"{family}/{model}"


def report(ledger, week=None, lanes=None, accounts_by_lane=None, prices=None, now=None):
    """The week's outcome counts. `lanes` + `accounts_by_lane` (both optional — a
    lanes.json-shaped dict and its lane-id -> account-alias map) label rows by lane id
    instead of family/model, disambiguating lanes that share a model. `prices` (optional,
    {account or alias: monthly USD}) is the only source of the cost line — nothing here
    estimates a subscription price."""
    now = time.time() if now is None else now
    start, end = week_bounds(week, now)
    prices = prices or {}

    with ledger.engine.connect() as con:
        specs = {t["id"]: t["spec"] for t in con.execute(select(task_records)).mappings()}
        rows = list(
            con.execute(
                select(attempt_records).where(
                    attempt_records.c.state.in_(TERMINAL_STATES),
                    attempt_records.c.updated >= start,
                    attempt_records.c.updated < end,
                )
            ).mappings()
        )
        held = list(
            con.execute(select(attempt_records).where(attempt_records.c.state == "held")).mappings()
        )
        resolutions = list(
            con.execute(
                select(event_records).where(
                    event_records.c.kind == "operator_resolved",
                    event_records.c.at >= start,
                    event_records.c.at < end,
                )
            ).mappings()
        )
        alias_map = {r["id"]: r["account"] for r in con.execute(select(alias_records)).mappings()}

    lane_index = _lane_index(lanes, accounts_by_lane, alias_map)
    per_lane = {}
    reviews_performed = reviews_rejected = 0
    landed_by_account = {}
    for row in rows:
        spec = specs.get(row["task"], {})
        family, model = spec.get("family", "?"), spec.get("model", "?")
        label = _lane_label(lane_index, family, model, row["account"])
        entry = per_lane.setdefault(
            label, {"lane": label, "completed": 0, "failed": 0, "abandoned": 0}
        )
        if row["state"] in ("completed", "accepted"):
            entry["completed"] += 1
        elif row["state"] == "failed":
            entry["failed"] += 1
        elif row["state"] == "abandoned":
            entry["abandoned"] += 1
        # The ledger's own spec carries no board category (that lives in the board task
        # file, not the worker spec); "review-<id>" is create_review_task's own naming
        # convention (board/runner.py) and the only review signal the ledger itself has.
        if str(row["task"]).startswith("review-"):
            reviews_performed += 1
            if row["state"] != "accepted":
                reviews_rejected += 1
        if row["state"] == "accepted":
            landed_by_account[row["account"]] = landed_by_account.get(row["account"], 0) + 1

    holds = [
        {"attempt": r["id"], "task": r["task"], "account": r["account"], "reason": r["reason"]}
        for r in held
    ]
    resolved = [
        {
            "attempt": r["attempt"],
            "operator": r["detail"].get("operator"),
            "outcome": r["detail"].get("outcome"),
            "reason": r["detail"].get("reason"),
        }
        for r in resolutions
    ]
    cost = {}
    for account_or_alias, price in prices.items():
        account = alias_map.get(account_or_alias, account_or_alias)
        landed = landed_by_account.get(account)
        cost[account_or_alias] = (price / landed) if landed else None

    return {
        "week": week,
        "start": start,
        "end": end,
        "lanes": sorted(per_lane.values(), key=lambda e: e["lane"]),
        "reviews": {"performed": reviews_performed, "rejected": reviews_rejected},
        "held": holds,
        "resolved": resolved,
        "cost_per_landed_packet_usd": cost,
    }
