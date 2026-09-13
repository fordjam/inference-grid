# Handoff brief 4 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-3.md` (B1–B4 done 2026-09-13, `3834901`…`113d427`; 283 tests).
**Section 1 of `docs/handoff-glm.md` applies unchanged.**

This is a short brief on purpose. The defect backlog from live operation is spent; the next real
defects come from the next live round (tier 3 below), which the coordinator runs. These three
items make that round cheaper to run and read.

---

#### B1. `board-tick --dry-run`: the plan, without the dispatch
Today's ticks were run blind: the operator learned which lane each task would take, and why
three were refused, only after quota was spent. Add `dry_run: true` to the `board-tick` JSON:
the runner does everything up to and including `select_lane` for every `ready` task — readiness
view, campaign windows, busy accounts, family exclusion, `unsupported_until` — and returns one
row per task `{task, lane | null, reason}` plus the readiness view it used, **without** creating
an attempt, writing a task file, or touching a packet directory. Assert that last part in the
test by comparing the board directory and ledger before and after.
- Tests in `tests/test_board_runner.py`: three tasks, one busy lane, one excluded family; the
  plan names the lane or the exact skip reason; nothing on disk or in the ledger changes.
- Size: small.

#### B2. `evaluation`: the scorecard as a document
`ledger.scorecard()` exists and the dashboards overlay it (handoff-1 B3), but `docs/EVALUATION.md`
is hand-written and stale. Add `inference-grid evaluation --json {out?}`: render the scorecard
rows as markdown — per family, model and category: attempts, completions, acceptances,
acceptance rate, mean tokens in/out where recorded, last attempt time — followed by the
per-account readiness view. Write to stdout, or to `out` when given; never into `docs/` by
itself (the coordinator decides when a regenerated `docs/EVALUATION.md` is committed).
- Tests: temp ledger with a handful of attempts and outcomes; rates and ordering; empty ledger
  renders headers only.
- Size: small.

#### B3. `board-status --suggest`: the retry, pre-filled
`board-status` now shows a blocked task's held attempt with its refusal and bounds (handoff-3
B3/B4), and `board-new --retry` exists (handoff-3 B1). Join them: with `suggest: true`, for every
blocked task whose attempt refusal starts with `transport_error` and whose artifacts are absent,
print the exact `board-new` JSON that would retry it with `wall_seconds` raised to
`min(900, 2 × previous)` and the `change` string pre-written from the refusal. Print only —
never author.
- Tests in `tests/test_board_status.py`.
- Size: small.

## Tier 3 — the next live round (coordinator runs it; not for this brief)
1. `board-tick --dry-run` on the live board once B1 lands, to see the plan.
2. `board-new --retry review-board-runner-3` and `--retry review-scorecard-summary` at 900 s.
3. One tick. If `scorecard-summary` is approved, that is the first real `land_in_inbox`; inspect
   the `grid/inbox` branch against what `tests/test_board_end_to_end.py` expects.
4. Whatever that round exposes becomes handoff 5.

## C — out of scope
Same as handoff 1.

---

## Definition of done, per item
1. Tests added or extended; whole suite passes; `.venv/bin/ruff check src tests` passes; do not
   reformat files you did not otherwise touch.
2. `git status --short` shows only files under `src/`, `tests/`, `docs/`, `grid/`.
3. One commit per item, message explaining why, your own trailer; a row in `docs/CONTRIBUTIONS.md`.

Report at the end: items done (commit hashes), items skipped and why, bugs found but not fixed.
