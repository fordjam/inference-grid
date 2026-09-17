"""Policy in one place: the autonomy line.

(brief 14 M3) is the sentence `docs/AUTONOMY.md` writes out: what a board tick does
alone and what always waits for the operator. The two lists live here as constants and
the document's table is a test-checked mirror of them — change both together or the
test says which side moved. The one wait the runner enforces mechanically is
`owner_only`: a board config may name path prefixes whose files no packet may touch
without the operator, and `owner_only_prefix` is the matcher the runner refuses
dispatches with and the digest lists from.

B7 deleted this module's other half: a `review_needed`/`record_waiver` path let a
passing work task settle `passed` with no review task at all when its receipt proved
the source lane's own gates ran green. Review is a gate, not a waiver — every passing
work task now always gets a review task (board/runner.py); a task whose reviewer lane
is unavailable waits in `review_pending` with route()'s own reason, it does not settle
unreviewed. No code path emits `review_waived` anymore.
"""

# --- The autonomy line (brief 14 M3): what a board tick does alone, and what always
# --- waits for the operator. `docs/AUTONOMY.md`'s table is a test-checked mirror of
# --- these two tuples; change both together.

# Actions the grid takes with no operator in the loop.
ALONE = (
    "dispatch ready tasks within each account's admission limit",
    "requeue a dead attempt (L2)",
    "draft a fix packet for a blocked build (M1)",
    "land a packet whose gates and independent review passed, on a board with auto_land",
    "publish observations",
)

# Decisions the grid never makes by itself.
WAITS = (
    "anything that changes published research numbers",
    "the holdout register",
    "credentials",
    "provider configuration",
    "deploys",
    "spend beyond the configured limit",
    "a packet whose files fall under a board's owner_only prefixes",
)


def owner_only_prefix(task, prefixes):
    """The board's first owner-only prefix one of the packet's declared files falls under.

    `prefixes` is the board config's `owner_only` key (a list of path prefixes, `[]` by
    default); entries that are not non-empty strings are ignored, so a half-written key
    narrows the gate instead of crashing the tick. A prefix matches directory-style —
    the path itself or anything beneath it (`docs` covers `docs/x.md`, never
    `docs-x.md`). The declared paths are what a packet can touch: staging copies
    exactly the task's inputs, tests and artifacts into the attempt, and the brief
    rides in the inputs.
    """
    if not isinstance(prefixes, list):
        return None
    paths = [*task.get("inputs", ()), *task.get("tests", ()), *task.get("artifacts", ())]
    for prefix in prefixes:
        if not isinstance(prefix, str) or not prefix:
            continue
        root = prefix.rstrip("/")
        for path in paths:
            if path == root or path.startswith(root + "/"):
                return prefix
    return None

