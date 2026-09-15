# Task board and autonomous coordinator

Planned operating model for continuous Grid operation (agreed 2026-09-12). The Grid builds the Grid: lanes author modules against coordinator-written specs and tests; the coordinator integrates and gates.

## Board

`grid/board/<task-id>.json`, one task each:

| Field | Meaning |
| --- | --- |
| `id`, `category` | Stable id; category from the scorecard vocabulary (`pure_function`, `tests_multi_file`, `fixtures_multi_file`, `independent_review`, `canary`, `packet`) |
| `brief` | Path to the exact text sent to the provider, format rule restated last |
| `inputs` | Files staged read-only for the attempt (manifest hashed by the ledger) |
| `tests` | Coordinator-written tests run against the artifact before any acceptance |
| `artifacts` | Expected artifact names; anything else is refused |
| `lanes` | Allowed lane ids; selection uses `inference_grid.lanes.select` over readiness and the scorecard |
| `author_family` | Set for review tasks so the reviewer family differs |
| `budget` | Wall seconds, output cap, thinking budget where the lane supports one |
| `state` | `ready`, `dispatched`, `passed`, `review_pending`, `accepted`, `blocked` with a reason; a packet task also settles `landed` (see Landing) |
| `spec` | Packet tasks only (see Packet tasks below); absent on every other category |

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

## Packet tasks

A task with `category: "packet"` runs the build→gate→re-enter loop that used to live only
in the operator's `scripts/run_lane.py` — the board now dispatches it itself. The spec
shape (validated by `board/packet_task.py`; `board/task.py` is provider-authored, so the
caller adapts):

```json
{"brief": "grid/briefs/packet-d1.txt", "packet_id": "D1",
 "gates": [{"name": "pytest", "argv": ["python", "-m", "pytest", "-q"], "cwd": ".",
            "timeout": 1800, "env": {}}],
 "max_rounds": 3, "base": "main"}
```

`spec.brief` is the task's own brief file; `packet_id` names a `#### <id>.` heading inside
it, and the same document must carry the umbrella brief's `## 1. Hard rules` section — the
prompt is composed from both, plus scout orientation, by `lanes/brief.py` (the module the
operator's driver imports). The declared `gates` run as code after each agent round, then
the commit gate: exactly one commit ahead of `base`, trailer present, clean tree.
`max_rounds` bounds the re-entry loop (default 3).

Dispatch admits the attempt first — submit, claim, workspace lease — and only then builds
the worktree: a scratch shared clone of the project inside the attempt directory, branched
from `base`, never the operator checkout. The loop runs on the lane's adapter
(`goat_cli` → `CommandCodeAdapter` with a high-effort module beside the attempt, not in
the worktree; `zcode_cli` → `ZcodeAdapter`; `cline_cli` is refused before any attempt: its
CLI has no session-resume flag, so the loop could not re-enter). Packet tasks may select
unverified or unqualified lanes the way canaries do — the task names its lanes explicitly
and no packet could run to earn the qualification rows first — but a recently
model-refused lane stays closed.

On green gates the branch (`packet/<task-id>`) is fetched into the project repository, the
attempt completes with the verdict as its receipt (`verified_in_lane`, `repairs` =
rounds − 1, and an artifact digest pinning the exact head commit), and the task settles
`passed` with no review task — the gates already ran as code inside the attempt. On any
other outcome the attempt is held with the loop's reason and the branch is left inside the
attempt directory for the operator. The runner itself never advances the base; landing is
the code node below. `board-tick --dry-run` plans packet tasks like any other.

## Landing

`inference-grid land --json {board_dir, project_root, task, base, gates, dry_run}`
(`board/land.py`) settles a `passed` packet task: it runs `verify_merge` for
`packet/<task-id>` against `base` with the task's declared gates in a scratch worktree, and
on a clean merge with green gates merges the branch into `base` with `--no-ff` in a scratch
worktree of the base — never the operator checkout. Conflicts confined to
`docs/CONTRIBUTIONS.md` and `docs/LANES.md` are resolved by keeping both sides (git's union
merge driver; rows there are independent); any other conflict, or a gate failing on the
merged tree, blocks the task with the file list and leaves the base untouched. The suite
runs once more on the real merged tree before the merge commit is kept. `how` records what
the landing was: `ff` (the base head was an ancestor of the branch), `union` (conflicts
resolved by keeping both sides) or `merge`. Landings serialize on a lock directory under
`packets_root`, one per base. When the base is checked out at the project root itself it
must have no tracked local changes — the landing moves that ref and syncs the checkout with
`reset --hard`; a base held by any other worktree is refused. On success the task settles
`landed` with `landed: {base_head, merge_commit, how}` (validated in
`board/packet_task.py`, since the provider-authored `board/task.py` refuses both the state
and the extra key) and a `packet_landed` ledger event is recorded; a refusal leaves the
task `passed` for the next tick. `--dry-run` reports what would land and why not, touching
nothing. A tick whose board config carries `"auto_land": true` lands every `passed` packet
task after the dispatch pass.

## Authoring tools

`board-new --json {task: …}` writes one validated task file with its empty brief (handoff-1 B4); `{retry, change, budget?, lanes?, author_family?}` authors the authorised retry — a new task with a `superseded:` predecessor, the source link moved for board-work reviews (handoff-5/7).

`{review_branch: {repo, base, tip, scope?}}` authors independent_review task(s) from a git range, staging the changed files plus a generated `diff.patch` under `grid/board/review/<task-id>/`. The keys inside `review_branch` are exactly `repo`, `base`, `tip` and the optional `scope` — anything else is refused. `scope: "branch"` (see Review policy) stages the merged tree's diff instead of the commit range and requires a passed `verify_merge` task for the same branch. The top-level knobs are:

| Knob | Meaning |
| --- | --- |
| `max_input_bytes` | Packet budget in staged bytes (default 120 000 ≈ 30k tokens); over budget the range splits or refuses |
| `split` | `"commit"` authors one task per commit (also the over-budget fallback); `"none"` keeps one task and refuses over budget |
| `include_docs` | Review docs-only commits too (default: skipped, listed `docs-only, not reviewed`) |
| `exclude_commits` | Sha prefixes to skip in a split, listed `excluded by operator` |
| `lanes`, `budget` | The review task's lanes and budget (defaults `["go"]` and the review budget) |

## Review policy

Every passing work task gets one `independent_review` task by default. That is not free and
it is not always informative: when the source attempt's receipt already proves the work —
`verified_in_lane: true` **and** every declared gate passed — and the author family is not
`claude`, the per-commit review is **waived** (`board/policy.py::review_needed`). Only that
proof waives. A missing receipt, an unverified lane, a receipt that declares no gate
results, any declared gate that did not pass, and a first-party author all keep the current
path and author the review. A packet task whose lane could not verify itself also keeps the
current path (no per-commit review; see Packet tasks), and the branch-scope review below is
what reads such a branch as a whole.

A waiver is never silent. It is recorded as the source task's review record at
`<board>/review/<task-id>/review.json` (`{"review": {"waived": true, "reason": …}}`) and, in
the ledger, as a `review_waived` event. The record is a board-owned sidecar, not a task
field: `board/task.py` is provider-authored (integrated unmodified) and refuses any key
outside its fixed schema, so the record lands where a review task's `source.json` link
already lives.

The evidence that earns the waiver is calibration recall, not the boolean. A green gate
says the code the gate exercises behaved; `board/calibration.py` measures how much a
reviewer actually finds, per lane, against known answer keys. Read a lane's recorded recall
and the waiver is a decision made on measured evidence; without it, `verified_in_lane` is
only a claim a lane makes about itself.

`review_branch(..., scope: "branch")` reads a branch the merge node has already proved: one
packet for the whole branch-vs-target diff, staged as the merged tree (`git merge-tree`)
against the *target's* current tree, never the branch's history. It is authored only when a
`verify_merge` task for the same branch settled `passed`, and its brief asks for findings
against the target's current tree — the thing no per-commit review can see.

## Reviewer calibration

The board routes reviews by an acceptance rate that measures whether a reviewer produced
a well-formed verdict, not whether the verdict was right. Calibration measures the
difference with a corpus whose defects are known. `calibration/example/` in this
repository shows the format — clean cases to catch false positives beside cases with a
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
