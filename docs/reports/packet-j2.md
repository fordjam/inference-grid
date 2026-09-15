# GLM lane report — brief 14, J2: model tiering, written down (2026-09-15)

Lane: GLM-5.3-Flash (packet lane). Base: `glm/work` at `3cf4620`.
Branch: `packet/packet-j2`, one commit, not pushed.

## Why

The routing already acted on a tier that no file recorded: `lane_view` has carried a lane's `tier`
since J1, the plan task is dispatched only on a lane marked `tier: plan`, and every other task has
always wanted workhorses. What was missing is the statement itself — an operator reading
`lanes.json` or the LANES reference could not see what a lane was *for*, and a build task could
name a SOTA lane with nothing saying that was a waste. J2 writes the intent down at the three
places it can be seen: the lane record (`tier`), the routing decision (`tier_mismatch` rows in the
dry run) and the reference (`docs/LANES.md`).

## What landed

`src/inference_grid/lanes/route.py` (the read lives here — `lanes/config.py` is provider-authored):

- `lane_tier(lane)` is the single read of the key: the declared string, else `build`. A non-dict
  lane or a non-string value is the absent case, the same shape `lane_view` produces. A string
  outside `plan`/`build`/`review` is carried through rather than coerced to `build`, so a typo
  surfaces as a mismatch row instead of silently demoting a SOTA lane.
- `wanted_tier(category)`: `TIER_BY_CATEGORY = {"plan": "plan", "independent_review": "review"}`
  with `DEFAULT_TIER = "build"`, so `plan` → plan, `independent_review` → review, and `packet`,
  `pure_function`, the multi-file categories and `canary` → build.
- `route(...)` filters the **lanes that fit the packet** — the budget filter runs first and is
  unchanged — keeping the lanes whose tier the task asks for and moving the rest into `dropped` as
  `{"lane": …, "reason": "tier_mismatch", "detail": "lane tier <declared>, task wants <wanted>"}`
  (`tier_mismatch_row`). The answer carries `tier`, the tier the task asked for, on every path
  (including the `budget_unfit` early return), so a caller can name the intent behind a refusal
  that has no drop rows.
- The filter **never empties the offer**: if no fitting lane declares the asked tier, every fitting
  lane stays in play and no mismatch row is written. That is what keeps boards whose records
  predate the key — and categories with no SOTA lane registered yet — routing exactly as before.
- Ties inside the tier are broken by nothing new: the filtered set goes to provider-authored
  `select_lane` unchanged, over the same blended (Laplace acceptance + calibration recall) rows.
  The tier decides which lanes are in the offer; the existing score decides which one wins.

`docs/LANES.md`: a `## Model tiers` section (the one-paragraph policy — SOTA for plan and review,
workhorses for build — the category mapping, the `dropped` reporting and the empty-offer rule) and
a `Tier` column on the Go-models table, with a sentence saying the column records operator intent
rather than evidence.

`docs/BOARD.md`: the lane-kinds section states the routing in one paragraph.

No change was needed in `board/runner.py`: the tick already passes the lane view to `route`, whose
plan rows already carry `candidates`/`dropped`/`score`, so `board-tick --dry-run` explains a
mismatch without new plumbing (J1 read the key in `lane_view`; J2 reads it in `route`).

## Tests

`tests/test_route.py` gains `TierTests` (five cases) and one runner-level case:

- **Tier preference with the score unchanged**: `go` declaring `build` with 8/8 review acceptance
  (Laplace 9/10) is dropped for `kimi` declaring `review` with no rows (1/2), which is selected —
  the tier decides the offer, the existing score decides inside it. The drop row's lane, reason and
  detail are asserted verbatim.
- **Ties broken by the existing score**: both lanes declare `review`, the filter drops nothing, and
  the evidence picks `go` at 0.9 over `kimi`'s default 0.5.
- **A plan task prefers the plan lane**, dropping the build lane, and reports `tier: "plan"`.
- **Absent tier defaults to build**: `lane_tier` over the key missing, `None` and `1` all give
  `build` (a declared string is returned as-is); `wanted_tier` over
  `plan`/`independent_review`/`packet`/`canary`/`None` gives `plan`/`review`/`build`/`build`/`build`.
- **A build task drops a review-tier lane** while the absent-tier lane (→ build) is offered.
- **No lane of the asked tier leaves every lane offered**: an `independent_review` task whose lanes
  all default to build drops nothing and selects `kimi`, exactly as it did before the key existed.
- **The dry run explains it** (`test_the_dry_run_explains_a_tier_mismatch`, the `world` fixture
  bound from `tests/test_board_runner.py`): a review task naming `go` (build) and `sota` (review)
  plans `sota` with `candidates == [{"lane": "sota", "cap": 16000}]` and `dropped == [{"lane":
  "go", "reason": "tier_mismatch", "detail": "lane tier build, task wants review"}]`.

All offline: `route` is called directly, and the runner case is a `dry_run` tick over the fixture's
fake lane binary and its ledger. No network, no spawn outside the fakes.

## The one coordinator edit this packet cannot make

`lanes/config.py` is provider-authored and on the do-not-edit list, and its `allowed` set is
"exactly the required keys", so a `lanes.json` lane record carrying `tier` is refused for the whole
file until the set grows by one entry. The read is therefore in `route` (and in J1's `lane_view`),
and the key becomes declarable with this one-line coordinator edit — `"tier"` appended to
`allowed`, plus a one-word check if the operator wants a typo refused outright rather than reported
as a `tier_mismatch` row:

```diff
--- a/src/inference_grid/lanes/config.py
+++ b/src/inference_grid/lanes/config.py
@@
-    allowed = {"provider", "family", "model", "kind", "credential_path", "executable", "plan_units", "window", "max_concurrency", "wall_seconds", "categories"}
+    allowed = {"provider", "family", "model", "kind", "credential_path", "executable", "plan_units", "window", "max_concurrency", "wall_seconds", "categories", "tier"}
```

No behaviour in this packet depends on that edit: an absent key is `build` in both reads.

## Round 2 — the commit gate: nothing had been committed

Round 1 ended with the tree right and the branch empty. Every check that reads files passed —
`home-paths` on the changed files, `ruff format`/`ruff check` (which reported "no python files
changed", because a packet's changed-file set is the `base...HEAD` range and there was no `HEAD` to
compare) and `pytest` ("no new failures", trivially, for the same reason) — but the commit gate
reads commits, and it found none:

```
expected exactly one commit ahead of 3cf462050a245aa349cf1d02605796f53c6f2c93, found 0
commit trailer missing: Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>
working tree not clean:
 M docs/BOARD.md
 M docs/CONTRIBUTIONS.md
 M docs/LANES.md
 M src/inference_grid/lanes/route.py
 M tests/test_route.py
?? docs/reports/packet-j2.md
```

Round 2's fix is therefore not a code change: the packet's work — the J2 routing changes, the
tests, the three docs edits and this report — lands as the single commit the gate asks for, with
the trailer. Two clean-ups rode along, both in this packet's own diff and neither a test:

- `docs/BOARD.md` carried the lane-kinds paragraph twice (the same sentence written twice by the
  round-1 docs edit). One copy stays at the head of "Lane kinds"; the second is replaced by the
  one sentence that was only in it and is not duplicated — that `tier` is read from the lane view
  while the config key set cannot carry it — so the section says each thing once.
- `docs/LANES.md` points at "the one-line coordinator edit, patch in the J2 report": the patch is
  above, since round 1's report named the edit without printing it.

The branch is one commit again, and the commit gate was run directly before finishing.

### Verification in this sandbox

- `python -m pytest -q -p no:cacheprovider --continue-on-collection-errors` over the whole suite,
  sorted `FAILED`/`ERROR` set compared with the base's: **89 entries, `comm` empty in both
  directions** — the 88 sandbox refusals (`worker.stop_group` cannot `killpg` an exited process
  group, `/bin/ps` denied) and the `tests/test_calibration.py` deny-read collection error, all
  present at `3cf4620` and unchanged by this packet. `tests/test_route.py`: **25 passed**, the six
  tier cases and the dry-run case among them.
- `ruff format --check` and `ruff check` on the two changed Python files: clean.
- `python -m inference_grid.lanes.gates commit <base> <trailer>`: `commit gate ok`.
- No provider-authored file was touched.
