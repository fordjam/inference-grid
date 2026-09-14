# Migrating yt-research-mcp's dispatch stack to the Grid

The scripts in `~/projects/yt-research-mcp/scripts/` predate the Grid. They still read a
ledger that is no longer the live one, and every hand-dispatch they enable is a
hand-dispatch the board now runs with recorded evidence and cross-family review. This
note maps each script to its replacement (or names the gap) and orders the retirement.
No edit has been made in that repository.

## What each script does, and what replaces it

| Script | What it does today | Grid replacement | Gap |
| --- | --- | --- | --- |
| `capacity.py` | Reads every metered lane, records spend, `--dispatch` starts a job, `--done` closes it, `--open` lists unreconciled dispatches | `collect` + the ledger's accounts and scorecard are the meters; `board-tick` is the dispatcher; held attempts + `resolve` are the reconciliation | The Grid does not yet read the plan's own quota console per seat — the lane records must be refreshed by the collector until then |
| `capacity_report.py` | Renders the capacity table for reading | `inference-grid evaluation` (scorecard, acceptance rates, per-account readiness) and the dashboard's Evidence section | None for the reading use-case |
| `routing.py` | Per-class lane policy (lane, fallbacks, effort), reviewer affinity via Codex session ids, per-lane/class yield | `lanes.json` + `inference_grid.lanes.select` (family rules, window gating, category qualification) and the scorecard's acceptance rates; `--resume-id` has no Grid equivalent | Session-resume (warm reviewer affinity) is not modeled — a review is always a fresh packet, which the cross-family rule wants anyway |
| `codex_dispatch.sh` | One dispatcher for Codex jobs: class, effort, kill timer, ledger rows, reviewer affinity, `--commit` of reviewed paths | The `codex_cli` lane module (wall deadline, event-stream verification) behind a board task; `board-status --suggest` and `inbox-integrate` cover the post-review half | Effort classing is per-task budget (`thinking_tokens`) rather than a class table; no session resume |
| `opencode_dispatch.sh` | Same, for the OpenCode Go endpoint | The `go` / `go-kimi` lanes (`go_http` with `reasoning_effort` tiers and overrun refusals) | None for the dispatch itself |
| `runner.py` | Runs `config/jobs.yml` command jobs one at a time in lanes with room; `--execute` after a plan | A board per project: `board-init`, tasks authored (`board-new`), dispatched by `tick-all` on the scheduler, gated by readiness and category qualification | The jobs.yml concept of *command jobs* (non-model shell work) has no board category — keep `runner.py` for pure shell jobs or propose a `command` category |
| `board_audit.py` | Audits the board's own task files (aging, strict violations, `--fix-plan`) | Still useful against the Grid's own boards — point it at a `grid/board` directory | Re-read against `board.task`'s validation and the `superseded:` convention before trusting its verdicts |

## What has no replacement yet

1. Codex session resume (`routing.py --resume-id`, `codex_dispatch.sh --resume`). The Grid
   re-reviews from packets by design; if warm-start reviews prove necessary, that is a
   lane-capability fact to record, not a dispatcher feature.
2. Effort *classes* (`review` / `build-spec` / `build-tooling`). The Grid expresses the
   same lever as per-task budgets (`thinking_tokens`, `wall_seconds`); a class table
   would be a thin template over those.
3. Reading the plan console per seat. `capacity.py`'s read side stays until the
   collector covers the same seats.

## Retirement order

1. Freeze writes: stop using `capacity.py --dispatch/--done` and both `*_dispatch.sh`
   scripts for new work; author the work as board tasks instead (the review flow is
   already cheaper: `--review-branch` + one tick).
2. Keep reads: `capacity.py` (read mode) and `capacity_report.py` remain the only
   consumers of the old ledger until the collector covers the same seats.
3. Retire `routing.py` once its yield numbers are superseded by the scorecard's
   acceptance rates per lane/class — the qualification matrix is the successor.
4. Retire `runner.py` when the last command job is either a board task or explicitly
   out of scope for the Grid.
5. Re-point `board_audit.py` at the Grid's boards; retire the rest of the stack, then
   the old ledger itself.
