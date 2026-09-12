"""Strict provider observation normalizer. Authored by OpenCode Go (glm-5.3-flash) through a
Grid attempt; integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

import math
from datetime import datetime

_PROVIDERS = ("codex", "claude", "clinepass", "command-code", "opencode")
_IDS = ("five_hour", "weekly", "monthly")


def _parse_ts(s):
    if not isinstance(s, str):
        raise ValueError("timestamp must be str")
    txt = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = datetime.fromisoformat(txt)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive timestamp not allowed")
    return dt.timestamp()


def normalize_observation(raw, now):
    if not isinstance(raw, dict):
        raise ValueError("raw must be dict")
    provider = raw.get("provider")
    if provider not in _PROVIDERS:
        raise ValueError("invalid provider")
    oa = raw.get("observed_at")
    if not isinstance(oa, str):
        raise ValueError("observed_at must be str")
    obs_ts = _parse_ts(oa)
    if obs_ts > now + 60:
        raise ValueError("observed_at too far in future")
    windows_in = raw.get("windows")
    if not isinstance(windows_in, list) or not 1 <= len(windows_in) <= 12:
        raise ValueError("windows must be list of 1..12")
    seen, out = set(), []
    for w in windows_in:
        if not isinstance(w, dict) or w.get("id") not in _IDS or w["id"] in seen:
            raise ValueError("bad or duplicate window id")
        seen.add(w["id"])
        if "used_percent" not in w:
            raise ValueError("missing used_percent")
        up = w["used_percent"]
        if up is not None:
            if isinstance(up, bool) or not isinstance(up, (int, float)):
                raise ValueError("used_percent must be number or None")
            if not math.isfinite(up) or not 0 <= up <= 100:
                raise ValueError("used_percent out of range")
            up = float(up)
        ra = w.get("resets_at")
        if ra is not None and _parse_ts(ra) < obs_ts:
            raise ValueError("resets_at before observed_at")
        out.append({"id": w["id"], "used_percent": up, "resets_at": ra})
    order = {p: i for i, p in enumerate(_IDS)}
    out.sort(key=lambda x: order[x["id"]])
    return {"provider": provider, "observed_at": oa, "observed_ts": obs_ts, "windows": out}
