# Handoff brief 2 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm.md` (A1–A5, B1–B5 done 2026-09-13). Same scope: offline engineering
work on this repository, verifiable with `~/.local/share/inference-grid/venv/bin/python -m pytest -q`,
touching no credentials or private data. **Section 1 of `docs/handoff-glm.md` applies unchanged:
never read or edit `~/.config/inference-grid/`, `~/.grid-workspaces/`, `board.sqlite` or any
credential file; never run a tick or dispatch; never push; tests stay offline; provider-authored
modules marked "integrated unmodified" are fixed at their callers.**

Every item below was observed on the live board on 2026-09-13 during the first operator-driven
review round after handoff 1 (four `go-kimi` reviews dispatched by hand; evidence rows are in
`docs/CONTRIBUTIONS.md` under that date). Each has a location, an acceptance test and a size.

---

## A — defects observed on the live board

#### A1. A held attempt occupies the account but does not count as busy
`src/inference_grid/board/runner.py::readiness_view` counts attempts in `queued`/`dispatching`
toward `max_concurrency` (handoff 1, B1). An attempt in state `held` still holds the account slot
in the ledger, so after `review-board-runner-3` was held at its transport timeout the same tick
dispatched three more tasks to `go-kimi` and each came back `refused: account busy` — exactly the
collision B1 was meant to remove.
- Count every state the ledger treats as ACTIVE (queued, dispatching, held) in `readiness_view`.
  Look at `ledger.py` for the ACTIVE set rather than hard-coding the list twice.
- Test: one lane of concurrency 1, one held attempt on its account, two ready tasks → both report
  `lane_busy`, no dispatch.
- Size: small.

#### A2. A re-dispatched review cannot find its source task
`runner.py::accept_reviewed` derives the source id by stripping `review-` from the review task id.
`review-lane-cline-2` (the authorised retry of `review-lane-cline`, per BOARD.md "retries only as
new tasks with a recorded change") therefore looked for `lane-cline-2`, returned
`source link missing, nothing accepted`, and the operator accepted `lane-cline` by hand.
`propagate_rejection` has the same derivation.
- Resolve the source explicitly instead of by name: have `create_review_task`/`write_source_link`
  record `review_task` in `grid/board/review/<source>/source.json`, and have `accept_reviewed` and
  `propagate_rejection` find the source by scanning `review/*/source.json` for a `review_task`
  equal to the review id, falling back to the `review-` prefix rule. Adding a field to the task
  schema is not an option (`board/task.py` is provider-authored).
- Tests: a review task named `review-x-2` whose `source.json` under `review/x/` names it accepts
  `x`; the prefix fallback still works; a rejection of `review-x-2` blocks `x`.
- Size: medium.

#### A3. The Go lane transport timeout ignores the task budget
`src/inference_grid/lanes/go.py::run` derives `transport_timeout = min(lane["wall_seconds"], 600)`
from the **lane** record. The board task's `budget.wall_seconds` (600 for the runner review) never
reaches it, so a 16k-token `kimi-k3` review of the board runner timed out at the lane's 400 s with
no reply, twice (`review-lane-cline` on 2026-09-12 at the old fixed 150 s, `review-board-runner-3`
on 2026-09-13 at 400 s). The two smaller reviews completed in 2 and 5.5 minutes on the same lane, but an 8.5 KB
`review-scorecard-summary` packet also timed out at 400 s, so the lane is intermittently slow rather
than simply outrun by large inputs; the budget fix is still correct, it is just not sufficient.
- Pass the attempt's budget through the lane runner request and let `run` use
  `min(task_wall, lane_wall, 600)` — the tighter of the two, never longer than the lane allows.
  Record both bounds in the verdict.
- Tests in `tests/test_lane_go.py` with an injected `send` that asserts the timeout it received.
- Size: small.

#### A4. Superseded tasks have no state, so retries are recorded as `blocked`
BOARD.md allows retries "only as new tasks with a recorded change", but a task has no way to say
what it supersedes, and the predecessor has no terminal state other than `blocked`. On 2026-09-13
the operator blocked `review-board-runner`, `review-board-runner-2` and `review-lane-cline` with
prose reasons naming their successors. `doctor` now counts those as blocked work.
- Without editing `board/task.py`: treat a `blocked_reason` beginning with `superseded:` as a
  closed task in `doctor` (count it under `superseded`, not `blocked`) and in the runner's
  `propagate_rejection`/`accept_reviewed` (never touch a superseded source). Document the
  convention in BOARD.md.
- Tests in `tests/test_doctor.py` and `tests/test_board_runner.py`.
- Size: small.

## B — operating-model features still missing

#### B1. `board-status` CLI
Reading the board today took five sqlite queries and a loop over JSON files. Add a read-only
`inference-grid board-status --json {board_dir}` printing one row per task: id, state, lane list,
author family, `blocked_reason` (first 80 chars), and for `review_pending` tasks the review task id
and its state; for `blocked` tasks whose reason names an attempt id, that attempt's ledger state.
No dispatch, no writes.
- Tests in `tests/test_board_status.py` with a temp board and a temp ledger.
- Size: medium.

#### B2. Cline lane: forbid deleting a passing artifact inside an attempt
From CONTRIBUTIONS (2026-09-12): a passing artifact was destroyed to chase a line-count hint. The
snapshot supervision now exists in `lanes/cline.py`; add the guard: if a snapshot at
`iteration_end` N contained every expected artifact and iteration N+1 removed one, stop the run,
restore the last complete snapshot into `artifacts/`, and record `restored_from_snapshot: N` in the
verdict.
- Tests with a fake CLI that writes then deletes a file across two iterations.
- Size: medium.

#### B3. Auto-exclude review models the Go endpoint refuses
CONTRIBUTIONS 2026-09-12: `deepseek-v4-flash` and `grok-4.6` returned 403 RegionError, an oa-compat
model 401. Only `kimi-k3` reviews on Go. Record a per-model `unsupported_until` in the lane record
when the endpoint returns 401/403 for the model, and have `readiness_view` mark such a lane
`unqualified` for that model until then. Do not edit `lane_readiness.py` or `lanes/select.py`
(provider-authored); shape their inputs.
- Tests: a 403 verdict sets the record; the next readiness view excludes the lane; expiry restores it.
- Size: medium.

## C — out of scope
Running boards or lanes, editing `lanes.json` or any credential, installing or loading
`local.inference-grid.board-tick.plist` (operator decision; the board idled eight hours because it
is not loaded), releases, pushes, anything under `~/.grid-workspaces`.

---

## Definition of done, per item
1. Tests added or extended; whole suite passes; `ruff check` and `ruff format --check` pass.
2. `git status --short` shows only files under `src/`, `tests/`, `docs/`, `grid/`.
3. One commit per item, message explaining why, your own trailer.
4. A row in `docs/CONTRIBUTIONS.md`.

Report at the end: items done (commit hashes), items skipped and why, bugs found but not fixed.
