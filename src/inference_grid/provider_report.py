"""Per-provider refresh report for the capacity refresh agent. Authored by OpenCode Go (glm-5.3-flash)
through a Grid attempt; integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

import math
import datetime


def provider_report(provider, publish, cooldown_until, now):
    if not isinstance(provider, str) or not (1 <= len(provider) <= 40):
        raise ValueError("provider")
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now")
    if cooldown_until is not None:
        if isinstance(cooldown_until, bool) or not isinstance(cooldown_until, (int, float)) or not math.isfinite(cooldown_until):
            raise ValueError("cooldown_until")
    if publish is not None:
        if not isinstance(publish, dict) or publish.get("status") not in ("published", "stale", "refused", "auth_required"):
            raise ValueError("publish")
    if cooldown_until is not None and cooldown_until > now:
        t = datetime.datetime.fromtimestamp(cooldown_until, datetime.timezone.utc).isoformat()
        return {"provider": provider, "status": "cooldown", "next_eligible_at": t}
    if publish is None:
        return {"provider": provider, "status": "unknown", "next_eligible_at": None}
    st = publish["status"]
    s = {"auth_required": "auth_required", "published": "ok", "stale": "unknown"}.get(st, "error")
    return {"provider": provider, "status": s, "next_eligible_at": None}
