"""Plan-unit conversion for normalized observations. Authored by OpenCode Go (glm-5.3-flash)
through a Grid attempt; integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

import math


def remaining_units(observation, plan_units):
    if not isinstance(plan_units, dict) or not plan_units:
        raise ValueError("plan_units must be a non-empty dict")
    for k, v in plan_units.items():
        if not isinstance(k, str) or not k:
            raise ValueError("plan_units keys must be non-empty strings")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("plan_units values must be int/float")
        if not math.isfinite(v) or v <= 0:
            raise ValueError("plan_units values must be finite and > 0")
    if not isinstance(observation, dict):
        raise ValueError("observation must be a dict")
    windows = observation.get("windows")
    if not isinstance(windows, list):
        raise ValueError("observation windows must be a list")
    by_id = {}
    for w in windows:
        if not isinstance(w, dict):
            raise ValueError("windows entries must be dicts")
        wid = w.get("id")
        if wid in by_id:
            by_id[wid] = None
        else:
            by_id[wid] = w
    result = {}
    for k, allowance in plan_units.items():
        if k not in by_id or by_id[k] is None:
            raise ValueError("plan key must match exactly one window id")
        up = by_id[k].get("used_percent")
        if up is None:
            raise ValueError("used_percent is unknown")
        if isinstance(up, bool) or not isinstance(up, (int, float)):
            raise ValueError("used_percent must be int/float")
        if not math.isfinite(up) or up < 0 or up > 100:
            raise ValueError("used_percent out of range")
        result[k] = float(math.floor(allowance * (100 - up) / 100 * 1e6) / 1e6)
    return result
