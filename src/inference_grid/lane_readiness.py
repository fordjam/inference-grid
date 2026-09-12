"""Per-lane readiness classification. Authored by OpenCode Go (glm-5.3-flash) through a Grid
attempt; integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

import math

def _num(v, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("bad number")
    if lo is not None and v < lo:
        raise ValueError("out of range")
    if hi is not None and v > hi:
        raise ValueError("out of range")
    return v

def _opt_num(v, lo=None, hi=None):
    if v is not None:
        _num(v, lo, hi)
    return v

def lane_readiness(lane, now):
    _num(now)
    if not isinstance(lane, dict):
        raise ValueError("lane must be dict")
    spec = {"provider": None, "auth": None, "quota_observed_at": None,
            "quota_freshness_seconds": None, "used_percent_max": None,
            "admission_limit_percent": None, "cooldown_until": None,
            "qualification": None, "blocked_until": None, "blocker": None}
    if set(lane) != set(spec):
        raise ValueError("bad keys")
    p = lane["provider"]
    if not isinstance(p, str) or not p or len(p) > 40:
        raise ValueError("bad provider")
    if lane["auth"] not in ("ok", "expired", "unknown"):
        raise ValueError("bad auth")
    _opt_num(lane["quota_observed_at"])
    _num(lane["quota_freshness_seconds"], lo=0)
    if lane["quota_freshness_seconds"] <= 0:
        raise ValueError("bad freshness")
    _opt_num(lane["used_percent_max"], 0, 100)
    _num(lane["admission_limit_percent"], 0, 100)
    _opt_num(lane["cooldown_until"])
    if lane["qualification"] not in ("unqualified", "effort_controlled", "normalizer", "representative_job", "qualified"):
        raise ValueError("bad qualification")
    _opt_num(lane["blocked_until"])
    b = lane["blocker"]
    if b is not None and (not isinstance(b, str) or len(b) > 200):
        raise ValueError("bad blocker")
    if lane["blocked_until"] is not None and lane["blocked_until"] > now:
        r = lane["blocker"] if isinstance(lane["blocker"], str) and lane["blocker"] else "operator_block"
        return {"provider": p, "state": "blocked", "reason": r, "next_check_at": lane["blocked_until"]}
    if lane["auth"] == "expired":
        return {"provider": p, "state": "blocked", "reason": "auth_expired", "next_check_at": None}
    if lane["cooldown_until"] is not None and lane["cooldown_until"] > now:
        return {"provider": p, "state": "cooling", "reason": "usage_cooldown", "next_check_at": lane["cooldown_until"]}
    if lane["quota_observed_at"] is None:
        return {"provider": p, "state": "stale", "reason": "quota_unobserved", "next_check_at": None}
    if now - lane["quota_observed_at"] > lane["quota_freshness_seconds"]:
        return {"provider": p, "state": "stale", "reason": "quota_stale", "next_check_at": None}
    if lane["used_percent_max"] is not None and lane["used_percent_max"] >= lane["admission_limit_percent"]:
        return {"provider": p, "state": "exhausted", "reason": "admission_limit", "next_check_at": None}
    if lane["qualification"] != "qualified":
        return {"provider": p, "state": "unqualified", "reason": lane["qualification"], "next_check_at": None}
    if lane["auth"] == "unknown":
        return {"provider": p, "state": "unverified", "reason": "auth_unknown", "next_check_at": None}
    return {"provider": p, "state": "ready", "reason": "ready", "next_check_at": lane["quota_observed_at"] + lane["quota_freshness_seconds"]}
