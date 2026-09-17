"""Static tier-table routing (B4): tier, then cross-family, then remaining window quota.

No learned score, no bandit, no per-dollar value: a task's category asks for a tier
(plan | build | review, docs/LANES.md "Model tiers") and, among the lanes of that tier
that fit the packet and the board's policy, the one candidate a review must not share
the task's author family with, and whose account has the most remaining quota this
window, wins — ties break on lane id, so the choice is always reproducible. route
prepares select_lane's constraints and the quota tiebreak, and carries the answer back
with the drop report:

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
- Selection: among what tier/budget/policy left, a lane not `ready` (readiness view) or
  sharing the task's `author_family` (the cross-family rule: a review may not be graded
  by its own author) or with a closed campaign window is not offered at all — this is
  select_lane's own hard-constraint logic, kept unmodified in lanes/select.py. Of what
  remains, `quota` (lane id -> that lane's account's tightest remaining window, the same
  reading the capacity dashboard's headline uses) picks the lane with the most headroom;
  a lane the caller has no quota reading for is last, not dropped. `score` in the answer
  is the winning lane's quota reading, so a dry run explains every choice by tier and
  quota alone — never a learned quality number.
"""

from math import ceil

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


def route(task, lanes, readiness, now, inputs_bytes, quota=None):
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
    # B4: select_lane's own hard constraints (ready, cross-family, window) decide what
    # is offered at all — kept unmodified, called with no scorecard so every lane ties
    # at its default score and `candidates` is exactly the constrained set. The winner
    # among that set is re-ranked by remaining window quota, not select_lane's score.
    choice = select_lane(
        {"category": cat, "author_family": task.get("author_family")},
        {lid: lanes[lid] for lid in (row["lane"] for row in kept)},
        readiness,
        [],
        now,
    )
    if choice["reason"] != "selected":
        return {
            "lane": None,
            "score": None,
            "reason": choice["reason"],
            "candidates": sorted(kept, key=lambda row: row["lane"]),
            "dropped": dropped,
            "tier": tier,
        }
    quota = quota or {}
    quota_rows = [{"lane": lid, "quota": quota.get(lid)} for lid in choice["candidates"]]
    winner = sorted(choice["candidates"], key=lambda lid: (-(quota.get(lid) or 0.0), lid))[0]
    return {
        "lane": winner,
        "score": quota.get(winner),
        "reason": "selected",
        "candidates": sorted(kept, key=lambda row: row["lane"]),
        "dropped": dropped,
        "tier": tier,
        "quota_rows": quota_rows,
    }
