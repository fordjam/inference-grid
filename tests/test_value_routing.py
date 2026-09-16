"""Quality priors (O3) and quality-per-dollar selection (O2)."""

import json
import random
from datetime import date

import pytest

from inference_grid import catalogue as C
from inference_grid.board import priors
from inference_grid.lanes import value
from inference_grid.lanes.route import route


def benchmarks_file(tmp_path, models=None, aliases=None):
    doc = {"models": models or {}, "aliases": aliases or {}}
    p = tmp_path / "benchmarks.json"
    p.write_text(json.dumps(doc))
    return p


# ---------------------------------------------------------------------------
# O3: priors
# ---------------------------------------------------------------------------
def test_missing_benchmarks_file_is_the_flat_prior(tmp_path):
    b = priors.load_benchmarks(tmp_path / "nope.json")
    assert b["present"] is False
    est = priors.quality_estimate("qwen3.8-flash", "packet", benchmarks=b)
    assert est["mean"] == 0.5 and est["prior_strength"] == 0 and est["source"] == "flat prior"
    assert est["trusted"] is False and est["evidence_n"] == 0


def test_benchmark_prior_is_overridden_by_twenty_outcomes(tmp_path):
    p = benchmarks_file(
        tmp_path,
        {
            "kimi-k3": [
                {
                    "benchmark": "swe-bench-verified",
                    "score": 72,
                    "max_score": 100,
                    "date": "2026-08-01",
                    "source_url": "https://example.test/swe",
                }
            ]
        },
        aliases={"kimi-k3": ["moonshotai/kimi-k3", "cline-pass/kimi-k3"]},
    )
    b = priors.load_benchmarks(p)
    zero = priors.quality_estimate("moonshotai/kimi-k3", "packet", benchmarks=b)
    # 8 pseudo-outcomes at 0.72 plus the Laplace 1/1: (5.76+1)/(8+2)
    assert zero["mean"] == pytest.approx((0.72 * 8 + 1) / 10)
    assert zero["source"] == "benchmark:swe-bench-verified" and zero["prior_strength"] == 8
    twenty = priors.quality_estimate(
        "kimi-k3",
        "packet",
        scorecard=[
            {"model": "cline-pass/kimi-k3", "category": "packet", "attempts": 20, "accepted": 2}
        ],
        benchmarks=b,
    )
    # Twenty real outcomes at 10% swamp a 72% benchmark.
    assert twenty["mean"] < 0.3 and twenty["evidence_n"] == 20 and twenty["trusted"]
    assert "scorecard" in twenty["source"] and "benchmark" in twenty["source"]
    # A category no benchmark maps to falls back to flat.
    assert priors.quality_estimate("kimi-k3", "canary", benchmarks=b)["prior_strength"] == 0


def test_reviewer_quality_is_calibration_recall_not_completion():
    scorecard = [
        {
            "model": "kimi-k3",
            "family": "kimi",
            "category": "independent_review",
            "attempts": 113,
            "accepted": 97,
        }
    ]
    no_cal = priors.quality_estimate(
        "kimi-k3", "independent_review", scorecard=scorecard, family="kimi"
    )
    assert no_cal["evidence_n"] == 0 and no_cal["mean"] == 0.5
    assert "completion is not quality" in no_cal["source"] or no_cal["source"] == "flat prior"
    with_cal = priors.quality_estimate(
        "kimi-k3",
        "independent_review",
        scorecard=scorecard,
        calibration=[{"family": "kimi", "model": "kimi-k3", "cases": 5, "accepted": 4}],
        family="kimi",
    )
    assert with_cal["evidence_n"] == 5 and with_cal["mean"] == pytest.approx(5 / 7)
    assert with_cal["source"] == "calibration" and with_cal["trusted"]


def test_benchmarks_validation_refuses_junk(tmp_path):
    with pytest.raises(ValueError, match="outside 0..100"):
        priors.load_benchmarks(
            benchmarks_file(
                tmp_path,
                {"m": [{"benchmark": "aider-polyglot", "score": 120, "source_url": "https://x"}]},
            )
        )
    with pytest.raises(ValueError, match="unknown benchmark"):
        priors.load_benchmarks(
            benchmarks_file(
                tmp_path, {"m": [{"benchmark": "vibes", "score": 1, "source_url": "https://x"}]}
            )
        )
    with pytest.raises(ValueError, match="source_url required"):
        priors.load_benchmarks(
            benchmarks_file(tmp_path, {"m": [{"benchmark": "aider-polyglot", "score": 50}]})
        )


# ---------------------------------------------------------------------------
# O2: selection
# ---------------------------------------------------------------------------
def cat_rows():
    return [
        dict(
            C.normalise(
                {
                    "model": "kimi-k3",
                    "input_per_m": 3.0,
                    "output_per_m": 15.0,
                    "monthly_limit_usd": 15,
                }
            ),
            provider="opencode",
        ),
        dict(
            C.normalise({"model": "qwen3.8-flash", "input_per_m": 0.15, "output_per_m": 0.47}),
            provider="opencode",
        ),
        dict(
            C.normalise(
                {
                    "model": "deepseek-v4.1-flash",
                    "input_per_m": 0.15,
                    "output_per_m": 0.6,
                    "promo_multiplier": 4.0,
                    "promo_ends": "2026-09-20",
                }
            ),
            provider="opencode",
        ),
        dict(C.normalise({"model": "union-alpha", "free": True}), provider="opencode"),
        dict(
            C.normalise({"model": "z-ai/glm-5.3-flash", "input_per_m": 0.02, "output_per_m": 0.02}),
            provider="command-code",
        ),
    ]


def lanes():
    def go(model, family):
        return {
            "provider": "opencode",
            "family": family,
            "model": model,
            "kind": "go_http",
            "categories": ["independent_review", "canary"],
            "max_concurrency": 1,
            "wall_seconds": 400,
        }

    return {
        "go-kimi": go("kimi-k3", "kimi"),
        "go-qwen": go("qwen3.8-flash", "qwen"),
        "go-deepseek": go("deepseek-v4.1-flash", "deepseek"),
        "go-union-alpha": go("union-alpha", "union"),
        "goat": {
            "provider": "goat",
            "family": "glm",
            "model": "z-ai/glm-5.3-flash",
            "kind": "goat_cli",
            "categories": ["independent_review", "packet"],
            "max_concurrency": 2,
            "wall_seconds": 1800,
        },
    }


def ready(lanes_):
    return {lid: {"state": "ready", "qualified_for": ["independent_review"]} for lid in lanes_}


def trusted_calibration():
    return [
        {"family": "kimi", "model": "kimi-k3", "cases": 5, "accepted": 4},
        {"family": "qwen", "model": "qwen3.8-flash", "cases": 5, "accepted": 4},
        {"family": "deepseek", "model": "deepseek-v4.1-flash", "cases": 5, "accepted": 4},
        {"family": "union", "model": "union-alpha", "cases": 5, "accepted": 4},
        {"family": "glm", "model": "z-ai/glm-5.3-flash", "cases": 5, "accepted": 4},
    ]


def test_free_first_then_promo_then_price_at_equal_quality():
    task = {"category": "independent_review", "author_family": "kimi"}
    out = value.select_by_value(
        task,
        lanes(),
        ready(lanes()),
        [],
        trusted_calibration(),
        cat_rows(),
        rng=random.Random(1),
        explore=0.0,
        at=date(2026, 9, 16),
        inputs_bytes=100_000,
    )
    order = [r["lane"] for r in sorted(out["rows"], key=lambda r: -r["value"])]
    # kimi excluded: same family as the author. Free leads; the 4x promo beats Qwen's list
    # price; Command Code's observed rate is cheaper still and lands between them.
    assert "go-kimi" not in order
    assert order[0] == "go-union-alpha" and order.index("go-deepseek") < order.index("go-qwen")
    assert out["lane"] == "go-union-alpha" and out["chosen_by"] == "value"
    assert out["rows"][0]["cost"] is not None and all(
        r["quality_source"] == "calibration" for r in out["rows"]
    )


def test_kimi_is_last_by_price_not_by_name():
    task = {"category": "independent_review", "author_family": "glm"}
    out = value.select_by_value(
        task,
        lanes(),
        ready(lanes()),
        [],
        trusted_calibration(),
        cat_rows(),
        rng=random.Random(1),
        explore=0.0,
        at=date(2026, 9, 16),
        inputs_bytes=100_000,
    )
    by = {r["lane"]: r for r in out["rows"]}
    assert "goat" not in by  # glm reviewer excluded for glm work
    assert by["go-kimi"]["value"] == min(r["value"] for r in out["rows"])
    assert by["go-kimi"]["cost"] > by["go-qwen"]["cost"] * 15


def test_after_the_promo_ends_deepseek_is_priced_normally():
    task = {"category": "independent_review", "author_family": "glm"}
    during = value.select_by_value(
        task,
        lanes(),
        ready(lanes()),
        [],
        trusted_calibration(),
        cat_rows(),
        rng=random.Random(1),
        explore=0.0,
        at=date(2026, 9, 19),
        inputs_bytes=100_000,
    )
    after = value.select_by_value(
        task,
        lanes(),
        ready(lanes()),
        [],
        trusted_calibration(),
        cat_rows(),
        rng=random.Random(1),
        explore=0.0,
        at=date(2026, 9, 21),
        inputs_bytes=100_000,
    )
    ds_during = next(r for r in during["rows"] if r["lane"] == "go-deepseek")["cost"]
    ds_after = next(r for r in after["rows"] if r["lane"] == "go-deepseek")["cost"]
    assert ds_after == pytest.approx(ds_during * 4, rel=1e-4)


def test_evidence_beats_price_when_the_cheap_lane_is_bad():
    task = {"category": "independent_review", "author_family": "glm"}
    calibration = [
        {"family": "kimi", "model": "kimi-k3", "cases": 10, "accepted": 10},
        {"family": "qwen", "model": "qwen3.8-flash", "cases": 10, "accepted": 0},
        {"family": "deepseek", "model": "deepseek-v4.1-flash", "cases": 10, "accepted": 0},
        {"family": "union", "model": "union-alpha", "cases": 10, "accepted": 0},
    ]
    rows_no_free = [r for r in cat_rows() if r["model"] != "union-alpha"]
    lanes_ = {k: v for k, v in lanes().items() if k != "go-union-alpha"}
    out = value.select_by_value(
        task,
        lanes_,
        ready(lanes_),
        [],
        calibration,
        rows_no_free,
        rng=random.Random(1),
        explore=0.0,
        at=date(2026, 9, 16),
        inputs_bytes=100_000,
    )
    # Per dollar alone the 1/12-quality lanes would still win (0.08/$0.006 > 0.92/$0.12);
    # the quality floor removes trusted lanes below 0.6 recall, so Kimi is the reviewer.
    assert out["lane"] == "go-kimi" and out["chosen_by"] == "value"
    by = {r["lane"]: r for r in out["rows"]}
    assert by["go-qwen"]["under_floor"] and not by["go-kimi"]["under_floor"]
    # And when nothing clears the floor, the least bad is chosen and says so.
    worst = [{"family": "kimi", "model": "kimi-k3", "cases": 10, "accepted": 1}] + calibration[1:]
    out = value.select_by_value(
        task,
        lanes_,
        ready(lanes_),
        [],
        worst,
        rows_no_free,
        rng=random.Random(1),
        explore=0.0,
        at=date(2026, 9, 16),
        inputs_bytes=100_000,
    )
    assert out["chosen_by"] == "value_under_floor"


def test_untrusted_lanes_are_sampled_and_explore_is_seeded():
    task = {"category": "independent_review", "author_family": "glm"}
    out = value.select_by_value(
        task,
        lanes(),
        ready(lanes()),
        [],
        [],
        cat_rows(),
        rng=random.Random(7),
        explore=0.0,
        at=date(2026, 9, 16),
        inputs_bytes=100_000,
    )
    assert all(r["trusted"] is False and r["evidence_n"] == 0 for r in out["rows"])
    # Sampled quality varies per lane; the mean is the flat prior for all of them.
    assert all(r["quality_mean"] == 0.5 for r in out["rows"])
    a = value.select_by_value(
        task,
        lanes(),
        ready(lanes()),
        [],
        trusted_calibration(),
        cat_rows(),
        rng=random.Random(3),
        explore=1.0,
        at=date(2026, 9, 16),
        inputs_bytes=100_000,
    )
    b = value.select_by_value(
        task,
        lanes(),
        ready(lanes()),
        [],
        trusted_calibration(),
        cat_rows(),
        rng=random.Random(3),
        explore=1.0,
        at=date(2026, 9, 16),
        inputs_bytes=100_000,
    )
    assert a["chosen_by"] == "explore" and a["lane"] == b["lane"]


def test_unpriced_lanes_fall_back_to_quality_only():
    task = {"category": "independent_review", "author_family": "glm"}
    out = value.select_by_value(
        task,
        lanes(),
        ready(lanes()),
        [],
        trusted_calibration(),
        [],
        rng=random.Random(1),
        explore=0.0,
        at=date(2026, 9, 16),
    )
    assert out["chosen_by"] == "quality_only" and all(r["cost"] is None for r in out["rows"])
    assert out["lane"] is not None


def test_hard_constraints_still_refuse():
    task = {"category": "independent_review", "author_family": "glm"}
    lanes_ = {"goat": lanes()["goat"]}
    out = value.select_by_value(task, lanes_, ready(lanes_), [], [], cat_rows())
    assert out["lane"] is None and out["reason"] == "no_independent_family"
    out = value.select_by_value(
        task, lanes(), {lid: {"state": "stale"} for lid in lanes()}, [], [], cat_rows()
    )
    assert out["reason"] == "no_ready_lane"


def test_route_uses_the_value_path_only_with_pricing():
    task = {
        "category": "independent_review",
        "author_family": "glm",
        "lanes": ["go-kimi", "go-qwen"],
        "budget": {"wall_seconds": 400, "output_bytes": 100000, "thinking_tokens": None},
    }
    lanes_ = {k: dict(v, lane_meta=None) for k, v in lanes().items() if k in ("go-kimi", "go-qwen")}
    legacy = route(task, lanes_, ready(lanes_), [], [], 0.0, 40_000)
    assert "value_rows" not in legacy
    priced = route(
        task,
        lanes_,
        ready(lanes_),
        [],
        trusted_calibration(),
        0.0,
        40_000,
        pricing={
            "catalogue": cat_rows(),
            "rng": random.Random(1),
            "explore": 0.0,
            "at": date(2026, 9, 16),
        },
    )
    assert priced["lane"] == "go-qwen" and priced["chosen_by"] == "value"
    assert {r["lane"] for r in priced["value_rows"]} == {"go-kimi", "go-qwen"}
    assert priced["cost"] < 0.01
