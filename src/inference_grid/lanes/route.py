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
- Tier: the lane record's `tier` (plan | build | review) is the operator's statement of
  what a lane is for — SOTA for planning and review, workhorses for building (docs/LANES.md,
  "Model tiers"). Among the lanes that fit the packet, route offers only the lanes whose
  tier the task's category asks for and reports the others in `dropped` with reason
  `tier_mismatch`. The filter never empties the offer: with no lane of the task's tier
  offered, every lane stays, so a board whose records predate the key routes as it did.
- Lane policy: a board that demands hosting and retention guarantees carries
  `require_lane_meta` ({"residency": ["us", "eu"], "retention": ["zero"]}); every
  candidate — an explicit list included — whose `lane_meta` record (the lanes-meta.json
  sidecar, read by the tick, lanes/meta.py) does not satisfy every listed key is dropped
  with reason `lane_policy` naming the key. Unknown never satisfies, so an untagged lane
  is refused rather than trusted, and a requirement that no lane satisfies refuses the
  task: policy decides where work may go at all, before budgets and tiers say where it
  fits. Without the key, route behaves as if the sidecar did not exist.
"""

from math import ceil, gcd

from .go import CONTENT_ALLOWANCE, REASONING_HEADROOM
from .meta import policy_drop, validate_requirement
from .select import select_lane

# go.run's request cap when a task carries no thinking budget.
UNBUDGETED_MAX_TOKENS = 16000

# The tier a task's category asks for: planning and review take the SOTA lanes the
# operator marked, everything else the build workhorses. A lane record that carries no
# tier (or a non-string one) is a workhorse — the same read runner.lane_view does.
TIER_BY_CATEGORY = {"plan": "plan", "independent_review": "review"}
DEFAULT_TIER = "build"


def lane_tier(lane):
    """The lane view's tier: the declared string, else `build`.

    `lanes.json` is validated by provider-authored `lanes/config.py`, whose fixed key set
    has no `tier`, so in practice the key arrives from a lane view a caller assembled (the
    runner's `lane_view` reads it the same way). Anything that is not a string is the
    absent case. A string outside plan/build/review is carried through: it matches no
    category's tier, so a typo shows up as a `tier_mismatch` drop row instead of silently
    demoting a SOTA lane to a workhorse.
    """
    tier = lane.get("tier") if isinstance(lane, dict) else None
    return tier if isinstance(tier, str) else DEFAULT_TIER


def wanted_tier(category):
    """The tier the task's category routes to: plan → plan, independent_review → review,
    everything else (packet, pure_function, multi-file, canary) → build."""
    return TIER_BY_CATEGORY.get(category, DEFAULT_TIER)


def tier_mismatch_row(lane_id, lane, tier):
    """The `dropped` row for a lane that declares another tier than the task's."""
    return {
        "lane": lane_id,
        "reason": "tier_mismatch",
        "detail": f"lane tier {lane_tier(lane)}, task wants {tier}",
    }


# Context windows by model id, for lane views that carry none (lane specs cannot). From the
# providers' own catalogues on 2026-09-15; a model outside the table gets no context check.
MODEL_CONTEXT = {
    "kimi-k3": 1_048_576,
    "glm-5.3-flash": 1_048_576,
    "glm-5.3": 1_048_576,
    "deepseek-v4.1-flash": 262_144,
    "deepseek-v4-flash": 262_144,
    "qwen3.8-max": 262_144,
}


def model_context(model):
    """The catalogue context window for a model id (substring match), else None."""
    if not model:
        return None
    for key, window in MODEL_CONTEXT.items():
        if key in str(model):
            return window
    return None


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


def default_lanes(category, lanes):
    """route's defaulting: every lane declaring the category, minus explicit_only families.

    An explicit_only lane (the first-party claude/openai families) runs only where the
    author named it. The plan node uses this to give a drafted packet the build lanes its
    category is offered by, before any selection has run.
    """
    return [
        lid
        for lid in lanes
        if category in (lanes[lid].get("categories") or []) and not lanes[lid].get("explicit_only")
    ]


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


def route(task, lanes, readiness, scorecard, calibration, now, inputs_bytes, pricing=None):
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

    The tier filter runs on the lanes that fit: the task's category asks for a tier
    (wanted_tier) and, while at least one offered lane declares it, the other lanes are
    dropped with reason `tier_mismatch`. `tier` in the answer is the tier the task asked
    for, so a caller can explain a refusal that has no drop rows. The filter never
    empties the offer — a task whose lanes all declare another tier is routed by readiness
    and score exactly as it was before the key existed — so the budget decides fit and the
    tier decides choice among the lanes that fit.

    Before both, the board's `require_lane_meta` (carried on the task dict the way
    `budget` is) filters the candidates on their `lane_meta` records: a lane failing any
    listed key is dropped `lane_policy` with the key named, and when every candidate
    fails — the untagged included, whose unknown satisfies nothing — the answer is lane
    None, reason `lane_policy`. A malformed requirement raises ValueError: the tick
    refuses rather than routes around its own policy.
    """
    cat = task.get("category") if isinstance(task, dict) else None
    budget = task.get("budget") if isinstance(task, dict) else None
    thinking = budget.get("thinking_tokens") if isinstance(budget, dict) else None
    explicit = task.get("lanes") if isinstance(task, dict) else None
    explicit = explicit if isinstance(explicit, list) else []
    tier = wanted_tier(cat)
    require = task.get("require_lane_meta") if isinstance(task, dict) else None
    if explicit:
        candidates = [lid for lid in lanes if lid in explicit]
    else:
        candidates = default_lanes(cat, lanes)
    if require:
        validate_requirement(require)
        kept_ids, policy_drops = [], []
        for lid in candidates:
            row = policy_drop(lid, lanes[lid].get("lane_meta"), require)
            if row is None:
                kept_ids.append(lid)
            else:
                policy_drops.append(row)
        if not kept_ids:
            return {
                "lane": None,
                "score": None,
                "reason": "lane_policy",
                "candidates": [],
                "dropped": policy_drops,
                "tier": tier,
            }
        candidates = kept_ids
        dropped = policy_drops
    else:
        dropped = []
    prompt = ceil(inputs_bytes / 4)
    # An output cap cannot bound a prompt. What it bounds is the reasoning the model spends
    # on it, which on 2026-09-14's reviews ran about half the prompt (10 279 tokens on a
    # 15 949-token packet overran a 10 000 cap); the prompt itself is bounded by the
    # model's context window, from the lane view or the catalogue below.
    need = prompt // 2 + CONTENT_ALLOWANCE
    kept = []
    for lid in candidates:
        lane = lanes[lid]
        cap = max_tokens_cap(lane, thinking)
        context = lane.get("context") or model_context(lane.get("model"))
        if need > cap:
            dropped.append(
                {
                    "lane": lid,
                    "reason": "budget_unfit",
                    "detail": f"output need ~{need} tokens (prompt/2 + {CONTENT_ALLOWANCE}) exceeds max_tokens cap {cap}",
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
            "tier": tier,
        }
    matching = [row for row in kept if lane_tier(lanes[row["lane"]]) == tier]
    if matching and len(matching) < len(kept):
        # A lane of the task's tier is in play: the other lanes are not offered for this
        # task, and the drop report says which tier each declares.
        dropped.extend(
            tier_mismatch_row(row["lane"], lanes[row["lane"]], tier)
            for row in kept
            if lane_tier(lanes[row["lane"]]) != tier
        )
        kept = matching
    if pricing is not None:
        # O2: quality per dollar over the same constrained set (lanes/value.py). The
        # legacy Laplace-score path below stays for a board without a catalogue.
        from .value import select_by_value

        choice = select_by_value(
            {"category": cat, "author_family": task.get("author_family")},
            {lid: lanes[lid] for lid in (row["lane"] for row in kept)},
            readiness,
            scorecard,
            calibration,
            pricing.get("catalogue") or [],
            benchmarks=pricing.get("benchmarks"),
            inputs_bytes=inputs_bytes,
            rng=pricing.get("rng"),
            explore=pricing.get("explore", 0.1),
            at=pricing.get("at"),
            usage_medians=pricing.get("usage_medians"),
        )
        return {
            "lane": choice["lane"],
            "score": choice["score"],
            "reason": choice["reason"],
            "candidates": sorted(kept, key=lambda row: row["lane"]),
            "dropped": dropped,
            "tier": tier,
            "value_rows": choice.get("rows", []),
            "chosen_by": choice.get("chosen_by"),
            "cost": choice.get("cost"),
            "value": choice.get("value"),
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
        "tier": tier,
    }
