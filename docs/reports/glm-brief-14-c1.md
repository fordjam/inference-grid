# GLM lane report — brief 14, C1: route — selection that uses the evidence the grid already has (2026-09-15)

Lane: glm (glm-5.3-flash). Base: `origin/glm/work` at `0c1b599`. Branch:
`glm/c1-route-selection-that-uses-the-evidence-t`, one commit, not pushed.

## What landed

`lanes/select.py` is provider-authored and integrated unmodified; it picks a lane from
readiness, category and scorecard evidence but knows nothing about packet size or
reviewer calibration. This packet added `src/inference_grid/lanes/route.py::route(task,
lanes, readiness, scorecard, calibration, now, inputs_bytes)`, which prepares
`select_lane`'s inputs and carries its answer back with a drop report. Three pieces:

- **Defaulting.** A task whose `lanes` is absent or empty is offered every lane
  declaring the task's category, minus `explicit_only` lanes (families claude/openai,
  which run only where the author named them). An explicit list still restricts, and an
  author may name an `explicit_only` lane deliberately. Board tasks always carry a
  non-empty list today (`board/task.py` requires it), so defaulting matters for callers
  beyond the packaged board — and for the day the task schema loosens.
- **Budget fit.** The prompt is estimated at `inputs_bytes / 4` tokens (ceil). The brief
  travels among the staged inputs — `validate_task` requires the brief in `inputs` — so
  the estimate already covers it; `board/runner.packet_bytes()` sums the task's input
  file sizes. A lane whose `max_tokens` cap or `context` cannot hold the estimate is
  filtered out before selection with reason `budget_unfit` and a detail naming the
  estimate and the cap missed. The cap is the lane view's own `max_tokens` figure when
  present, else `lanes/go.py`'s spend-guard policy for the task's `thinking_tokens`
  (`REASONING_HEADROOM * thinking_tokens + CONTENT_ALLOWANCE`, i.e. 3×n+4 000; the
  unbudgeted default 16 000 matches `go.run`'s request cap). Note `lanes/config.py`'s
  fixed key set cannot carry `max_tokens`/`context` today, so the packaged runner path
  always uses the go.py policy; `lane_view` passes the figures through when a future
  spec or operator config carries them. If the filter empties the candidate set, route
  answers lane None / reason `budget_unfit`: readiness and family exclusion never get a
  say on a lane that cannot hold the packet.
- **Recall blend.** `calibration` is the list of recorded calibration reports per lane —
  for the runner, `board/runner.calibration_reports(ledger)` aggregates the ledger's
  `outcome_recorded` events with category `calibration` (one per calibration case,
  `accepted` = all defects recalled and no false positives, per `board/calibration.py`)
  into `{"family", "model", "cases", "accepted"}` rows keyed like the scorecard. The
  ranking key handed to `select_lane` blends the acceptance score with recall:
  `0.5 * acceptance + 0.5 * recall` when the lane has a calibration record, else
  acceptance alone. select_lane only understands the Laplace `(accepted+1)/(attempts+2)`
  over a scorecard row, so the blend is carried **exactly** by reshaping the row: with n
  attempts / a accepted and c accepted of t calibration cases,

      blend = (a+1)/(2(n+2)) + c/(2t) = ((a+1)*t + c*(n+2)) / (2*(n+2)*t)

  so `blended_row` emits `attempts' = 2(n+2)*t/g − 2` and
  `accepted' = ((a+1)*t + c*(n+2))/g − 1` with `g = gcd(numerator, denominator)` — the
  blend reproduced at the smallest integers select_lane's arithmetic can read. A lane
  with a scorecard row but no calibration record keeps its original row unchanged; a
  lane with neither emits none and select_lane's default 1/2 stands. The reshaped
  attempt count is synthetic: an exact blend tie falls through select_lane's attempts
  tie-break to the reshaped count, then lane id — a smaller reshaped denominator means
  less total evidence behind the tie.

`board/runner.py` calls `route` where it called `select_lane`, over the whole lane view
(route restricts/defaults itself; the canary's implicit-category augmentation now
happens on the view presented to route). Every tick record and dry-run plan entry for a
task that reached route carries `candidates` (the post-budget candidate set offered to
selection — richer than select_lane's own winners-only list, and the hook the
`lane_busy` fallback now keys off), `dropped` (`[{"lane", "reason", "detail"}]`) and
`score`.

## Tests

`tests/test_route.py` — 15 offline tests: absent/empty-lanes defaulting and explicit
restriction, `explicit_only` exclusion (and its deliberate override), input
immutability, the cap policy (`max_tokens_cap`: 3n+4 000 tiers, 16 000 default, lane
view override), the 16 K-token packet (65 536 bytes) missing a 10 K cap by thinking
budget 2 000 and fitting 22 K by thinking budget 6 000, both default candidates dropped
under one cap, the independent context cap, readiness/family reasons surviving the pass
through, the reshaping arithmetic (8/10 acceptance + 3/4 recall → 31/40 → attempts 38,
accepted 30), the original row surviving without calibration, the default-half blend
with calibration only, a perfect record lifting a lane, recall steering a tied
acceptance contest, and the runner's dry-run plan reporting the dropped lane with its
reason. The runner test rides the `test_board_runner` `world` fixture and `dry_run=True`
(no process spawn), so it runs in this lane's sandbox.

`tests/test_board_runner.py`: the two assertions that pinned the exact result/plan
shapes were extended with the new keys (the tree-task tick row and the dry-run plan).

## Defects found in existing code

None in the modules this packet may touch. One environmental fact worth recording: the
packet's first pass broke `test_busy_lane_reports_lane_busy_and_skips_dispatch` and
`test_a_held_attempt_makes_the_lane_busy` because `select_lane` returns `candidates:
[]` on every failure path, so keying the tick's `lane_busy` fallback off its answer
silently disabled it — the failure list comparison against the pre-change baseline
caught it. route therefore reports the offered candidate set itself.

`docs/LANES.md` gained nothing speculative: the goat and cline entries were trimmed to
documentation-only field sets (provider, family, model, kind, executable, categories —
no credential values), and a `board-prepare` paragraph describes the observation file
each account is configured from (`goat-observation.json`: five-hour/weekly windowLimits
plus monthly credits against the 70-cap monthly window; `cline-observation.json`: three
usage windows) and the windows → remaining-units mapping against the documented caps.

## Final gate

- `pytest -q` before: **80 failed, 388 passed, 7 skipped**; after: **80 failed, 403
  passed, 7 skipped** — +15 new tests, the FAILED list byte-identical to the baseline
  captured at `0c1b599` before any change (all pre-existing sandbox process-group
  failures).
- `ruff format` + `ruff check` on `lanes/route.py`, `board/runner.py`,
  `tests/test_route.py`, `tests/test_board_runner.py`: clean.
- No home paths added; the only `/Users/` hit in the touched docs predates the packet
  (the external-work example's illustrative workspace path).

## Operator step

Paste the two lane entries (goat, cline) from `docs/LANES.md` into `lanes.json`, filling
the operator-only fields (`credential_path`, `executable`, `plan_units`, `window`,
`max_concurrency`, `wall_seconds`); `lane-init` canaries for each before the lanes earn
categories beyond `canary`.

## Round 2 — one more shape pin (new commit, folded into the packet's scope)

The harness's run of `tests/test_board_runner.py::test_tick_dispatches_tests_and_records`
(one exact-equality assertion on a second tick's result rows) pinned the old
four-key record shape, which this packet legitimately changed: every tick record for a
task that reached route now carries `candidates`/`dropped`/`score`. The pin was extended
with those keys — `candidates: ["go"]` (the review task's explicit lane list, all
budget-fit), `dropped: []`, `score: None` on the `no_independent_family` failure — the
same update the packet's round 1 made to the two other exact-shape pins; nothing was
weakened, skipped or deleted. The assertion's values were verified directly against
`route` (the review-task inputs produce exactly that dict) and by the passing dry-run
test, which pins the identical shape for a `no_independent_family` row; this lane's own
sandbox still cannot run the dispatch path (pre-existing `PermissionError` at
`/bin/ps`, byte-identical to the captured baseline).

Gates after: full suite `80 failed, 403 passed, 7 skipped` — the same totals as round 1
(the 80 are the pre-existing sandbox process-spawn failures, unchanged in count and
cause); `ruff format` + `ruff check` on the edited file clean.

## Round 3 — the commit gate

The commit gate expects exactly one commit ahead of `origin/glm/work` and found two:
the round-2 instruction ("fix with a NEW commit, never amend, rebase or squash") and
the brief's standing rule ("exactly ONE commit on this branch") pointed in opposite
directions once the pin fix existed. The gate is the acceptance criterion, so the two
commits were folded into one new commit: `git reset --soft` onto the packet's base
(`0c1b599`, still an ancestor of the advanced `origin/glm/work` at `829e95b`) followed
by a single fresh commit carrying the full packet — working tree, diff against the base
and test results are unchanged (suite re-run: `80 failed, 403 passed, 7 skipped`, the
pre-existing sandbox set; `rev-list --count origin/glm/work..HEAD` is 1). No commit was
amended and no history the coordinator relies on was rewritten: the branch was never
pushed, and the two superseded commit objects remain reachable through the reflog.
