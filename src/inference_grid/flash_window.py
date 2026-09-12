"""Z.ai GLM-5.3-Flash campaign window (2026-09-03..20, 23:00-09:00 SGT). Authored by OpenCode Go
(glm-5.3-flash) through a Grid attempt; integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

import math
import datetime


def flash_window(now):
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now must be a finite non-bool number")
    tz = datetime.timezone(datetime.timedelta(hours=8))
    start = datetime.datetime(2026, 9, 3, 23, 0, 0, tzinfo=tz)
    over = datetime.datetime(2026, 9, 21, 9, 0, 0, tzinfo=tz)
    if now >= over.timestamp():
        return {"active": False, "multiplier": 1, "next_change_at": None}
    if now >= start.timestamp():
        sgt = datetime.datetime.fromtimestamp(now, tz)
        d = sgt.date()
        if sgt.hour >= 23:
            end = datetime.datetime.combine(d + datetime.timedelta(days=1), datetime.time(9, 0), tzinfo=tz)
            return {"active": True, "multiplier": 2, "next_change_at": end.timestamp()}
        if sgt.hour < 9:
            end = datetime.datetime.combine(d, datetime.time(9, 0), tzinfo=tz)
            return {"active": True, "multiplier": 2, "next_change_at": end.timestamp()}
        nxt = datetime.datetime.combine(d, datetime.time(23, 0), tzinfo=tz)
        return {"active": False, "multiplier": 1, "next_change_at": nxt.timestamp()}
    return {"active": False, "multiplier": 1, "next_change_at": start.timestamp()}
