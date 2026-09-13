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
5. On an approved review from a different family, the runner calls `accept()` on the reviewed attempt, marks the source task `accepted`, and commits the artifacts plus a record (`grid/inbox/<task-id>.json`) to the project's `grid/inbox` branch through a separate worktree under `~/.grid-workspaces/inbox/`; tree tasks land at their project paths, flat tasks under `grid/inbox/<task-id>/`. Nothing is pushed or merged. The link between a review and the attempt it judges lives in `<board>/review/<task-id>/source.json`.

Retries are new tasks with a recorded change; the predecessor's `blocked_reason` then begins with `superseded:` naming its successor. Doctor counts such a task under `superseded`, not `blocked`, and the runner never accepts or re-blocks a superseded source.

Nothing merges automatically. Held attempts wait for `resolve` with evidence, with one exception the runner applies itself: an attempt held at its wall deadline whose expected files all exist in its workspace is resolved `consumed` and followed by a single verify-only attempt (a new ledger task id, a changed brief that only runs the tests, both recorded); anything else stays held.

## Coordinator schedule

- Local launchd job on the collector host: runner ticks every 15 minutes; ZCode-class work queues for the campaign window; Cline only after resets; GOAT only for multi-file work.
- Daily cloud routine: integrate the inbox branch, write CONTRIBUTIONS/EVALUATION rows, open the release PR; the operator approves the release.

## Lane kinds

`go_http` (dedicated subscription endpoint, JSON schema gate), `goat_cli` (`--mod` session effort, `classify_goat`), `cline_cli` (write sandbox, snapshot supervision, `classify_cline`), `claude_headless` (Anthropic-compatible base URL, thinking budget), `zcode_cli` (bundled CLI, session-DB evidence, `~/.zcode` write allowance). Configuration lives in a private `lanes.json` validated by `inference_grid.lanes.config`.

## Packaged lanes

`inference-grid-lane <lane-id> --config lanes.json` is the single trusted adapter argv for every packaged lane. The worker passes the attempt request on stdin; the runner validates the private `lanes.json`, dispatches to `inference_grid.lanes.<kind>`, writes `verdict.json` beside the attempt and prints one receipt. Refusals (no native terminal, unexpected provider or model, missing artifacts) exit non-zero so the ledger holds the attempt with the reason. Expected artifact names come from `inputs/expected.json`; without it, every new file at the workspace root is the artifact set, and a reply-only task publishes `reply.txt`. Packaged: `zcode_cli`, `claude_headless`, `go_http`, `goat_cli` and `cline_cli` (canaries `zcode-runner-canary-2`, `zai-runner-canary` and `go-runner-canary-1` completed through the ledger on 2026-09-12). Each lane uses a scratch HOME under the attempt directory; the real home is never a writable sandbox root.

## Sensitive data

Board inputs are sent to third-party models, so only files a task lists are staged, and `board.guard` refuses any input whose name looks like a credential store (`.env*`, `auth.json`, `credentials*`, key files, databases, `lanes.json`) or whose content matches a credential pattern (private key blocks, `sk-`/GitHub/AWS/Slack token shapes, bearer tokens, `api_key: …` assignments) or is binary. Lane modules read credentials from mode-0600 files named in the private `lanes.json`, pass them only through the child environment or request headers, and never write them to receipts, verdicts or attempt files; each lane runs with a scratch HOME. The board ledger and packet roots are private (0700/0600). The cloud dashboard receives only sanitized usage fields.

## Attested readings

Providers without a usage API (the Z.ai Coding Plan) are admitted from an operator-attested console reading kept in a private file with its true observation time. Policy, not measurement: such a reading stays valid for 24 hours for admission and lane readiness; ZCode consumes no plan quota inside the campaign window. Every other lane uses a collector reading no older than 15 minutes, refreshed by the board's prepare step before each dispatch.
