"""Lane selection on quality per dollar (O2): price and evidence decide, the rules constrain.

`select_lane` (lanes/select.py, provider-authored, kept unmodified) picks the highest
smoothed acceptance rate and knows nothing of price: on 2026-09-16 that put 121 reviews on
Kimi K3 at $0.12 each while Qwen3.8 Flash on the same plan would have cost $0.006, and a
free model could not be preferred by any rule. `select_by_value` keeps every hard
constraint select_lane applies — category, the cross-family rule for reviews, readiness,
window — and then ranks by

    value = quality(model, category).mean / expected_cost(lane, task)

quality from board/priors.py (benchmark prior, the grid's own outcomes, calibration recall
for reviewers) and cost from the catalogue (catalogue.py: promo, free, cache prices) times
the task's expected tokens. A candidate with fewer than TRUST_THRESHOLD outcomes in the
category is *explored*: its quality is a Thompson draw, not the mean, so a new cheap model
earns its trials without being trusted; with probability `explore` the whole choice is a
draw. Every row the caller prints carries value, cost, quality, evidence_n and chosen_by.

No special cases: Kimi K3 falls to last reviewer choice by its price, not by name; a
frontier model on a credit plan competes on the same footing.
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..board.priors import quality_estimate, sample_quality
from ..catalogue import FREE_COST_FLOOR_USD, cost_estimate

# Which catalogue provider a lane's `provider` reads from.
CATALOGUE_PROVIDERS = {
    "opencode": "opencode",
    "go": "opencode",
    "goat": "command-code",
    "command-code": "command-code",
    "zai": "zai",
    "zcode": "zai",
    "clinepass": "clinepass",
    "cline": "clinepass",
}
# Expected tokens per task category when nothing better is known: (input, output, cache share
# of input). A review's input is its staged bytes (the caller passes them); a packet is an
# agent loop — the 2026-09-16 transcripts averaged ~5M input tokens, 85% cache reads, 100k out.
DEFAULT_TOKENS = {
    "independent_review": (30_000, 2_000, 0.0),
    "canary": (2_000, 50, 0.0),
    "pure_function": (20_000, 3_000, 0.0),
    "tests_multi_file": (40_000, 5_000, 0.0),
    "fixtures_multi_file": (40_000, 5_000, 0.0),
    "packet": (5_000_000, 100_000, 0.85),
    "calibration_run": (30_000, 2_000, 0.0),
}
BYTES_PER_TOKEN = 3.5
DEFAULT_EXPLORE = 0.1
# Below this posterior mean a TRUSTED lane is not a candidate however cheap it is: a
# reviewer that finds one defect in twelve is not a cheaper reviewer, it is not a reviewer,
# and per-dollar arithmetic alone would still pick it (0.08/$0.006 beats 0.92/$0.12). An
# untrusted lane is explored, not floored — it has not earned a verdict yet. When every
# candidate is under its floor the best of them is still chosen, and the row says so.
QUALITY_FLOOR = {"independent_review": 0.6, "packet": 0.5, "canary": 0.0}
DEFAULT_QUALITY_FLOOR = 0.4


def catalogue_row(
    catalogue: Iterable[Dict[str, Any]], lane: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """The catalogue row for this lane's model on its plan, matched exactly then by bare id."""
    provider = CATALOGUE_PROVIDERS.get(str(lane.get("provider") or ""), lane.get("provider"))
    model = str(lane.get("model") or "")
    bare = model.rsplit("/", 1)[-1].lower()
    exact = None
    loose = None
    for row in catalogue:
        if row.get("provider") != provider:
            continue
        rid = str(row.get("model") or "")
        if rid == model:
            exact = row
            break
        if rid.rsplit("/", 1)[-1].lower() == bare and loose is None:
            loose = row
    return exact or loose


def expected_tokens(
    category: str,
    inputs_bytes: Optional[int] = None,
    usage_medians: Optional[Dict[str, Tuple[float, float, float]]] = None,
    lane_kind: Optional[str] = None,
) -> Tuple[float, float, float]:
    """(input_tokens, output_tokens, cache_share). Observed medians per lane kind (N1) win;
    then a review's staged bytes; then the category default."""
    if usage_medians and lane_kind in usage_medians:
        return tuple(usage_medians[lane_kind])  # type: ignore[return-value]
    base = DEFAULT_TOKENS.get(category, (30_000, 2_000, 0.0))
    if inputs_bytes and category not in ("packet",):
        return (max(inputs_bytes / BYTES_PER_TOKEN, 1.0), base[1], base[2])
    return base


def expected_cost(
    row: Optional[Dict[str, Any]], tokens: Tuple[float, float, float], at: Optional[date] = None
) -> Optional[float]:
    inp, out, cache_share = tokens
    cached = inp * cache_share
    return cost_estimate(row, inp - cached, out, cached, at=at)


def select_by_value(
    task: Dict[str, Any],
    lanes: Dict[str, Dict[str, Any]],
    readiness: Dict[str, Dict[str, Any]],
    scorecard: Iterable[Dict[str, Any]],
    calibration: Iterable[Dict[str, Any]],
    catalogue: Iterable[Dict[str, Any]],
    benchmarks: Optional[Dict[str, Any]] = None,
    inputs_bytes: Optional[int] = None,
    rng: Optional[random.Random] = None,
    explore: float = DEFAULT_EXPLORE,
    at: Optional[date] = None,
    usage_medians: Optional[Dict[str, Tuple[float, float, float]]] = None,
) -> Dict[str, Any]:
    """select_lane's answer shape plus `rows`: one priced, scored row per candidate.

    A lane without a catalogue price is not dropped — the plan may simply be unpriced —
    but it can only be chosen on quality alone when no priced lane competes, and its row
    says `cost: None`. `reason` is `selected` (or select_lane's own reasons when nothing
    passes the constraints); `chosen_by` is `value`, `explore` or `quality_only`.
    """
    cat = task.get("category")
    if not isinstance(cat, str) or not cat:
        raise ValueError("task must have a non-empty string category")
    author = task.get("author_family")
    rng = rng or random.Random()
    catalogue = list(catalogue or [])
    ready = {lid for lid in lanes if readiness.get(lid, {}).get("state") == "ready"}
    cat_match = {lid for lid in ready if cat in (lanes[lid].get("categories") or [])}
    fam_ok = {
        lid
        for lid in cat_match
        if not (isinstance(author, str) and lanes[lid].get("family") == author)
    }
    win_ok = {lid for lid in fam_ok if lanes[lid].get("window_active") is not False}
    if not win_ok:
        reason = (
            "no_ready_lane"
            if not ready
            else "no_lane_for_category"
            if not cat_match
            else "no_independent_family"
            if not fam_ok
            else "window_closed"
        )
        return {
            "lane": None,
            "score": None,
            "reason": reason,
            "candidates": [],
            "rows": [],
            "chosen_by": None,
        }

    rows: List[Dict[str, Any]] = []
    for lid in sorted(win_ok):
        lane = lanes[lid]
        est = quality_estimate(
            lane.get("model", ""),
            cat,
            scorecard,
            calibration,
            benchmarks,
            family=lane.get("family"),
        )
        crow = catalogue_row(catalogue, lane)
        tokens = expected_tokens(cat, inputs_bytes, usage_medians, lane.get("kind"))
        cost = expected_cost(crow, tokens, at)
        quality = est["mean"] if est["trusted"] else sample_quality(est, rng)
        rows.append(
            {
                "lane": lid,
                "model": lane.get("model"),
                "family": lane.get("family"),
                "quality": quality,
                "quality_mean": est["mean"],
                "evidence_n": est["evidence_n"],
                "quality_source": est["source"],
                "trusted": est["trusted"],
                "cost": cost,
                "free": bool(crow and crow.get("free")),
                "promo": (crow or {}).get("promo"),
                "priced": cost is not None,
                "value": (quality / cost) if cost else None,
                "_estimate": est,
            }
        )
    floor = QUALITY_FLOOR.get(cat, DEFAULT_QUALITY_FLOOR)
    for r in rows:
        r["under_floor"] = bool(r["trusted"] and r["quality_mean"] < floor)
    above = [r for r in rows if not r["under_floor"]]
    if above:
        rows_considered = above
    else:
        rows_considered = rows  # nothing clears the floor: the least bad, said plainly
    priced = [r for r in rows_considered if r["priced"]]
    chosen_by = "value"
    if priced:
        if rng.random() < explore:
            chosen_by = "explore"
            for r in priced:
                r["quality"] = sample_quality(r["_estimate"], rng)
                r["value"] = r["quality"] / r["cost"]
        # Ties on value: more evidence, then lane id, so the choice is reproducible.
        top = max(r["value"] for r in priced)
        best = sorted(
            [r for r in priced if r["value"] == top], key=lambda r: (-r["evidence_n"], r["lane"])
        )[0]
    else:
        chosen_by = "quality_only"
        top = max(r["quality"] for r in rows_considered)
        best = sorted(
            [r for r in rows_considered if r["quality"] == top],
            key=lambda r: (-r["evidence_n"], r["lane"]),
        )[0]
    if not above:
        chosen_by += "_under_floor"
    for r in rows:
        r["chosen_by"] = chosen_by if r is best else None
        r.pop("_estimate", None)
        for k in ("quality", "quality_mean", "value"):
            if isinstance(r[k], float):
                r[k] = round(r[k], 6)
        if isinstance(r["cost"], float):
            r["cost"] = round(r["cost"], 8)
    return {
        "lane": best["lane"],
        "score": best["quality_mean"],
        "reason": "selected",
        "candidates": sorted(win_ok),
        "rows": rows,
        "chosen_by": chosen_by,
        "cost": best["cost"],
        "value": best["value"],
    }


def is_free_cost(cost: Optional[float]) -> bool:
    return cost is not None and cost <= FREE_COST_FLOOR_USD
