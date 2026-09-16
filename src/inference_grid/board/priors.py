"""Quality estimates per (model, category): a published prior the evidence overrides (O3).

A model the grid has never run has no scorecard row, so the old rule scored it 1/2 and
never preferred it over a model with one lucky success; a model with a hundred cheap
failures still got tried. Published benchmarks are a prior, not a verdict: they say where
to start, and the grid's own outcomes say where it ends.

    posterior = Beta(prior_p * strength + accepted + 1,  (1 - prior_p) * strength + rejected + 1)

with `strength` pseudo-outcomes (default 8; 0 when no benchmark applies, i.e. the flat
prior). Twenty real outcomes swamp any benchmark; zero fall back to it.

For reviewers, quality is not "the review attempt completed": it is recall on the
calibration corpus (seeded defects found, no false positives — `calibration_reports`,
the `eval:*` rows M4/M5 write). A reviewer with no calibration record sits on its prior
however many reviews it has returned, and `source` says so.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_BENCHMARKS_PATH = Path.home() / ".config/inference-grid/benchmarks.json"
DEFAULT_STRENGTH = 8
FLAT_PRIOR = 0.5
# Fewer outcomes than this in a category and the lane is explored, not trusted.
TRUST_THRESHOLD = 3

# Which benchmark speaks for which kind of work. A category not listed under any
# benchmark takes the flat prior.
BENCHMARK_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "swe-bench-verified": (
        "packet",
        "bugfix",
        "tests_multi_file",
        "fixtures_multi_file",
        "api-feature",
        "python-feature",
        "frontend-feature",
        "integration",
        "chores",
    ),
    "swe-bench-pro": ("packet", "bugfix", "tests_multi_file"),
    "aider-polyglot": ("packet", "pure_function", "bugfix"),
    "livecodebench": ("pure_function",),
    "code-review": ("independent_review",),
}
REVIEW_CATEGORIES = ("independent_review",)


def bare_model(model: str) -> str:
    """`cline-pass/kimi-k3`, `moonshotai/kimi-k3` and `kimi-k3` are one model."""
    return str(model or "").rsplit("/", 1)[-1].lower()


def load_benchmarks(path: Optional[Path] = None) -> Dict[str, Any]:
    """The operator's benchmarks file, validated; absent = no priors (flat everywhere).

    Shape: {"models": {"<model id>": [{"benchmark", "score", "max_score", "date",
    "source_url"}, ...]}, "aliases": {"<canonical>": ["<alias>", ...]}}. Every score must
    lie in [0, max_score]; every benchmark name must be one this module maps to
    categories; a source_url is required — a number without a source is not a prior.
    """
    path = Path(path) if path else DEFAULT_BENCHMARKS_PATH
    if not path.is_file():
        return {"models": {}, "aliases": {}, "path": str(path), "present": False}
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError("benchmarks.json must be an object")
    models = document.get("models") or {}
    aliases = document.get("aliases") or {}
    if not isinstance(models, dict) or not isinstance(aliases, dict):
        raise ValueError("benchmarks.json: models and aliases must be objects")
    for model, entries in models.items():
        if not isinstance(entries, list):
            raise ValueError(f"benchmarks.json: {model} must list its benchmark entries")
        for e in entries:
            if not isinstance(e, dict):
                raise ValueError(f"benchmarks.json: {model} entry must be an object")
            name = e.get("benchmark")
            if name not in BENCHMARK_CATEGORIES:
                raise ValueError(
                    f"benchmarks.json: {model}: unknown benchmark {name!r}; known: "
                    + ", ".join(sorted(BENCHMARK_CATEGORIES))
                )
            score, max_score = e.get("score"), e.get("max_score", 100)
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise ValueError(f"benchmarks.json: {model}/{name}: score must be a number")
            if (
                not isinstance(max_score, (int, float))
                or max_score <= 0
                or not 0 <= score <= max_score
            ):
                raise ValueError(
                    f"benchmarks.json: {model}/{name}: score {score} outside 0..{max_score}"
                )
            if not isinstance(e.get("source_url"), str) or not e["source_url"]:
                raise ValueError(f"benchmarks.json: {model}/{name}: source_url required")
    for canonical, names in aliases.items():
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError(f"benchmarks.json: aliases for {canonical} must be a list of ids")
    return {"models": models, "aliases": aliases, "path": str(path), "present": True}


def resolve_model(benchmarks: Dict[str, Any], model: str) -> Optional[str]:
    """The benchmarks key this model id names, through aliases or the bare id."""
    models = benchmarks.get("models") or {}
    if model in models:
        return model
    for canonical, names in (benchmarks.get("aliases") or {}).items():
        if model in names or bare_model(model) in (bare_model(n) for n in names):
            if canonical in models:
                return canonical
    bare = bare_model(model)
    for key in models:
        if bare_model(key) == bare:
            return key
    return None


def quality_prior(
    benchmarks: Dict[str, Any], model: str, category: str, strength: int = DEFAULT_STRENGTH
) -> Dict[str, Any]:
    """(p, strength, benchmarks used) for this model and category; flat when none apply."""
    key = resolve_model(benchmarks, model)
    used = []
    if key:
        for e in benchmarks["models"][key]:
            if category in BENCHMARK_CATEGORIES.get(e["benchmark"], ()):
                used.append(
                    {
                        "benchmark": e["benchmark"],
                        "p": float(e["score"]) / float(e.get("max_score", 100)),
                        "date": e.get("date"),
                        "source_url": e.get("source_url"),
                    }
                )
    if not used:
        return {"p": FLAT_PRIOR, "strength": 0, "benchmarks": []}
    p = sum(u["p"] for u in used) / len(used)
    return {"p": p, "strength": strength, "benchmarks": used}


def _evidence(
    model: str,
    family: Optional[str],
    category: str,
    scorecard: Iterable[Dict[str, Any]],
    calibration: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Accepted/attempts the posterior should count, and where they came from."""
    bare = bare_model(model)
    if category in REVIEW_CATEGORIES:
        for report in calibration or []:
            if bare_model(report.get("model", "")) == bare and (
                family is None or report.get("family") == family
            ):
                return {
                    "accepted": int(report.get("accepted", 0)),
                    "attempts": int(report.get("cases", 0)),
                    "source": "calibration",
                }
        return {"accepted": 0, "attempts": 0, "source": "none (review completion is not quality)"}
    accepted = attempts = 0
    for row in scorecard or []:
        if bare_model(row.get("model", "")) == bare and row.get("category") == category:
            accepted += int(row.get("accepted", 0))
            attempts += int(row.get("attempts", 0))
    return {
        "accepted": accepted,
        "attempts": attempts,
        "source": "scorecard" if attempts else "none",
    }


def quality_estimate(
    model: str,
    category: str,
    scorecard=None,
    calibration=None,
    benchmarks: Optional[Dict[str, Any]] = None,
    family: Optional[str] = None,
    strength: int = DEFAULT_STRENGTH,
) -> Dict[str, Any]:
    """The posterior mean quality with its inputs; `alpha`/`beta` let a caller sample it."""
    benchmarks = benchmarks or {"models": {}, "aliases": {}}
    prior = quality_prior(benchmarks, model, category, strength)
    ev = _evidence(model, family, category, scorecard or [], calibration or [])
    rejected = max(ev["attempts"] - ev["accepted"], 0)
    alpha = prior["p"] * prior["strength"] + ev["accepted"] + 1
    beta = (1 - prior["p"]) * prior["strength"] + rejected + 1
    sources = []
    if prior["strength"]:
        sources.append("benchmark:" + "+".join(b["benchmark"] for b in prior["benchmarks"]))
    if ev["attempts"]:
        sources.append(ev["source"])
    return {
        "model": model,
        "category": category,
        "mean": alpha / (alpha + beta),
        "alpha": alpha,
        "beta": beta,
        "evidence_n": ev["attempts"],
        "accepted": ev["accepted"],
        "prior_p": prior["p"],
        "prior_strength": prior["strength"],
        "source": " + ".join(sources) if sources else "flat prior",
        "trusted": ev["attempts"] >= TRUST_THRESHOLD,
    }


def sample_quality(estimate: Dict[str, Any], rng) -> float:
    """One Thompson draw from the estimate's Beta posterior."""
    return rng.betavariate(max(estimate["alpha"], 1e-9), max(estimate["beta"], 1e-9))


def quality_table(ledger, benchmarks_path=None, category=None) -> List[Dict[str, Any]]:
    """Every (model, category) the ledger or the benchmarks know, with the estimate."""
    from .runner import calibration_reports

    benchmarks = load_benchmarks(benchmarks_path)
    scorecard = ledger.scorecard()
    calibration = calibration_reports(ledger)
    pairs = set()
    for row in scorecard:
        if row.get("category") and not str(row["category"]).startswith("eval:"):
            pairs.add((row["model"], row["family"], row["category"]))
    for report in calibration:
        pairs.add((report["model"], report["family"], "independent_review"))
    for model, entries in (benchmarks.get("models") or {}).items():
        for e in entries:
            for cat in BENCHMARK_CATEGORIES.get(e["benchmark"], ()):
                pairs.add((model, None, cat))
    out = []
    for model, family, cat in sorted(pairs, key=lambda p: (p[2], p[0], str(p[1]))):
        if category and cat != category:
            continue
        est = quality_estimate(model, cat, scorecard, calibration, benchmarks, family=family)
        est["family"] = family
        est["mean"] = round(est["mean"], 4)
        out.append(est)
    return out


def log_odds(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))
