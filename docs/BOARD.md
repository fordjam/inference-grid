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

First-party families (`claude`, `openai`) are `explicit_only` in the runner's lane view: a task's `lanes` must name them for them to be selected, so first-party review lanes run only where the author chose them.

`go_http` (dedicated subscription endpoint, JSON schema gate), `goat_cli` (`--mod` session effort, `classify_goat`), `cline_cli` (write sandbox, snapshot supervision, `classify_cline`), `claude_headless` (Anthropic-compatible base URL, thinking budget), `zcode_cli` (bundled CLI, session-DB evidence, `~/.zcode` write allowance). Configuration lives in a private `lanes.json` validated by `inference_grid.lanes.config`.

The `go_http` request shapes reasoning explicitly, since kimi-k3 thought its whole output away at the endpoint's default effort: `lanes/go.py::REASONING_EFFORT` maps each model id to the `reasoning_effort` values its documented endpoint schema accepts (`kimi-k3` → `low`, `high`, `max`; the endpoint default is `max`), and `lanes/go.py::EFFORT_TIERS` is the coordinator's policy mapping the task's `thinking_tokens` to a tier — absent or at most 4 000 → `low`, at most 12 000 → `high`, beyond → `max` — so board review tasks (6 000) ask for `high`. The token count also sizes `max_tokens` at `3 × thinking_tokens + 4 000` (`lanes/go.py::REASONING_HEADROOM`): the endpoint bounds reasoning only by effort, never by count — on 2026-09-14 `high` reasoned 1.4–1.7× a 6 000 budget on 16–18 K-token review prompts, and a `thinking_tokens + 4 000` cap cut one review off mid-finding — so the cap guards spend rather than thinking. A model outside the map sends no effort field and the verdict records `reasoning_effort: unsupported` (plus `reasoning_budget: unsupported` when a thinking budget was requested); a `length` stop with no content is held as `reasoning_overrun` with both counts.

## Authoring tools

`board-new --json {task: …}` writes one validated task file with its empty brief (handoff-1 B4); `{retry, change, budget?, lanes?, author_family?}` authors the authorised retry — a new task with a `superseded:` predecessor, the source link moved for board-work reviews (handoff-5/7).

`{review_branch: {repo, base, tip}}` authors independent_review task(s) from a git range, staging the changed files plus a generated `diff.patch` under `grid/board/review/<task-id>/`. The keys inside `review_branch` are exactly `repo`, `base`, `tip` — anything else is refused. The top-level knobs are:

| Knob | Meaning |
| --- | --- |
| `max_input_bytes` | Packet budget in staged bytes (default 120 000 ≈ 30k tokens); over budget the range splits or refuses |
| `split` | `"commit"` authors one task per commit (also the over-budget fallback); `"none"` keeps one task and refuses over budget |
| `include_docs` | Review docs-only commits too (default: skipped, listed `docs-only, not reviewed`) |
| `exclude_commits` | Sha prefixes to skip in a split, listed `excluded by operator` |
| `lanes`, `budget` | The review task's lanes and budget (defaults `["go"]` and the review budget) |

## Reviewer calibration

The board routes reviews by an acceptance rate that measures whether a reviewer produced
a well-formed verdict, not whether the verdict was right. Calibration measures the
difference with a corpus whose defects are known. `calibration/example/` in this
repository shows the format — one clean case to catch false positives and one with a
planted defect — each holding a `diff.patch`, the changed files and a brief, plus an
`answer.json` that is never staged. A real corpus is built from the defect classes an
operator's own gates have caught; it lives outside the repository (pass its path as
`corpus_dir`) because it is a record of that operator's projects. `inference-grid calibrate --json
{board_dir, project_root, corpus_dir, lanes, run_id}` authors one independent_review task
per case (`calib-<run_id>-<case>`, `author_family: "calibration"` — a sentinel no lane
declares, so every listed lane stays eligible) and writes the answer keys plus a manifest
under `<board_dir>/calibration/<run_id>/`. After the board settles the replies,
`inference-grid calibration-score --json {board_dir, run_id, packets_root, record}` reads
each packet's reply.txt through the runner's own verdict reader and reports per lane:
cases, defects, recall, false positives, precision and severity-weighted recall
(high=3, medium=2, low=1), written to `report.json` with the markdown tables printed. A
defect is recalled only when a finding names its file (relative path or basename) and
carries every `must_mention` keyword case-insensitively; a finding matching no defect is
a false positive. Nothing reaches the ledger unless `record: true`, and then only one
`record_outcome` per calibration attempt with category `calibration`.

## Packaged lanes

`inference-grid-lane <lane-id> --config lanes.json` is the single trusted adapter argv for every packaged lane. The worker passes the attempt request on stdin; the runner validates the private `lanes.json`, dispatches to `inference_grid.lanes.<kind>`, writes `verdict.json` beside the attempt and prints one receipt. Refusals (no native terminal, unexpected provider or model, missing artifacts) exit non-zero so the ledger holds the attempt with the reason. Expected artifact names come from `inputs/expected.json`; without it, every new file at the workspace root is the artifact set, and a reply-only task publishes `reply.txt`. Packaged: `zcode_cli`, `claude_headless`, `go_http`, `goat_cli` and `cline_cli` (canaries `zcode-runner-canary-2`, `zai-runner-canary` and `go-runner-canary-1` completed through the ledger on 2026-09-12). Each lane uses a scratch HOME under the attempt directory; the real home is never a writable sandbox root.

## Sensitive data

Board inputs are sent to third-party models, so only files a task lists are staged, and `board.guard` refuses any input whose name looks like a credential store (`.env*`, `auth.json`, `credentials*`, key files, databases, `lanes.json`) or whose content matches a credential pattern (private key blocks, `sk-`/GitHub/AWS/Slack token shapes, bearer tokens, `api_key: …` assignments) or is binary. Lane modules read credentials from mode-0600 files named in the private `lanes.json`, pass them only through the child environment or request headers, and never write them to receipts, verdicts or attempt files; each lane runs with a scratch HOME. The board ledger and packet roots are private (0700/0600). The cloud dashboard receives only sanitized usage fields.

## Attested readings

Providers without a usage API (the Z.ai Coding Plan) are admitted from an operator-attested console reading kept in a private file with its true observation time. Policy, not measurement: such a reading stays valid for 24 hours for admission and lane readiness; ZCode consumes no plan quota inside the campaign window. Every other lane uses a collector reading no older than 15 minutes, refreshed by the board's prepare step before each dispatch.
