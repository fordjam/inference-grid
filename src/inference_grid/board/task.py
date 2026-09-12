"""Board task validation. Authored by OpenCode Go (glm-5.3-flash) through a Grid attempt; integrated
unmodified after local tests. See docs/CONTRIBUTIONS.md and docs/BOARD.md."""

def validate_task(raw):
    def err(k, m):
        raise ValueError(k + ": " + m)
    if not isinstance(raw, dict):
        err("task", "expected a dict")
    want = ["id", "category", "brief", "inputs", "tests", "artifacts", "lanes", "author_family", "budget", "state", "blocked_reason"]
    if set(raw) != set(want):
        err("task", "extra or missing keys")
    def lset(s):
        return isinstance(s, str) and s and all(c == "-" or "a" <= c <= "z" or "0" <= c <= "9" for c in s)
    def safe(p):
        if not isinstance(p, str) or not p or p.startswith("/") or "\\" in p or "\x00" in p:
            return False
        return all(seg and seg != "." and seg != ".." for seg in p.split("/"))
    def uniq(lst):
        return len(lst) == len(set(lst))
    def isint(v):
        return isinstance(v, int) and not isinstance(v, bool)
    v = raw["id"]
    if not lset(v) or len(v) > 60:
        err("id", "must be non-empty str, max 60 chars, [a-z0-9-]")
    if raw["category"] not in ("pure_function", "tests_multi_file", "fixtures_multi_file", "independent_review", "canary"):
        err("category", "unknown category")
    if not isinstance(raw["brief"], str) or not raw["brief"] or not raw["brief"].endswith(".txt"):
        err("brief", "must be non-empty str ending in .txt")
    for key in ("inputs", "tests", "artifacts"):
        if not isinstance(raw[key], list):
            err(key, "must be a list")
        if not all(safe(p) for p in raw[key]):
            err(key, "contains an unsafe path")
    if not raw["inputs"] or not uniq(raw["inputs"]):
        err("inputs", "must be non-empty with distinct entries")
    if not uniq(raw["tests"]):
        err("tests", "must contain distinct entries")
    if not raw["artifacts"] or not uniq(raw["artifacts"]):
        err("artifacts", "must be non-empty with distinct entries")
    lanes = raw["lanes"]
    if not isinstance(lanes, list) or not lanes or not uniq(lanes) or not all(lset(s) for s in lanes):
        err("lanes", "must be non-empty list of distinct lane ids")
    af = raw["author_family"]
    if af is not None and (not isinstance(af, str) or not af or len(af) > 40):
        err("author_family", "must be None or non-empty str of at most 40 chars")
    b = raw["budget"]
    if not isinstance(b, dict) or set(b) != {"wall_seconds", "output_bytes", "thinking_tokens"}:
        err("budget", "must be dict with exactly wall_seconds, output_bytes, thinking_tokens")
    if not isint(b["wall_seconds"]) or not 30 <= b["wall_seconds"] <= 3600:
        err("budget", "wall_seconds must be int 30..3600")
    if not isint(b["output_bytes"]) or not 1 <= b["output_bytes"] <= 10000000:
        err("budget", "output_bytes must be int 1..10000000")
    tt = b["thinking_tokens"]
    if tt is not None and (not isint(tt) or not 0 <= tt <= 100000):
        err("budget", "thinking_tokens must be None or int 0..100000")
    if raw["state"] not in ("ready", "dispatched", "passed", "review_pending", "accepted", "blocked"):
        err("state", "unknown state")
    br = raw["blocked_reason"]
    if raw["state"] == "blocked":
        if not isinstance(br, str) or not br or len(br) > 300:
            err("blocked_reason", "must be non-empty str of at most 300 chars when blocked")
    elif br is not None:
        err("blocked_reason", "must be None unless state is blocked")
    if raw["brief"] not in raw["inputs"]:
        err("inputs", "brief must appear in inputs")
    def cp(x):
        if isinstance(x, list):
            return [cp(i) for i in x]
        if isinstance(x, dict):
            return {k: cp(v2) for k, v2 in x.items()}
        return x
    return {k: cp(raw[k]) for k in want}
