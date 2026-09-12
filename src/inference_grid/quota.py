"""Pure quota admission. Unknown reset never means quota replenishment.

Locally implemented after a provider response truncated; no provider SDK required.
"""

import math
import re
from datetime import UTC, datetime

_RFC3339 = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")


def parse_timestamp(value: str) -> datetime:
    """Parse aware RFC3339, flooring nanoseconds to microseconds; return UTC."""
    match = _RFC3339.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("aware RFC3339 timestamp required")
    base, fraction, offset = match.groups()
    if offset == "-00:00":
        raise ValueError("unknown local offset is not an established UTC offset")
    if offset != "Z" and (int(offset[1:3]) > 23 or int(offset[4:]) > 59):
        raise ValueError("invalid UTC offset")
    suffix = "." + fraction[:6].ljust(6, "0") if fraction else ""
    return datetime.fromisoformat(base + suffix + offset.replace("Z", "+00:00")).astimezone(UTC)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def admit(
    snapshot: dict,
    *,
    now: datetime,
    required_windows: set[str],
    threshold: float,
    ttl_seconds: float,
) -> bool:
    """Admit only fresh, complete measured usage; do not predict resets or spend."""
    try:
        if not isinstance(now, datetime) or now.utcoffset() is None:
            return False
        if not _finite(ttl_seconds) or ttl_seconds <= 0:
            return False
        if not _finite(threshold) or not 0 < threshold <= 100:
            return False
        if not isinstance(required_windows, set) or not required_windows:
            return False
        if any(not isinstance(name, str) or not name.strip() for name in required_windows):
            return False
        if not isinstance(snapshot, dict) or snapshot.get("status") != "ok":
            return False
        age = (now - parse_timestamp(snapshot["observed_at"])).total_seconds()
        if not 0 <= age <= ttl_seconds:
            return False
        windows = snapshot["windows"]
        if not isinstance(windows, list) or len(windows) != len(required_windows):
            return False
        seen = set()
        for window in windows:
            if not isinstance(window, dict):
                return False
            name = window["id"]
            if not isinstance(name, str) or name not in required_windows or name in seen:
                return False
            seen.add(name)
            used = window["used_percent"]
            if not _finite(used) or not 0 <= used <= 100 or used >= threshold:
                return False
            reset = window["resets_at"]
            if reset is not None and parse_timestamp(reset) <= now:
                return False
        return seen == required_windows
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
