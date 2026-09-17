"""`inference-grid report --week`: the ledger's own outcome counts, one page a week.

B4 replaced the bandit router's learned score with a static tier table; what the router
used to spend on estimating quality, this reads back afterward, from what actually
happened — no estimate, the ledger's own terminal states.

Per lane (family/model, or the lane id when `lanes` and `accounts_by_lane` are supplied
— see `_lane_label`): attempts that completed, failed or were abandoned this window.
Reviews: independent_review attempts performed this window vs rejected vs waived
(waived should always be zero — B7 removed every waiver code path; this is a live
tripwire, not a number the report ever expects to see). "Rejected" reads the runner's
own `record_outcome(aid, "independent_review", passed, ...)` call (an `outcome_recorded`
event, `accepted` field) -- never the review attempt's own ledger *state*: B7's
`accept_reviewed` (board/runner.py) calls `ledger.accept()` on the *source* packet
attempt, not the review attempt, so a review attempt's terminal state is always
"completed", approved or not, and can't tell the two apart on its own. An attempt whose
outcome has not yet been recorded (rare — the runner records it in the same tick the
attempt settles) counts as performed but not rejected, never a guessed verdict. Holds:
attempts held right now, and every `operator_resolved` event this window
with its resolver — B8 requires that string be a human operator, never "coordinator" or
"board-runner". Packets landed: attempts that reached `accepted` this window (source
packet attempts only — a review attempt itself never reaches `accepted`, see
`accept_reviewed` in board/runner.py, so this never double-counts a review as a packet).
Unattended-land rate: of those, the fraction whose attempt id never appears in an
`operator_resolved` event at any point in the ledger's history — C2's "packets that
reached approval with no operator touch before it," read here for the weekly page even
though C2's own threshold/report is a later row. Claude Max share: of review attempts
this window (the only category the ledger's own spec can identify a "plan or review"
task by — a plan-node attempt carries no such marker in the ledger, only on the board
task file this module never reads), the fraction whose spec names family "claude"; None
when no review attempt ran this window, never a fabricated 0. Cost: an account's
operator-supplied monthly subscription price divided by the attempts that landed (state
== "accepted") this window; an account with no price on file reports cost as None rather
than a fabricated number. `prices` may be keyed by an account's primary id or by any of
its aliases — resolved the same way `quota_context` resolves `accounts_by_lane`. Its
shape (an optional file at `~/.config/inference-grid/prices.json`, read by the CLI, never
by this module — nothing here touches a filesystem path): `{"<account or alias>": <USD
monthly price, number>, ...}`; a missing file or an account absent from it costs `None`.
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
REVIEW_PREFIX = "review-"


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
        # All-time, not windowed: an attempt an operator resolved before this window
        # opened is still an operator touch on it — the unattended-land rate asks
        # whether a human ever touched this exact attempt, not just this week.
        touched_attempts = set(
            con.execute(
                select(event_records.c.attempt).where(event_records.c.kind == "operator_resolved")
            ).scalars()
        )
        waived = (
            con.execute(
                select(event_records).where(
                    event_records.c.kind == "review_waived",
                    event_records.c.at >= start,
                    event_records.c.at < end,
                )
            )
            .mappings()
            .all()
        )
        # A review attempt's own ledger *state* is never a reject/approve signal: B7's
        # accept_reviewed() (board/runner.py) calls ledger.accept() on the *source*
        # packet attempt, never the review attempt itself, so a review attempt's
        # terminal state is "completed" whether it was approved or rejected -- the
        # runner's own record_outcome(aid, "independent_review", passed, ...) call,
        # recorded as this outcome_recorded event's "accepted" field, is the only real
        # signal. Windowed like `rows`: this is what "performed this window" means.
        outcomes = list(
            con.execute(
                select(event_records).where(
                    event_records.c.kind == "outcome_recorded",
                    event_records.c.at >= start,
                    event_records.c.at < end,
                )
            ).mappings()
        )
        alias_map = {r["id"]: r["account"] for r in con.execute(select(alias_records)).mappings()}

    review_accepted = {}
    for o in outcomes:
        detail = o["detail"] if isinstance(o["detail"], dict) else {}
        if detail.get("category") == "independent_review" and isinstance(
            detail.get("accepted"), bool
        ):
            review_accepted[o["attempt"]] = detail["accepted"]

    lane_index = _lane_index(lanes, accounts_by_lane, alias_map)
    per_lane = {}
    reviews_performed = reviews_rejected = 0
    claude_reviews = 0
    landed_by_account = {}
    landed_attempts = []
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
        # "review-<id>" is create_review_task's own naming convention (board/runner.py)
        # and the only review signal the ledger's attempts table has; the accept/reject
        # verdict itself comes from `review_accepted` above, never this row's own state
        # (see the comment where `outcomes` is queried).
        is_review = str(row["task"]).startswith(REVIEW_PREFIX)
        if is_review:
            reviews_performed += 1
            if review_accepted.get(row["id"]) is False:
                reviews_rejected += 1
            if family == "claude":
                claude_reviews += 1
        if row["state"] == "accepted":
            landed_by_account[row["account"]] = landed_by_account.get(row["account"], 0) + 1
            if not is_review:
                # accept_reviewed() (board/runner.py) calls ledger.accept() on the
                # *source* packet attempt, never on the review attempt itself — a
                # review attempt can never legitimately reach "accepted" — but this
                # guard costs nothing and keeps the count honest if that ever changes.
                landed_attempts.append(row["id"])

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

    unattended = None
    if landed_attempts:
        untouched = sum(1 for aid in landed_attempts if aid not in touched_attempts)
        unattended = untouched / len(landed_attempts)

    return {
        "week": week,
        "start": start,
        "end": end,
        "lanes": sorted(per_lane.values(), key=lambda e: e["lane"]),
        "reviews": {
            "performed": reviews_performed,
            "rejected": reviews_rejected,
            "waived": len(waived),
        },
        "held": holds,
        "resolved": resolved,
        "packets_landed": len(landed_attempts),
        "unattended_land_rate": unattended,
        "claude_max_share": (claude_reviews / reviews_performed) if reviews_performed else None,
        "cost_per_landed_packet_usd": cost,
    }


def _stamp(instant):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(instant)) if instant else "—"


def _pct(fraction):
    return f"{fraction * 100:.0f}%" if fraction is not None else "unrecorded"


def _money(amount):
    return f"${amount:.2f}" if amount is not None else "unrecorded (no price on file)"


def render_markdown(rep):
    """The weekly page as markdown -- the one thing 02-C1 asks a human to read.

    A pure formatter over `report()`'s own dict; every number here is read back from
    it, nothing is recomputed. `rep["week"]` may be None (the last 7 days, `report()`'s
    own default) -- the heading names the window by its actual start/end in that case,
    never invents an ISO week label for a window that was not one.
    """
    window = rep["week"] or f"{_stamp(rep['start'])} — {_stamp(rep['end'])} UTC"
    lines = [
        f"# Inference Grid — weekly report ({window})",
        "",
        f"Packets landed: **{rep['packets_landed']}**. "
        f"Unattended-land rate: **{_pct(rep['unattended_land_rate'])}** "
        "(packets that reached approval with no operator touch before it).",
        "",
        "## Per lane",
        "",
        "| lane | completed | failed | abandoned |",
        "| --- | --- | --- | --- |",
    ]
    for entry in rep["lanes"]:
        lines.append(
            f"| {entry['lane']} | {entry['completed']} | {entry['failed']} | {entry['abandoned']} |"
        )
    if not rep["lanes"]:
        lines.append("| (no attempts settled this window) | | | |")
    reviews = rep["reviews"]
    lines += [
        "",
        "## Reviews",
        "",
        f"Performed: **{reviews['performed']}**. Rejected: **{reviews['rejected']}**. "
        f"Waived: **{reviews['waived']}**"
        + (
            " — should always be zero (B7 removed every waiver path)." if reviews["waived"] else "."
        ),
        "",
        f"Claude Max share of reviews this window: **{_pct(rep['claude_max_share'])}**.",
        "",
        "## Holds",
        "",
        "| attempt | task | account | reason |",
        "| --- | --- | --- | --- |",
    ]
    for h in rep["held"]:
        lines.append(f"| {h['attempt']} | {h['task']} | {h['account']} | {h['reason']} |")
    if not rep["held"]:
        lines.append("| (nothing held right now) | | | |")
    lines += [
        "",
        "### Resolved this window",
        "",
        "| attempt | operator | outcome | reason |",
        "| --- | --- | --- | --- |",
    ]
    for r in rep["resolved"]:
        lines.append(f"| {r['attempt']} | {r['operator']} | {r['outcome']} | {r['reason']} |")
    if not rep["resolved"]:
        lines.append("| (no holds resolved this window) | | | |")
    lines += [
        "",
        "## Cost per landed packet",
        "",
        "| account | subscription price ÷ landed packets |",
        "| --- | --- |",
    ]
    for account, amount in sorted(rep["cost_per_landed_packet_usd"].items()):
        lines.append(f"| {account} | {_money(amount)} |")
    if not rep["cost_per_landed_packet_usd"]:
        lines.append("| (no prices on file — see `~/.config/inference-grid/prices.json`) | |")
    return "\n".join(lines) + "\n"
