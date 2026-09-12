"""Reset instant from a rounded provider display string. Authored by OpenCode Go (glm-5.3-flash)
through a Grid attempt; integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

import re
import math

_UNIT_SEC = {"day": 86400, "hour": 3600, "minute": 60, "second": 1}


def reset_instant(observed_ts, reset_display):
    if isinstance(observed_ts, bool) or not isinstance(observed_ts, (int, float)) or not math.isfinite(observed_ts):
        raise ValueError("observed_ts must be a non-bool finite number")
    if reset_display is None:
        return None
    if not isinstance(reset_display, str):
        raise ValueError("reset_display must be a str or None")
    s = reset_display.strip()
    if not s:
        return None
    if not re.fullmatch(r"(?:\d+\s+(?:days?|hours?|minutes?|seconds?)\s*,?\s*)+", s, re.IGNORECASE):
        raise ValueError("unparseable reset display")
    pairs = re.findall(r"(\d+)\s+(days?|hours?|minutes?|seconds?)", s, re.IGNORECASE)
    total = 0
    precision = None
    seen = set()
    for num, unit in pairs:
        key = unit.lower().rstrip("s")
        if key in seen:
            raise ValueError("repeated unit")
        seen.add(key)
        total += _UNIT_SEC[key] * int(num)
        if precision is None or _UNIT_SEC[key] < precision:
            precision = _UNIT_SEC[key]
    base = float(observed_ts)
    return {"resets_at": max(base + total, base), "precision_seconds": float(precision)}