# GLM lane report — brief 14, I2: review budgets sized from the packet (2026-09-15)

Lane: GLM-5.3-Flash via Command Code. Base: `glm/work` at `9056f17`.
Branch: `packet/packet-i2`, one commit, not pushed.

## Why

The review line authored every packet with `thinking_tokens: 6000`
(`runner.REVIEW_BUDGET`), and the go lane derives its request cap from that budget
(`3 × thinking + 4000` = 22 000) whatever the packet weighs. The 46 KB vix-rs packet
needed three attempts on that fixed cap. The brief: make the budget follow the staged
bytes, and make the dry run explain a refusal in tokens.

## What landed

`src/inference_grid/board/branch_review.py`:

- `sized_thinking_tokens(staged_bytes)` = `min(24000, max(6000, staged_bytes // 4))` —
  the same 4 bytes-per-token estimate `route` uses, so the ask tracks the packet: a
  16 KB packet asks for 6 000 (cap 22 000 as before), a 60 KB packet asks for 15 000
  (cap 49 000), and anything over 96 KB clamps at the working reviewer's proven 24 000.
  The floor and ceiling are the module constants `MIN_THINKING_TOKENS` /
  `MAX_THINKING_TOKENS`.
- `_plan_task` takes the packet's staged bytes (staged files under the per-file cap
  plus the patch — the same `total` the byte budget is checked against) and, when no
  explicit `budget` argument is given, sizes the default budget's `thinking_tokens`
  from it; all three authoring paths (whole range, per-commit split, branch scope) pass
  their own total, so a split sizes each packet on its own bytes. An explicit budget
  still wins unchanged.
- The authoring summary records `staged_bytes` and the chosen `budget` beside the task
  path. The task record itself cannot carry them: `board/task.py` (provider-authored,
  integrated unmodified) refuses any key outside its fixed eleven, so the summary is
  the record — the chosen budget is also literally on the task file in
  `budget.thinking_tokens`, which is what the runner and the lane read.

`src/inference_grid/lanes/route.py`:

- Every `budget_unfit` row of `dropped` now carries the two numbers: `prompt` (the
  `ceil(bytes / 4)` estimate) and `cap` — the max_tokens cap it missed, or the context
  window when that is what refused (the `detail` string still names which). The
  `candidates` rows became `[{"lane", "cap"}]`, each carrying the
  `max_tokens_cap(lane, thinking)` the offered lane would run under. The empty-candidate
  early return and the drop-report shape are otherwise unchanged.

`src/inference_grid/board/runner.py`:

- The tick's `lane_busy` fallback reads the candidate rows' `lane` field; the plan and
  result rows pass `candidates`/`dropped` through unchanged, so the dry run now carries
  the cap for every candidate and both token numbers for every refusal.

No provider-authored file was touched (`board/task.py`, `lanes/select.py`,
`lanes/config.py`, `lanes/sandbox.py`, `observation.py`, `goat_outcomes.py`,
`remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`).
`branch_review.py` is not on the do-not-edit list and changed in place.

## Tests

All offline; no test opens a network connection.

`tests/test_branch_review.py`:

- `sized_thinking_tokens` unit cases: 16 000 bytes → 6 000 (floor), 60 000 → 15 000,
  120 000 → 24 000 (ceiling).
- Three packet sizes → three budgets: whole-range packets of ~0.5 KB, ~36 KB and
  ~100 KB ask for 6 000, a middle value and 24 000, each equal to the formula over the
  recorded `staged_bytes`; the summary's `budget` equals the task file's.
- A split sizes each packet on its own bytes: two ~30 KB commit packets budget below
  what the whole range's bytes would ask for.
- The explicit-budget override test now also pins that the summary records the chosen
  budget and `staged_bytes`.
- The pre-existing default-budget assertion (`task["budget"] == runner.REVIEW_BUDGET`)
  still passes: a small packet's sized budget equals the old constant.

`tests/test_route.py`:

- The 16 K-token packet vs a 10 K cap asserts the full dropped row with
  `prompt: 16384, cap: 10000`; a lane whose own `max_tokens` figure is below the packet
  need is `budget_unfit` with both numbers; a context refusal's `cap` is the context.
- Candidate rows carry the lane's cap, including a lane-view `max_tokens` override.
- Through `runner.tick` with dry runs: the dropped plan row carries `prompt`/`cap`, and
  a selected plan row carries the candidate's cap (a new test).

Shape updates forced by the candidate rows (assertions changed, behaviour they pinned
kept): `test_board_runner.py`'s three exact result/plan rows and
`test_packet_task.py`'s packet plan row now expect `[{"lane", "cap": 16000}]` (the
fixture lanes are unbudgeted, so the go policy's 16 000).

## Defects found in existing code

None. One judgment call worth recording: the packet's "a 16 K-token packet asks for
6 000" example only works if "16 K-token" means a ~16 KB packet (16 000 bytes // 4 =
4 000 → the 6 000 floor); a literal 16 K-token packet (64 KB) would ask for 16 000. The
60 KB → 15 000 example confirms the byte reading, and the implementation follows it.

## Final gate

- Full suite before the change (stashed worktree at `9056f17`, same flags): **88
  failed, 503 passed, 7 skipped, 1 collection error**; after: **88 failed, 509 passed,
  7 skipped, 1 collection error** (+6 new tests, all passing). The sorted
  `FAILED`/`ERROR` set is byte-identical to the base; the collection error is
  `tests/test_calibration.py` hitting the lane's deny-read on the private calibration
  corpus — environmental, present at the base (the run needs
  `--continue-on-collection-errors` for that reason). The 88 inherited failures are the
  sandbox `killpg` refusals the earlier lane reports name.
- `ruff format --check` and `ruff check` on the seven changed files: clean. Note: a
  bare `ruff format` over the whole tree reformats 39 files including provider-authored
  modules (`flash_window.py`, `goat_outcomes.py`, `provider_report.py`,
  `lane_readiness.py`, `lanes/config.py`, `lanes/select.py`) — that churn was reverted;
  the tree is not format-clean at this ruff version and the packet's rule is honored on
  the changed files, the same reading the earlier lane reports use.

## Round 2 — the pytest gate caught a missed caller

The gate's 14 `tests/test_calibration.py` failures were real and mine:
`board/calibration.py::author_calibration` imports `_plan_task` from `branch_review`
and called it with the old ten-argument positional signature, so every calibration
authoring call raised TypeError the moment `_plan_task` grew the required
`staged_bytes` parameter. My own full-suite runs never saw it: the lane sandbox's
deny-read on the private calibration corpus makes the module's collection itself fail
(the skipif marker stats the denied path), so none of its tests ran locally, and the
sorted failure set stayed byte-identical for the wrong reason. The gate runs outside
that deny-read and collects the module, which is exactly what the pytest gate is for.

Fix: `author_calibration` now computes the case's real staged bytes —
`sum(len(data) for _, data in case["files"]) + len(case["diff"].encode())`, the same
staged-files-plus-patch measure the branch-review paths pass — and passes it as
`staged_bytes`, so calibration packets get packet-sized budgets too instead of silently
floored ones. No test was weakened, skipped or deleted.

Verification, given the local deny-read: the suite's test module was copied (minus the
one private-corpus test, which the sandbox cannot collect) into the gitignored
`.commandcode/` and run against the fix — 20 passed, 1 skipped. The copy was deleted
afterwards; nothing under `.commandcode/` is committed.

`ruff format` also applied to `calibration.py` itself: the file was not format-clean at
this ruff version before the packet (three pre-existing spots, including two whole-list
expansions untouched by this packet's logic). Since the file is now in the change set
and is not on the provider-authored do-not-edit list, it was formatted whole;
`ruff format --check` and `ruff check` are clean on it.

Full suite after the fix: 88 failed, 509 passed, 7 skipped, 1 collection error — the
same failure set as the base and as round 1 in this sandbox, with the calibration
module still unable to collect here; the gate's own environment is the authoritative
runner for it, and the fix targets exactly the calls the gate exercises.

## Round 3 — commit gate: the two instructions cannot both hold

Round 2's instruction ("fix with a NEW commit, never amend, rebase or squash") and the
commit gate ("exactly one commit ahead of the base") are irreconcilable: a third commit
on top of two still finds two, and no new commit can reduce the count. The packet's own
standing contract also says exactly one commit on this branch, and the gates are the
judge — the same collision packet-i1 met in its rounds 2–3, with the same resolution:
the rounds' work is re-committed as the single commit the gate requires. Nothing about
the tree changed in this reconciliation except this report section; the round-1 packet,
the round-2 calibration fix and both disclosures land as one commit whose message
covers all of it. The commit gate was then run directly
(`python -m inference_grid.lanes.gates commit <base> <trailer>`): `commit gate ok`.
