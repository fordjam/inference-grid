# Handoff brief 7 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-6.md` (A1 and handoff-4 B1–B3 done 2026-09-13, `c84bb8d`…`4239c4a`;
307 tests). **Section 1 of `docs/handoff-glm.md` applies unchanged.**

---

#### A1. Standalone reviews cannot be retried
`board-new --retry review-board-runner-3-2` was refused: "no source link … the source directory
is missing, so the retry would judge nothing". Correct for a review of a board work task; wrong
for `review-board-runner-*`, which reviews coordinator code and has never had a source task or a
`review/<source>/` directory. The operator authored `review-board-runner-4` by hand.
Distinguish the two: a review is *linked* when a `source.json` names it as `review_task`, or when
the prefix-derived `review/<source>/` directory exists; otherwise it is *standalone*. Retrying a
linked review moves the link (handoff-5 A1); retrying a standalone review copies the task and
brief and moves nothing; refuse only when a link exists and cannot be moved. Record
`standalone: true` in the `--retry` result.
- Tests in `tests/test_board_new.py`: standalone retry succeeds with no `review/` dir at all;
  linked retry still moves; a link that names a different review is not touched.
- Size: small.

#### A2. `board-status --suggest` must skip superseded tasks
It offered a retry of `review-lane-cline`, whose predecessor chain was already closed — because
the operator's hand-written reason did not start with `superseded:`. The convention is now
enforced by `retry_task`, but old reasons exist. Have `--suggest` skip any blocked task whose id
is the predecessor of an existing task (`<id>-<n>` present on the board) regardless of reason
text, and say so in a `skipped` list with the successor id.
- Tests in `tests/test_board_status.py`.
- Size: small.

#### A3. Test-vs-test basename collisions silently drop a declared test
Found by `review-board-runner-4` (kimi-k3 at `reasoning_effort=high`, attempt `d36f530c…`, the
first Go-lane review to complete since the effort fix). A flat task declaring
`tests=["strict/test_mod2.py", "lenient/test_mod2.py"]` passes `validate_task` (entries are
distinct), passes `shadowing_names` (which compares artifact↔test, artifact↔input and
test↔input basenames but never test↔test), and `run_tests` copies both to
`scratch/test_mod2.py` in list order — the lenient test overwrites the strict one, unittest runs
`test_mod2` once, and an artifact that fails a declared coordinator test is accepted. BOARD.md:
"name collisions between artifacts, tests and inputs block before dispatch" and every declared
test runs before acceptance.
- Extend `shadowing_names` to flag duplicate basenames within `tests` (and within `inputs`, and
  within `artifacts`, for the same reason) in both flat and tree branches; block before dispatch
  with the two colliding paths in the reason. Do not change the copy semantics — the block
  makes them unreachable.
- Tests in `tests/test_board_runner.py`: the reviewer's exact case blocks before dispatch; a
  tree task with distinct paths and distinct basenames still dispatches.
- Size: small. **Do this first**; then author nothing — the coordinator will re-review the runner
  as `review-board-runner-5` once it lands.

## B — DeepSeek V4.1 Flash as a third review family (owner request, 2026-09-13)

#### B1. Go lane support for `deepseek-v4-flash`
The only attempt so far (`lane-readiness-review-1`, 2026-09-12) was refused before any
generation: 403 RegionError, "China hosting opt-in disabled" — an OpenCode Go account setting the
operator enables in the console, not a lane defect. Prepare the lane side so a canary can run the
moment that is on:
- `lanes/go.py::REASONING_EFFORT`: add `deepseek-v4-flash` with the values the endpoint's
  documented request schema honours for it (read the schema; if DeepSeek exposes a thinking
  toggle rather than an effort tier, say so in the constant's comment and map `low` → off,
  `high`/`max` → on, or record `unsupported` — do not guess).
- `reasoning_overrun`, `actual_model` checks and the `unsupported_until` exclusion (handoff-2
  B3) must treat the new model like any other; add one test per path with the model id.
- A 403 whose body says region/opt-in must be classified as its own refusal
  (`region_optin_required`), recorded in the lane record with `unsupported_until`, and shown by
  `board-status` — the operator should see "enable China hosting in the Go console", not a bare 403.
- Family for cross-family selection is `deepseek`, giving reviews a third family besides `kimi`
  and `glm`; nothing in `lanes/select.py` (provider-authored) changes.
- Tests in `tests/test_lane_go.py` and `tests/test_board_runner.py`.
- Size: small–medium.

Operator steps (not for this brief): enable China hosting opt-in in the Go console; add a
`go-deepseek` lane to `lanes.json` (provider `go-deepseek`, family `deepseek`, model
`deepseek-v4-flash`, kind `go_http`, same credential and window as `go-kimi`, categories
including `independent_review`) and its account alias; author a `canary` task on it; tick.

## Tier 3 — coordinator
The outcome of `review-board-runner-4` (first Go-lane request with `reasoning_effort=high`) is
recorded in `docs/CONTRIBUTIONS.md`; whatever it exposes is the next A-item.

## C — out of scope
Same as handoff 1.

---

## Definition of done
As handoff 5.
