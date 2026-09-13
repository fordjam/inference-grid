def scorecard_summary(entries):
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")
    int_keys = ("attempts", "completed", "accepted", "held", "resolved", "repairs", "usage_reported")
    required = ("family", "model", "category", "usage") + int_keys
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("entry must be a dict")
        for k in required:
            if k not in e:
                raise ValueError("missing key: " + k)
        for k in int_keys:
            v = e[k]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError("bad int field: " + k)
        if not isinstance(e["usage"], dict):
            raise ValueError("usage must be a dict")
        for v in e["usage"].values():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError("bad usage value")
    models = {}
    cats = {}
    for e in entries:
        mk = (e["family"], e["model"])
        m = models.setdefault(mk, [0, 0, 0, 0])
        m[0] += e["attempts"]
        m[1] += e["accepted"]
        if "output" in e["usage"]:
            m[2] += e["usage"]["output"]
            m[3] += 1
        c = cats.setdefault(e["category"], [0, 0, {}])
        c[0] += e["attempts"]
        c[1] += e["accepted"]
        cm = c[2].setdefault(mk, [0, 0])
        cm[0] += e["attempts"]
        cm[1] += e["accepted"]
    by_model = []
    for (fam, mod), (att, acc, osum, ocnt) in models.items():
        by_model.append({"family": fam, "model": mod, "attempts": att, "accepted": acc,
                         "acceptance_rate": (acc / att) if att else None,
                         "avg_output_tokens": (osum / ocnt) if ocnt else None})
    by_model.sort(key=lambda r: (r["acceptance_rate"] is None, -(r["acceptance_rate"] or 0),
                                 -r["attempts"], r["family"], r["model"]))
    by_cat = []
    for name in sorted(cats):
        att, acc, cms = cats[name]
        cand = [(fam + "/" + mod, a, acc2 / a) for (fam, mod), (a, acc2) in cms.items() if a > 0]
        best = min(cand, key=lambda t: (-t[2], -t[1], t[0]))[0] if cand else None
        by_cat.append({"category": name, "attempts": att, "accepted": acc,
                       "acceptance_rate": (acc / att) if att else None, "best_model": best})
    return {"by_model": by_model, "by_category": by_cat}
