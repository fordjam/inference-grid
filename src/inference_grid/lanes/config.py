"""Lane configuration validation. Authored by OpenCode Go (glm-5.3-flash) through a Grid attempt;
integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

import math


def validate_lane_config(raw):
    if not isinstance(raw, dict) or set(raw.keys()) != {"lanes"}:
        raise ValueError("lanes: expected dict with exactly one key 'lanes'")
    lanes = raw["lanes"]
    if not isinstance(lanes, dict) or not lanes:
        raise ValueError("lanes: must be a non-empty dict")
    kinds = {"go_http", "goat_cli", "cline_cli", "claude_headless", "zcode_cli"}
    allowed = {"provider", "family", "model", "kind", "credential_path", "executable", "plan_units", "window", "max_concurrency", "wall_seconds", "categories"}
    out = {}
    for lid, spec in lanes.items():
        try:
            if not isinstance(lid, str) or not lid or len(lid) > 40 or any(not (c.isalpha() and c.islower()) and c not in "0123456789-" for c in lid):
                raise ValueError("lane id must be non-empty str <= 40 chars of lowercase letters, digits, hyphens")
            if not isinstance(spec, dict) or set(spec.keys()) != allowed:
                raise ValueError("lane must be a dict with exactly the required keys")
            for k, m in (("provider", 40), ("family", 40), ("model", 80)):
                v = spec[k]
                if not isinstance(v, str) or not v or len(v) > m:
                    raise ValueError(k + " must be a non-empty str of at most " + str(m) + " chars")
            if spec["kind"] not in kinds:
                raise ValueError("kind must be one of the allowed kinds")
            for k in ("credential_path", "executable"):
                v = spec[k]
                if v is not None and (not isinstance(v, str) or not v.startswith("/")):
                    raise ValueError(k + " must be None or a str starting with '/'")
            pu = spec["plan_units"]
            if not isinstance(pu, dict):
                raise ValueError("plan_units must be a dict")
            for w, n in pu.items():
                if not isinstance(w, str) or not w:
                    raise ValueError("plan_units keys must be non-empty strs")
                if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n <= 0:
                    raise ValueError("plan_units values must be finite non-bool numbers > 0")
            if spec["window"] is not None and spec["window"] != "zai_flash":
                raise ValueError("window must be None or 'zai_flash'")
            mc, ws = spec["max_concurrency"], spec["wall_seconds"]
            if isinstance(mc, bool) or not isinstance(mc, int) or not 1 <= mc <= 8:
                raise ValueError("max_concurrency must be an int in 1..8")
            if isinstance(ws, bool) or not isinstance(ws, int) or not 30 <= ws <= 3600:
                raise ValueError("wall_seconds must be an int in 30..3600")
            cats = spec["categories"]
            if not isinstance(cats, list) or not cats or any(not isinstance(c, str) or not c for c in cats) or len(set(cats)) != len(cats):
                raise ValueError("categories must be a non-empty list of distinct non-empty strs")
        except ValueError as e:
            raise ValueError(str(lid) + ": " + str(e)) from None
        except TypeError as e:
            raise ValueError(str(lid) + ": " + str(e)) from None
        out[lid] = dict(spec)
        out[lid]["plan_units"] = dict(pu)
        out[lid]["categories"] = list(cats)
    return {"lanes": out}
