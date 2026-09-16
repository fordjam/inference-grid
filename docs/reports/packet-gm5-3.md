# DeepSeek-V4.1-Flash lane report — brief 20, M5: `inference-grid evals`, the nightly run (2026-09-16)

Lane: DeepSeek-V4.1-Flash (packet lane). Base: `7bf3a3b` (the tip of `glm/work` at branch
time). Branch: `packet/packet-gm5-3`, one commit, not pushed.

## Why

M4 generalised the eval *case* and the *scoring* to review and packet kinds, but a
measurement that only happens when the operator remembers to run `calibrate` is a snapshot,
not evidence. `route`'s blend — `0.5 * acceptance + 0.5 * recall` — is only as current as
the newest `eval:*` row behind it. M5 makes the measurement routine: a command that authors
one `calibration_run` task per (lane, case) whose kind is stale, a launchd agent that runs it
hourly, and a digest section that says per lane and kind what the grid actually knows and
which lanes have gone dark.

## What landed

**`src/inference_grid/board/evals.py`** (new) — the due-set, the authoring and the summary:

- `eval_rows(ledger)` reads the ledger's `outcome_recorded` events whose category is
  `eval:<kind>`, joins each to its attempt's task spec, and returns per
  `(family, model, kind)`: `accepted`, `cases` and `newest_at`. This is the scorecard's own
  identity plus the clock the scorecard does not carry (the scorecard aggregates
  accepted/cases but stamps no time), so the due-check and the digest read the same rows M4
  writes. An unknown `eval:<whatever>` is not an eval row.
- `due_evals(corpus_dir, lanes, ledger, now, every_days)` returns one entry per
  `(lane, case)` whose **kind** is not fresh for that lane: no result ever, or the newest
  older than the window (7 days). Freshness is per (lane, kind) — `eval:<kind>` is the row
  identity M4 records — so a kind that is stale authors a run for every case of that kind,
  and a lane the operator has just configured authors its whole corpus. A new lane authors
  everything; fresh rows skip.
- `author_evals(...)` calls `calibration.calibration_task(..., case=...)` for each due entry.
- `eval_summary(ledger, lanes, now, every_days)` is per lane, per kind: accepted / cases and
  the newest result's age, plus the `needs_you` lanes (no result inside the window, with the
  age or "never").
- `eval_markdown` renders the table; `lanes_from_configs` finds the lane set from the first
  board config that names a readable `lanes_path` (the digest reads boards, not the operator's
  configuration; an absent file leaves the section lane-less, never an error).

**`src/inference_grid/board/calibration/__init__.py`** — `calibration_run` gains a case:

- `validate_calibration_run_task` accepts an optional `spec.case` (`[a-z0-9-]`, exactly one
  lane named); `calibration_task(..., case=None)` carries it and writes a brief that says so.
  Without `case` the whole-corpus run is unchanged.
- `case_task_id(run_id, case)` is the single-case board task's id (`eval-<run>-<case>`,
  bounded at 60 chars by digesting a long case name); `author_review_case` stages one review
  case exactly as `author_calibration` stages it (the same `_plan_task` path, so the task is
  the same shape); `case_attempt(ledger, task_id)` finds the ledger attempt an outcome
  attaches to and `case_attempt_dir(packets_root, task_id, aid)` locates the attempt
  directory under the runner's layout.

**`src/inference_grid/board/runner.py`** — `step_calibration_run` branches on the case:

- `step_eval_case` runs one M5 single-case eval per pass. A **review** case is authored as an
  ordinary `independent_review` task on its single lane (`author_review_case`), the run waits
  in `review_pending`, and once the case task settles the run scores it through
  `board/calibration/score.py::score` and records one `eval:review` outcome on the ledger
  attempt. A **packet** case is not a review packet: the run materializes a one-commit git
  repository of the case's `base/` (`materialize_case_repo`, with a brief document carrying
  the case's own brief as its packet section), builds an in-memory, validated `packet` task
  (`case_packet_task` — one compile gate, no declared tests, so **no reference test ever
  reaches the lane's context**), and dispatches it through the runner's own packet path with
  `project_root` = that repository. `score` then grades the lane's tree and the run records
  `eval:packet` and settles `passed` with the accepted/repairs figures. A held attempt is
  ACTIVE and no outcome can attach, so the run blocks naming it for the operator.
- The tick passes `lanes`, `lanes_path` and `accounts_by_lane` into the step, and the step
  takes an injectable `dispatch_fn` (the real `dispatch`), so the packet path is exercised in
  tests without a sandbox or a lane.

**`src/inference_grid/digest.py`** — an *Evals* section (per lane, per kind, accepted / cases
and the newest result's age) and a *Needs you* section that lists a lane with no eval inside
the window. Both are driven by the board configs' `lanes_path`; with none, the Evals section
says so instead of guessing.

**`src/inference_grid/cli.py`** — `inference-grid evals --json {corpus_dir, lanes, board_dir,
project_root}` (`lanes` is the lanes.json path; `project_root` is the board's project, where
the runner stages a review case). Idempotent per day and per (lane, case), so an hourly job is
safe.

**`deployments/local/install.py`** — renders a `com.inference-grid.evals` KeepAlive plist from
the same template as the capacity runtimes (`KeepAlive`/`RunAtLoad` true, `ProcessType:
Interactive`, the same `ExitTimeOut`, never `StartInterval`), whose program is
`deployments/local/evals_run.py` and whose arguments name the operator's config and, when
known, the corpus from its `evals_corpus`. `RUNTIMES` is unchanged (it is the capacity layer's
four runtimes), so the evals agent is rendered beside them.

**`deployments/local/evals_run.py`** (new) — the hourly loop: `inference-grid evals` once per
`EVAL_SECONDS` (3600) against the config's paths, then a sleep; the call happens before the
sleep, so a launchd restart does not delay it by an hour. An incomplete config is a logged
skip, not a crash; a SIGTERM ends the loop after the call in flight.

## Tests

`tests/test_evals.py` (14 offline cases):

- **the due-set**: a new lane authors every case (one run per (lane, case), each carrying the
  case and its single lane, validating as a `calibration_run`); a fresh review row skips both
  review cases while the packet case is still due, a stale row authors again, and both kinds
  fresh authors nothing; a second lane is judged on its own rows; authoring the same day twice
  returns the existing task.
- `eval_rows` aggregates accepted/cases/newest and ignores a non-eval outcome and an unknown
  `eval:quiz` category; `eval_summary` reports the per-kind table and the needs-you reasons
  ("no eval ever recorded" / "no eval in 7 days"); `case_task_id` stays legal and bounded; a
  case run must name exactly one lane while a whole-corpus run still takes the plural list.
- **the step, review**: `authored` then `waiting` while the case task is open, then `passed`
  with exactly one recorded `eval:review` outcome (accepted) once the lane's reply settles.
- **the step, packet**: the fake dispatch sees a repository holding the case's `base` and
  never `reference/`, a valid packet task (one gate, `packet_id E1`, no tests), and the run
  records one accepted `eval:packet`; a held attempt blocks the run instead.
- the CLI round-trips through `main()`.

`tests/test_digest.py` (+1): the Evals table per lane and kind with the newest age, and the
needs-you line for the lane with no eval.

`tests/test_local_collectors.py` (+5): the evals plist renders KeepAlive/RunAtLoad/Interactive
with the config and the corpus it names in `ProgramArguments` and no `StartInterval`; the
`--evals-corpus` flag overrides the config; `evals_run.eval_payload` names every path from the
config (or nothing when incomplete); the loop calls once per interval and stops at its
deadline; a default with an incomplete config logs a skip and spawns nothing.

## Judgment calls and defects worth recording

- **The due-check is per (lane, kind), the authoring is per (lane, case).** `eval:<kind>` is
  the row identity M4 writes and the scorecard aggregates, and it carries no case id, so
  "has an outcome for (lane, case)" can only be answered at kind granularity without parsing
  notes. One case of a kind therefore keeps that kind current for the lane, and a stale kind
  authors a run for every case of that kind. This is the reading M4's `record_outcome`
  docstring names ("the identity the routing evidence and M5's nightly due-check read"); if
  the operator wants per-case freshness, the outcome note already carries
  `calibration <run>/<case>` and a future packet can key on it.
- **A packet eval dispatches its own build, synchronously, from the calibration node.** The
  board's packet machinery is bound to the board's `project_root`, and a case's `base/` is not
  a ref in the operator's repository, so the run materializes the case as its own one-commit
  repository and calls `dispatch` with that as the project root. The build therefore blocks
  the tick pass that runs it, where an ordinary packet attempt runs in a J6 worker. That is a
  real trade-off: the calibration node is already described as a code node the tick steps
  through, and a packet eval is inherently a build, but an operator with a slow packet lane
  will see one tick pass held for it. The alternative — authoring a packet board task — cannot
  express the case's base without writing a ref into the operator's project.
- **Every lane is measured on every case of a due kind.** A corpus with several packet cases
  makes the first run author all of them at once for a lane. That is the intent ("a new lane
  authors everything") and keeps the run count bounded by the corpus, not by the clock.
- **The digest reads lanes from the board configs.** The digest is a read over `boards_dir`;
  it has no other road to the operator's lane set. A board config that names a readable
  `lanes_path` supplies it; with none, the section says so. An unreadable file is skipped,
  never fatal (the digest's standing rule).
- **`evals_run.py` is a loop, not a `StartInterval`.** launchd parks interval spawns for a GUI
  agent while the display is off — the note `install.py` already carries — so the hourly
  cadence is the agent's own loop, kept alive the same way the capacity runtimes are. The
  first call happens at load.
- **No new credential access.** The packet build's git identity is `evals`/`evals` (a
  synthetic name, no address); no home path, credential path or e-mail is added to code, tests
  or docs.

## Final gate

- Full suite with `--continue-on-collection-errors`: **88 failed, 613 passed, 7 skipped,
  1 error**. The 89 `FAILED`/`ERROR` node ids are **byte-identical** to a scratch `git
  worktree` at the base `7bf3a3b` (`diff` empty in both directions): the pre-existing sandbox
  refusals (`killpg`/`/bin/ps`/`sandbox-exec` denials) and the `tests/test_calibration.py`
  deny-read collection error, zero new, zero repaired. Passes 593 → 613 (the twenty new
  cases).
- `ruff format --check` and `ruff check` clean on every changed file.
- No provider-authored file was touched (`observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
  `lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py` all unchanged).
