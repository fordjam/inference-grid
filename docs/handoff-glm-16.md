# Handoff brief 16 — the board closes its own loop (any lane, dispatched by the board)

Follows `docs/handoff-glm-15.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.**
These packets are dispatched as `packet` tasks by `board-tick` itself (brief 14 D1) — the
first work the grid has run on its own harness rather than through `scripts/run_lane.py`.
Branch from `glm/work`; the runner fetches a passing branch as `packet/<task-id>` and never
advances the base — the coordinator (or I1, once it lands) does.

## Why this brief exists (2026-09-15, 04:00–09:00 UTC)

Seventeen packets landed across two briefs. Six of the landings needed the coordinator's
hands: union merges of `CONTRIBUTIONS.md`, one doubled `else` from a bad union, three tests
that pinned a plan-row shape another packet enriched. Every review packet for the vix-rs
line ran on a fixed 22 K-token cap while `route` (C1) can already size a cap from the staged
bytes. Calibration has one scored run, by hand. The ops layer is versioned and running; the
loop that lands work, sizes reviews and measures reviewers is still the operator.

---

## Phase I — landing, sizing, measuring

#### I1. Landing as a code node: `verify-merge` in front of `inbox-integrate` for packet branches
When a `packet` task settles `passed`, its branch is at `packet/<task-id>` in the project
repository and nothing happens next. Add `board/land.py` and CLI `inference-grid land --json
{board_dir, project_root, task, base, gates, dry_run}`:
- Runs `verify_merge` (B1) for `packet/<task-id>` against `base` with the task's declared
  gates in a scratch worktree. On `mergeable` and gates green: merge into `base` with
  `--no-ff` **in a scratch worktree of the base**, never the operator checkout; conflicts
  confined to `docs/CONTRIBUTIONS.md` and `docs/LANES.md` are resolved by keeping both sides
  (rows are independent), any other conflict aborts and blocks the task with the file list;
  the suite runs once more on the merged tree before the merge commit is kept.
- Serialized: one landing at a time per base, a lock directory under `packets_root`.
- Writes `landed: {base_head, merge_commit, how: ff|merge|union}` onto the task, settles
  it `landed` (a new terminal state after `passed`; `board/task.py` is provider-authored —
  validate the transition in `land.py`), and records a ledger event.
- `board-tick` calls it for every `passed` packet task when the board config carries
  `"auto_land": true`; `--dry-run` reports what would land and why not.
- Tests (`tests/test_land.py`): clean fast-forward; a merge with a CONTRIBUTIONS-only
  conflict landed by union; a source conflict blocked with the file named; a gate failing
  on the merged tree blocks and leaves the base untouched; the lock; dry run.
- Size: medium–large. Why: six of seventeen landings tonight were hand-merged.

#### I2. Review budgets sized from the packet, not a constant
`lanes/route.py` filters lanes whose cap cannot hold a packet (`budget_unfit`). The review
line still authors every packet with `thinking_tokens: 6000` and the Go lane derives
`max_tokens` from that. Make the budget follow the staged bytes:
- `board/branch_review.py` sets `thinking_tokens` per packet from the staged input size:
  `min(24000, max(6000, staged_bytes // 4))` (a 16 K-token packet asks for 6 000 → cap
  22 000; a 60 K-byte packet asks for 15 000 → cap 49 000), and records `staged_bytes` and the
  chosen budget on the task.
- `route` reports the cap it computed for each candidate in `dropped`/`candidates` rows so
  the dry run explains a refusal in tokens.
- Tests: three packet sizes → three budgets; the runner's plan row carries the cap; a lane
  whose cap is below the packet's need is `budget_unfit` with the two numbers.
- Size: small–medium. Why: the 46 KB vix-rs packet needed three attempts on a fixed cap.

#### I3. Calibration as a routine, not a one-off
- `board/calibration.py` gains `calibration_task(board_dir, corpus_dir, lanes, run_id)`:
  one board task of `category: "calibration_run"` that, when the runner dispatches it,
  authors the per-case review tasks (existing `author_calibration`) and, when they have all
  settled, scores them (existing `score_calibration` with `record=True`) and settles itself
  `passed` with the per-lane recall/precision in its `result` — no lane runs the
  calibration task itself; it is a code node the runner steps through across passes.
- `tick-boards` config gains `calibration: {corpus_dir, lanes, every_days: 7}`; the versioned
  `deployments/local/tick_boards.py` authors a run when the newest scored run for those
  lanes is older than `every_days` (state in the ledger's outcomes, not a file).
- The overlay's `accepted_work` gains a sibling `reviewer_recall`: `{run_id, lane, recall,
  precision, scored_at}` for the newest run per lane, carried through `clean_snapshot` and
  rendered in the dashboard's Evidence section beside accepted work.
- Tests: the task's state machine across three ticks (authored → waiting → scored); the
  weekly trigger with a fake clock; the overlay row and the cloud cleaner.
- Size: medium. Why: recall is the number that says what an approval is worth; one
  measurement is a snapshot.

---

## Definition of done, per packet
As brief 15. The runner's gates are the judge: pytest (baseline-aware), scoped ruff, home
paths, exactly one trailered commit. Report at `docs/reports/brief-16-<packet>.md`.
