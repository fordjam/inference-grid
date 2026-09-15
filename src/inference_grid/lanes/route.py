"""Routing that uses the evidence the grid already has, on top of lanes/select.py.

select_lane (integrated unmodified) picks a lane from readiness, category and scorecard
evidence but knows nothing about packet size or reviewer calibration. route prepares its
inputs and carries its answer back with the drop report:

- Defaulting: a task without an explicit `lanes` list is offered every lane declaring
  the task's category, minus explicit_only lanes (first-party families claude/openai,
  which run only where the author named them). An explicit list still restricts.
- Budget fit: the prompt is estimated at inputs_bytes / 4 tokens. The brief travels
  among the staged inputs (task validation requires the brief in `inputs`), so the
  estimate already covers it. A lane whose max_tokens cap (the lane view's own figure,
  else go.py's spend-guard policy for the task's thinking_tokens) or whose context
  window cannot hold the estimate is filtered out with reason budget_unfit before
  selection; the returned `dropped` rows say which lanes and why, carrying the two
  numbers (`prompt`, `cap`) so a dry run can explain the refusal in tokens. The
  `candidates` rows carry the cap each offered lane would run under.
- Recall blend: select_lane reads scorecard rows keyed by (family, model, category)
  and scores each lane with the Laplace (accepted + 1) / (attempts + 2). route hands it
  reshaped rows whose Laplace score equals the packet's blend — see blended_row — so
  calibration evidence steers selection without touching the provider-authored module.
"""

from math import ceil, gcd

from .go import CONTENT_ALLOWANCE, REASONING_HEADROOM
from .select import select_lane

# go.run's request cap when a task carries no thinking budget.
UNBUDGETED_MAX_TOKENS = 16000


def max_tokens_cap(lane, thinking_tokens):
    """The lane's max_tokens cap: the lane view's own figure, else go.py's policy.

    go.run shapes a budgeted request as REASONING_HEADROOM * thinking_tokens +
    CONTENT_ALLOWANCE and defaults to 16 000 when the task carries no thinking budget;
    the lane view may override with its own `max_tokens` figure.
    """
    cap = lane.get("max_tokens")
    if type(cap) is int and cap > 0:
        return cap
    if type(thinking_tokens) is int and thinking_tokens > 0:
        return REASONING_HEADROOM * thinking_tokens + CONTENT_ALLOWANCE
    return UNBUDGETED_MAX_TOKENS


def blended_row(lane, category, scorecard, calibration):
    """The scorecard row select_lane reads, reshaped so its Laplace score is the blend.

    The blend is 0.5 * acceptance + 0.5 * recall: acceptance is the lane's Laplace score
    over the (family, model, category) scorecard row, recall is the lane's calibration
    record — accepted calibration outcomes over recorded calibration outcomes, where a
    calibration outcome is accepted only when every seeded defect was recalled and no
    false positive was filed (board/calibration.py). A lane without a calibration record
    keeps its original row unchanged (acceptance alone); a lane with neither a row nor
    calibration emits none and select_lane's default score of 1/2 stands.

    The blend is carried exactly: with n attempts / a accepted and c accepted of t
    calibration outcomes,

        blend = (a+1) / (2(n+2)) + c / (2t) = ((a+1)*t + c*(n+2)) / (2*(n+2)*t)

    so attempts' = 2(n+2)*t / g - 2 and accepted' = ((a+1)*t + c*(n+2)) / g - 1, with
    g = gcd(numerator, denominator), reproduce the blend at the smallest integers
    select_lane's arithmetic can read. The reshaped attempt count is synthetic: an exact
    blend tie falls through select_lane's attempts tie-break to the reshaped count, then
    lane id — smaller reshaped denominators mean less total evidence behind the tie.
    """
    family, model = lane.get("family"), lane.get("model")
    attempts, accepted, found = 0, 0, False
    for row in scorecard or []:
        if (
            row.get("family") == family
            and row.get("model") == model
            and row.get("category") == category
        ):
            attempts, accepted, found = row.get("attempts", 0), row.get("accepted", 0), True
            break
    cases, cal_accepted = 0, 0
    for report in calibration or []:
        if report.get("family") == family and report.get("model") == model:
            cases, cal_accepted = report.get("cases", 0), report.get("accepted", 0)
            break
    if cases <= 0:
        if not found:
            return None
        return {
            "family": family,
            "model": model,
            "category": category,
            "attempts": attempts,
            "accepted": accepted,
        }
    numerator = (accepted + 1) * cases + cal_accepted * (attempts + 2)
    denominator = 2 * (attempts + 2) * cases
    g = gcd(numerator, denominator)
    return {
        "family": family,
        "model": model,
        "category": category,
        "attempts": denominator // g - 2,
        "accepted": numerator // g - 1,
    }


def route(task, lanes, readiness, scorecard, calibration, now, inputs_bytes):
    """Prepare select_lane's inputs and return its answer with the drop report.

    Returns select_lane's dict with `candidates` replaced by the post-budget candidate
    set offered to selection (select_lane reports only its ready winners, which would
    hide what was offered and lost on readiness) as [{"lane", "cap"}] rows — cap is
    max_tokens_cap for each offered lane — plus `dropped`: the lanes filtered out
    before selection as [{"lane", "reason", "detail", "prompt", "cap"}], reason
    `budget_unfit` with the prompt estimate and the cap it missed (`cap` is the
    max_tokens cap, or the context window when that is what refused). When the budget
    filter empties the candidate set the answer is lane None, reason `budget_unfit` —
    readiness and family exclusion never get a say on a lane that cannot hold the
    packet.
    """
    cat = task.get("category") if isinstance(task, dict) else None
    budget = task.get("budget") if isinstance(task, dict) else None
    thinking = budget.get("thinking_tokens") if isinstance(budget, dict) else None
    explicit = task.get("lanes") if isinstance(task, dict) else None
    explicit = explicit if isinstance(explicit, list) else []
    if explicit:
        candidates = [lid for lid in lanes if lid in explicit]
    else:
        candidates = [
            lid
            for lid in lanes
            if cat in (lanes[lid].get("categories") or []) and not lanes[lid].get("explicit_only")
        ]
    prompt = ceil(inputs_bytes / 4)
    dropped, kept = [], []
    for lid in candidates:
        lane = lanes[lid]
        cap = max_tokens_cap(lane, thinking)
        context = lane.get("context")
        if prompt > cap:
            dropped.append(
                {
                    "lane": lid,
                    "reason": "budget_unfit",
                    "detail": f"prompt ~{prompt} tokens exceeds max_tokens cap {cap}",
                    "prompt": prompt,
                    "cap": cap,
                }
            )
        elif context and prompt > context:
            dropped.append(
                {
                    "lane": lid,
                    "reason": "budget_unfit",
                    "detail": f"prompt ~{prompt} tokens exceeds context {context}",
                    "prompt": prompt,
                    "cap": context,
                }
            )
        else:
            kept.append({"lane": lid, "cap": cap})
    if not kept:
        return {
            "lane": None,
            "score": None,
            "reason": "budget_unfit",
            "candidates": [],
            "dropped": dropped,
        }
    rows = []
    for lid in sorted(row["lane"] for row in kept):
        row = blended_row(lanes[lid], cat, scorecard, calibration)
        if row is not None:
            rows.append(row)
    choice = select_lane(
        {"category": cat, "author_family": task.get("author_family")},
        {lid: lanes[lid] for lid in (row["lane"] for row in kept)},
        readiness,
        rows,
        now,
    )
    return {
        "lane": choice["lane"],
        "score": choice["score"],
        "reason": choice["reason"],
        "candidates": sorted(kept, key=lambda row: row["lane"]),
        "dropped": dropped,
    }
