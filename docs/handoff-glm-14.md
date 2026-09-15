# Handoff brief 14 — noticing, not just running (GLM, Command Code lane)

Follows `docs/handoff-glm-13.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.**
Every packet below is a code node in the sense of `docs/research/yt-VQy50fuxI34-ai-developer-workflows.md`:
plain code where code is enough, an agent only where judgement is needed, and a test on every
node and edge. None of it reads `lanes.json`, a credential, `~/.local/share/` or
`~/.grid-workspaces/`; where a step needs the operator, the packet says exactly what the
operator runs.

## Why this brief exists (state on 2026-09-14, from the ledger and the tree)

The headline goal is *more accepted work from the capacity you already have*. Tonight the grid
**ran** work well — the packet loop shipped the calibration tooling in 25 minutes, Kimi
scored 4/4 recall on the seed corpus, 14/14 vix-rs commits were reviewed — and **noticed**
nothing:

| What went wrong | How long | Who found it |
| --- | --- | --- |
| Z.ai quota attestation 51 h old; both GLM lanes refused as `stale` | 2 days | operator, by reading the tick log |
| Kimi attempt `held` on `go-account` (capacity 1); 14 reviews + 5 calibration packets queued behind it | 1 h | operator, ledger query |
| Calibration board never in the tick loop | since creation | operator |
| Claude usage token expired; collector reported it as `rate_limited` | 2 days | operator, probe |
| launchd parked every `StartInterval` agent while the display was off; phone showed "not uploaded for 10m" | recurring | operator's phone |
| Cloud dashboard running a build from before Sep 12; drops the Z.ai account on every upload | 2 days | operator's phone |
| `tooling/compaction-output-dir`: exporter pins a commit no clone carries; suite un-runnable off the mini | since Sep 8 | operator, following a reviewer's thread by hand |
| Same branch cannot merge to `main` (reports re-run under it); 14 per-commit approvals could not see it | since Sep 8 | operator |

Every row is a fact a query could have produced. The lane runner itself (`run_lane.py`) lived
in a scratch directory and is gone — the ops layer that runs most often is the least engineered.
Routing is hand-listed (`"lanes": ["go-kimi"]` on every packet) while two subscriptions sit idle.

The order below is the order of value. A1 and A2 are cheap and compound; A3 and A6 change what
the reviewer is for; A4 and A5 make the headline goal measurable.

---

## Phase A — the grid notices

#### A1. `watch`: stall and staleness alarms as a code node
New module `src/inference_grid/watch.py` and CLI `inference-grid watch --json spec.json`.
The spec names the inputs (all paths are operator-supplied; the module never assumes a home
directory):

```json
{"overlay": "/path/overlay.json", "upload_status": "/path/upload-status.json",
 "database": "sqlite:///path/board.sqlite",
 "boards": [{"name": "vix-rs", "board_dir": "/path/grid/board", "tick_log": "/path/tick-boards.log"}],
 "cloud_health_url": "https://host/healthz", "deploy_dir": "/path/inference-grid/deployments/capacity",
 "state": "/path/watch-state.json", "notify": ["/path/notify.sh"],
 "thresholds": {"reading_stale_s": 1200, "held_s": 900, "upload_stale_s": 300, "no_dispatch_passes": 2}}
```

Alarms, each with `kind`, `key`, `since`, `detail`:
- `reading_stale`: an overlay account whose `observed_at` is older than the threshold, or whose
  `status` is not `ok` (`auth_required`, `rate_limited`, `unknown`, `error` each named).
- `attempt_held`: a ledger attempt in state `held` longer than the threshold (id, task, reason).
- `board_stalled`: a board with ≥ 1 `ready` task whose tick log shows `no_dispatch_passes`
  consecutive passes with no `passed|held|refused|rejected` line for that board.
- `upload_stale`: `upload-status.json` older than the threshold or `status != ok`.
- `cloud_behind`: `GET cloud_health_url` returns JSON with `commit`, and it differs from
  `git -C deploy_dir log -1 --format=%H -- .` (the newest commit touching the deploy bundle).
  A plain-text `ok` body (the current server) is reported once as `cloud_health_unversioned`.
- Alarms are **edge-triggered** against `state`: raised once when they appear, `cleared` once
  when they go, re-raised after `renotify_s` (default 4 h) while they persist. The CLI prints the
  delta as JSON and, for each entry in `notify`, runs it with the message on stdin. It never
  imports a Telegram client: the notifier is the operator's script (see operator step).
- `cloud_server.py::/healthz` returns `{"ok": true, "commit": <RAILWAY_GIT_COMMIT_SHA or
  GRID_DEPLOY_COMMIT env, or null>}` with `Content-Type: application/json`; the existing tests
  that expect `ok` are updated, not weakened.
- Tests (`tests/test_watch.py`): each alarm kind from fixture files and a temp ledger; edge
  triggering (raise, silent while unchanged, clear, renotify after the interval); the notify
  hook receives exactly the delta; `/healthz` JSON in `deployments/capacity/test_cloud.py`.
- Size: medium.
- Operator step: write `notify.sh` that pipes stdin to the Telegram bot the desktop plugin already
  uses, and add `watch` to the scheduler loop delivered by A2 at a 60 s cadence.

#### A2. Version the operations layer: `deployments/local/`
The collectors, overlay builder, ledger refresh, scheduler loop, board tick loops and launchd
agents exist only under the operator's `~/.local/share/` and `monarch/grid/`. The operator has
staged copies of every script under `deployments/local/incoming/` in this worktree (untracked;
credentials and `client.json` deliberately absent). Produce the versioned layer:
- `deployments/local/` with one module per collector (`collect_zai.py`, `collect_codex.py`,
  `collect_goat.py`, `collect_cline.py`, `refresh_claude.py`, `refresh_go.py`), `overlay_build.py`,
  `board_prepare.py`, `capacity_loop.py`, `tick_boards.py` (the shell loop rewritten in Python:
  boards list from a JSON config, `board-prepare` before every board, deadline, ready count),
  `upload.py`. Every absolute home path becomes `Path.home()` or a config value; every credential
  path stays a *path read from config*, never a value. Delete `incoming/` in the same commit.
- `deployments/local/install.py`: renders the launchd plist for `capacity-loop` from a template
  (label, python, script path, log paths), writes it to a directory the operator names, and
  prints the `launchctl bootstrap` command. It never runs `launchctl` itself.
- Tests (`tests/test_local_collectors.py`): one fixture payload per provider API captured
  tonight — Z.ai `limits[]` (unit 3/number 5 → five_hour, unit 6/number 1 → weekly, percentage,
  nextResetTime ms), Codex `rate_limit.primary_window` (limit_window_seconds 604800 → weekly,
  reset_at s), GOAT `windowLimits.fiveHour/weekly` + `credits.monthlyCredits` against a 70 cap,
  Cline `data.limits[]` (percentUsed, resetsAt) — each collector's parse function returns the
  observation shape the overlay expects; `refresh_claude` reports an expired `expiresAt` as
  `auth_required` and carries the prior windows with `stale: true` on any failure; the loop's
  scheduler runs each job on its period with a fake clock and never lets one job's exception
  stop the others; `tick_boards` calls prepare before each board. Network calls are behind an
  injected `fetch`; no test opens a socket.
- A gate for this packet: `git grep -n "/Users/" -- deployments/local` returns nothing.
- Size: medium–large.
- Operator step: run `install.py`, bootstrap the plist, retire the three timer agents.

## Phase B — the reviewer gets information value

#### B1. `verify-merge`: a code node before any review
New `src/inference_grid/board/verify_merge.py` and CLI `inference-grid verify-merge --json
{repo, branch, target, gates: [{name, argv, cwd?, timeout?}], out}`:
- In a scratch worktree (never the operator checkout): check out `branch`, `git merge --no-commit
  --no-ff target`; on conflict record the conflicting paths and stop; otherwise run each gate as a
  code node (reuse `lanes/packet.py::run_gates`), then abort the merge and remove the worktree.
- Writes `out` as `{mergeable, conflicts: [...], gates: [{name, ok, returncode, tail}], branch_head,
  target_head, elapsed_s}` and exits non-zero when not mergeable or a gate fails.
- Board integration: a task with `category: "verify_merge"` and `spec: {branch, target, gates}`
  is run by `board-tick` **without a lane** — no ledger attempt, no quota — settling `passed` or
  `blocked` with the gate tail as `blocked_reason`. `validate_task` accepts the category and the
  spec shape.
- Tests (`tests/test_verify_merge.py`): temp repo with a clean branch (passed), a conflicting
  branch (blocked, paths named), a branch whose merge breaks a gate (blocked, tail carries the
  failure), the worktree removed in every case, the operator checkout's HEAD and index untouched;
  the board path through `tick` with a fake gate.
- Size: medium.
- Why: `tooling/compaction-output-dir` would have been blocked the day `main` re-ran the reports.
  No model can see that from a per-commit diff; `git merge && pytest` can.

#### B2. Review policy: waive what the gates already proved, review the branch as a whole
- `src/inference_grid/board/policy.py::review_needed(task, receipt, author_family) ->
  (bool, reason)`: a per-commit `independent_review` is **waived** when the source packet's
  receipt says `verified_in_lane: true`, every declared gate passed, and the author family is
  not `claude` — recorded on the source task as `review: {"waived": true, "reason": ...}` and in
  the ledger as an event, never silently. Everything else keeps the current path.
- `board/branch_review.py` gains `scope: "branch"`: one review packet for the whole
  branch-vs-target diff (the staging already exists for per-commit diffs; stage the merged tree's
  diff instead), authored only when a `verify_merge` task for the same branch is `passed`. Its
  brief asks for findings against the *target's* current tree, not the branch's history.
- Two new calibration cases in `calibration/corpus-v1/`, from tonight: `dangling-pin` (a script
  pins a commit hash as its data source; the answer key names the line and expects the finding to
  say the ref is not reachable from the branch) and `mutant-classified-invalid` (a **clean** case:
  a mutation harness whose classification rules look like they would misclassify a `KeyError`
  but do not, because the un-tampered path still returns the row — the reviewer must approve).
- Tests: `test_policy.py` (waived / not waived for each condition; the event written), the
  branch-scope packet in `test_branch_review.py`, the two cases load and author through
  `test_calibration.py`.
- `docs/BOARD.md`: a "Review policy" section — what is waived, why, and how calibration recall
  is the evidence that earns the waiver.
- Size: medium.

## Phase C — the headline goal, measured

#### C1. `route`: selection that uses the evidence the grid already has
`lanes/select.py` is provider-authored and integrated unmodified — **do not edit it**. Write
`src/inference_grid/lanes/route.py::route(task, lanes, readiness, scorecard, calibration, now,
inputs_bytes)` that prepares `select_lane`'s inputs and post-filters its answer:
- **Default candidates**: when a task's `lanes` is absent or empty, the candidate set is every
  lane declaring the task's category; an explicit list still restricts. `explicit_only` lanes
  (family `claude`, `openai`) are never defaulted in.
- **Budget fit**: estimate the prompt at `inputs_bytes / 4` tokens plus the brief; a lane whose
  `max_tokens` cap (from the lane spec, else `lanes/go.py`'s policy for the task's
  `thinking_tokens`) or `context` cannot hold it is filtered out with reason `budget_unfit`
  before selection, and the tick reports which lanes were dropped and why.
- **Recall weighting**: `calibration` is the list of recorded calibration reports (ledger outcomes
  with category `calibration`, per lane); the ranking key passed through is the acceptance
  score blended with recall — `0.5 * acceptance + 0.5 * recall` when a lane has a recall, else
  acceptance alone — computed here and handed to `select_lane` as the scorecard rows it reads
  (`attempts`/`accepted` reshaped so the Laplace score equals the blend). Document the reshaping.
- `board/runner.py` calls `route` where it calls `select_lane` today; the verdict carries
  `candidates`, `dropped: [{lane, reason}]`, `score`.
- Tests (`tests/test_route.py`): defaulting, explicit restriction, `explicit_only` exclusion,
  budget_unfit for the 16 K-token packet against a 10 K cap and fit against 22 K, recall blend
  arithmetic, and the runner reporting dropped lanes.
- `docs/LANES.md`: `goat` and `cline` lane entries as documentation only (provider, family,
  model, kind, executable, categories, no credential values) and a `board-prepare` paragraph
  describing the observation file each account is configured from (`goat-observation.json`,
  `cline-observation.json` — windows → remaining units against the documented caps).
- Size: medium–large.
- Operator step: paste the two lane entries into `lanes.json`; `lane-init` canaries for each.

#### C2. "Needs you": operator-blocked work on the phone, and the goal's own metric
- `deployments/local/overlay_build.py` (after A2; if A2 is not yet landed, write the function in
  `src/inference_grid/operator_queue.py` and call it from there): an `operator` list in the
  overlay — `{kind, id, reason, since}` for every board task in `blocked` whose reason names an
  operator (`operator`, `owner`, `resolve with evidence`, `superseded` excluded), every ledger
  attempt `held`, every alarm currently raised by A1's state file, and every row of an optional
  `owner-decisions.json` the operator keeps by hand (`{id, question, since}`).
- `accepted_work`: per subscription per ISO week, from the ledger — attempts `completed` and
  accepted, `external` rows with `accepted: true`, reviews with a `rejected` verdict that named a
  real finding — as `{week, account, accepted, attempts}` rows in the overlay.
- `deployments/capacity/cloud_server.py::clean_snapshot` accepts both lists with the same
  bounded-strings discipline as `accounts` (≤ 50 rows, ≤ 200 chars per string, no unknown keys);
  `capacity.py::project` carries them through.
- `deployments/capacity/web/app.js` renders a **Needs you** section above the provider cards
  (count in the summary strip; each row's kind, id, reason, age) and an **Accepted work** table
  (this week vs last, per subscription) in the Evidence section. Keep the existing theme tokens;
  no new dependencies.
- Tests: `test_cloud.py` for the cleaner (accepts, bounds, rejects unknown keys); the overlay
  function against a temp ledger and board; a static check that `app.js` references both keys.
- Size: medium.
- Operator step: redeploy `deployments/capacity` (`railway up`), as for every cloud change.

---

## Definition of done, per packet
One commit, `Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>`, one CONTRIBUTIONS row under
`## 2026-09-15 — brief 14`, `ruff format` and `ruff check` clean, the full suite green except the
pre-existing sandbox `killpg` failures (capture the failing-test list before you start and show it
is byte-identical after). Report in `docs/reports/glm-brief-14-<packet>.md`: what landed, what you
could not verify, and any defect found in existing code — including in provider-authored modules,
which you name rather than edit.
