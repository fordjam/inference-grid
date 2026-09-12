# Task board and autonomous coordinator

Planned operating model for continuous Grid operation (agreed 2026-09-12). The Grid builds the Grid: lanes author modules against coordinator-written specs and tests; the coordinator integrates and gates.

## Board

`grid/board/<task-id>.json`, one task each:

| Field | Meaning |
| --- | --- |
| `id`, `category` | Stable id; category from the scorecard vocabulary (`pure_function`, `tests_multi_file`, `fixtures_multi_file`, `independent_review`, `canary`) |
| `brief` | Path to the exact text sent to the provider, format rule restated last |
| `inputs` | Files staged read-only for the attempt (manifest hashed by the ledger) |
| `tests` | Coordinator-written tests run against the artifact before any acceptance |
| `artifacts` | Expected artifact names; anything else is refused |
| `lanes` | Allowed lane ids; selection uses `inference_grid.lanes.select` over readiness and the scorecard |
| `author_family` | Set for review tasks so the reviewer family differs |
| `budget` | Wall seconds, output cap, thinking budget where the lane supports one |
| `state` | `ready`, `dispatched`, `passed`, `review_pending`, `accepted`, `blocked` with a reason |

## Runner tick

1. Refresh lane readiness (`lane` records, campaign windows via `flash_window`, quota files).
2. For each `ready` task, `select_lane`; skip with a recorded reason when none.
3. Dispatch through the ledger with the packaged lane adapter (`inference_grid.lanes.<kind>`); one attempt per task id; retries only as new tasks with a recorded change.
4. Run the task's tests on the artifact; record `outcome`; on pass, create the review task with `author_family` set.
5. On an approved review, `accept()`; move artifact, qualification record and scorecard delta to the `grid/inbox` branch.

Nothing merges automatically. Held attempts wait for `resolve` with evidence.

## Coordinator schedule

- Local launchd job on the collector host: runner ticks every 15 minutes; ZCode-class work queues for the campaign window; Cline only after resets; GOAT only for multi-file work.
- Daily cloud routine: integrate the inbox branch, write CONTRIBUTIONS/EVALUATION rows, open the release PR; the operator approves the release.

## Lane kinds

`go_http` (dedicated subscription endpoint, JSON schema gate), `goat_cli` (`--mod` session effort, `classify_goat`), `cline_cli` (write sandbox, snapshot supervision, `classify_cline`), `claude_headless` (Anthropic-compatible base URL, thinking budget), `zcode_cli` (bundled CLI, session-DB evidence, `~/.zcode` write allowance). Configuration lives in a private `lanes.json` validated by `inference_grid.lanes.config`.

## Packaged lanes

`inference-grid-lane <lane-id> --config lanes.json` is the single trusted adapter argv for every packaged lane. The worker passes the attempt request on stdin; the runner validates the private `lanes.json`, dispatches to `inference_grid.lanes.<kind>`, writes `verdict.json` beside the attempt and prints one receipt. Refusals (no native terminal, unexpected provider or model, missing artifacts) exit non-zero so the ledger holds the attempt with the reason. Expected artifact names come from `inputs/expected.json`; without it, every new file at the workspace root is the artifact set, and a reply-only task publishes `reply.txt`. Packaged so far: `zcode_cli`, `claude_headless` and `go_http` (canaries `zcode-runner-canary-2`, `zai-runner-canary` and `go-runner-canary-1` completed through the ledger on 2026-09-12). Each lane uses a scratch HOME under the attempt directory; the real home is never a writable sandbox root.
