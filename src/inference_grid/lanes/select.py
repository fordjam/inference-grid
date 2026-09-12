"""Lane selection from readiness, category and scorecard evidence. Authored by OpenCode Go (glm-5.3-flash)
through a Grid attempt; integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

def select_lane(task, lanes, readiness, scorecard, now):
    cat = task.get("category") if isinstance(task, dict) else None
    if not isinstance(cat, str) or not cat:
        raise ValueError("task must have a non-empty string category")
    if not isinstance(lanes, dict):
        raise ValueError("lanes must be a dict")
    for lane_id in lanes:
        if lane_id not in readiness:
            raise ValueError("lane %r has no readiness entry" % (lane_id,))
    author = task.get("author_family")
    ready = {lid for lid in lanes if readiness[lid].get("state") == "ready"}
    cat_match = {lid for lid in ready if cat in (lanes[lid].get("categories") or [])}
    fam_ok = {lid for lid in cat_match
              if not (isinstance(author, str) and lanes[lid].get("family") == author)}
    win_ok = {lid for lid in fam_ok if lanes[lid].get("window_active") is not False}
    if not win_ok:
        if not ready:
            reason = "no_ready_lane"
        elif not cat_match:
            reason = "no_lane_for_category"
        elif not fam_ok:
            reason = "no_independent_family"
        else:
            reason = "window_closed"
        return {"lane": None, "score": None, "reason": reason, "candidates": []}

    def stats(lid):
        lane = lanes[lid]
        for row in scorecard:
            if (row.get("family") == lane.get("family")
                    and row.get("model") == lane.get("model")
                    and row.get("category") == cat):
                return row.get("attempts", 0), row.get("accepted", 0)
        return 0, 0

    def key(lid):
        attempts, accepted = stats(lid)
        return (-(accepted + 1) / (attempts + 2), attempts, lid)

    best = min(win_ok, key=key)
    attempts, accepted = stats(best)
    return {"lane": best, "score": (accepted + 1) / (attempts + 2),
            "reason": "selected", "candidates": sorted(win_ok)}
