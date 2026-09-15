# GLM lane report — brief 14, I3: calibration as a routine, not a one-off (2026-09-15)

Lane: GLM-5.3-Flash via Command Code. Base: `glm/work` at `bd159c7`.
Branch: `packet/packet-i3`, one commit, not pushed.

## Why

The calibration corpus existed and could be scored, but only by an operator running
`inference-grid calibrate` then `calibration-score` by hand — one measurement, whenever
someone remembered. Recall is the number that says what an approval is worth, and the
route blend already consumes it (`calibration_reports`); nothing kept it current. The
brief: make the run a board task the runner steps through, trigger it weekly from the
ledger's own state, and put the newest recall per lane in front of the operator beside
accepted work.

## What landed

`src/inference_grid/board/calibration.py`:

- `calibration_task(board_dir, corpus_dir, lanes, run_id)` writes one board task with
  `category: "calibration_run"`, `spec {corpus_dir, lanes, run_id}`, empty
  `inputs`/`tests`/`artifacts` (it is code, not work) and a documentation brief under
  `grid/briefs/`. Re-authoring the same `run_id` returns the existing task instead of
  refusing — the weekly trigger may fire again before the run has been scored.
- `validate_calibration_run_task(raw)` mirrors `validate_verify_merge_task`: the shared
  checks run on a copy whose category `board/task.py` accepts (provider-authored file
  untouched), and every `spec` key, the run id charset and the lane list are checked here.
  `packet_task.validate_board_task` dispatches to it.
- `calibration_settled(board_dir, run_id)` names the per-case review tasks that have not
  reached `passed`/`blocked`; a missing manifest is never "settled".
- `newest_calibration_at(ledger)` reads the newest `outcome_recorded` event whose detail
  category is `calibration` — the ledger's own timestamps are the weekly clock.
- `score_calibration` gained an optional `now` and stamps `scored_at` (ISO 8601 UTC) into
  `report.json`, so the overlay can order runs.

`src/inference_grid/board/runner.py`:

- `step_calibration_run(...)` drives the node one pass: `ready` authors the per-case
  reviews and settles `review_pending`; `review_pending` waits until every case task has
  settled, then scores (`record=True`) and settles `passed` with the per-lane
  recall/precision in the result. A bad corpus or a scoring error blocks the run with the
  reason, never the tick. No lane, no ledger attempt, no quota.
- The tick loop reads `calibration_run` in `ready` **or** `review_pending` before the
  ready gate; a dry run plans it without authoring or scoring.

`deployments/local/tick_boards.py`:

- `calibration_due(newest_at, every_days, now)` is the pure window test (None = never
  scored = due). `default_calibrate(config, now=None)` builds the ledger from
  `database_url`, reads `newest_calibration_at`, resolves the board (`calibration.board_dir`,
  else the config's, else the first named board with one), and authors a dated
  `auto-<YYYYMMDD>` run. `run(..., calibrate=)` fires the trigger once per pass, so a pass
  with a run in flight authors nothing. The config block is
  `calibration: {board_dir, corpus_dir, lanes, every_days}` (`board_dir` optional because
  the tick loop normally knows only board *names*).

`src/inference_grid/operator_queue.py`:

- `reviewer_recall(board_dirs)` reads every `<board>/calibration/*/report.json` and keeps
  the newest `scored_at` per lane as `{run_id, lane, recall, precision, scored_at}`;
  `build_overlay` returns it beside `operator`/`accepted_work`.

`capacity` (both the packaged copy and `deployments/capacity/capacity.py`) and
`cloud_server.py`:

- `clean_reviewer_recall` strips unknown keys, requires run and lane, and keeps the fixed
  row shape with rates as fractions in 0..1 or null. `capacity.project` carries the list;
  `cloud_server.clean_snapshot` validates the exact five-key shape, bounded strings and
  rates, and stores the cleaned list; the Store default gains an empty
  `reviewer_recall`.

Dashboards (`src/inference_grid/capacity_web/` and `deployments/capacity/web/`, `app.js`
and `index.html`): the Evidence section gains a **Reviewer recall** block beside Accepted
work, one row per lane (`recall · precision`, run id and how long ago it was scored).

## Tests

All offline; no test opens a network connection, and every new test uses temp ledgers and
temp boards.

- `tests/test_calibration.py`: the run task validates through `validate_board_task` and is
  idempotent per `run_id`; the validator refuses a missing key, an unknown key, a bad run
  id, empty lanes and an empty corpus dir; the **state machine across three ticks**
  (`authored` → `waiting on 1 case(s)` → `passed` with `{"go": {"recall": 1.0, "precision":
  1.0, "cases": 1}}`, report written, ledger still empty); `newest_calibration_at` ignores
  non-calibration outcomes; `calibration_settled` waits on an open case and a missing
  manifest.
- `tests/test_local_collectors.py`: the weekly trigger with a fake clock
  (`calibration_due` boundaries: None, 6 d, 7 d, 30 d), the trigger fires before every
  board pass, and `default_calibrate` authors against a real temp ledger then stops once a
  calibration outcome closes the window.
- `tests/test_operator_queue.py`: the newest run per lane wins and a second lane keeps its
  own; `build_overlay` carries the list; `capacity.project` cleans it; both dashboards
  reference `snapshot.reviewer_recall` and the `reviewerrecall` Evidence block.
- `deployments/capacity/test_cloud.py`: the cloud cleaner accepts the fixed shape (null
  rates included) and rejects junk, oversized lists, a missing key, a rate above 1, a
  bool, an empty run id and an unknown key.

## Defects found in existing code

None. Two scoping notes worth recording:

- The local upload path (`deployments/local/upload.py::build_body`) sends only `accounts`;
  it does not forward `operator`, `accepted_work`, `scorecard` or the new
  `reviewer_recall` to the cloud. That gap predates this packet (C2 added `operator` /
  `accepted_work` to `clean_snapshot` without teaching `build_body` to send them), so the
  cloud cleaner accepts a list that only a direct `POST /api/snapshot` would carry today.
  The packet asked for the value to be carried *through clean_snapshot*, which it is;
  teaching the upload to forward the sanitized lists is left for a follow-up.
- The tick-boards config names boards, not board dirs, so the calibration block needs its
  own `board_dir`. It falls back to the config's `board_dir` and then to the first board
  entry that carries one, so a config written either way works.

## Final gate

- Full suite before the change (`bd159c7`, same flags and sandbox):
  **88 failed, 509 passed, 7 skipped, 1 collection error**; after: **88 failed, 514
  passed, 7 skipped, 1 collection error** (+5 collected tests, the two trigger + two
  overlay + one cloud-family test set that can be reached). The sorted `FAILED`/`ERROR`
  set is byte-identical to the base (`diff` clean). The collection error is
  `tests/test_calibration.py` hitting the lane's deny-read on the private calibration
  corpus at module import — environmental, present at the base; the six new calibration
  tests therefore do not collect here and were run from a filtered copy of the module
  with the private-corpus test removed: **26 passed** (I2 used the same workaround). The
  88 inherited failures are the sandbox `killpg` refusals the earlier lane reports name.
- The cloud-family tests (`deployments/capacity/test_cloud.py`, outside `testpaths`) were
  run directly: **25 passed**, including the two new recall cases.
- `ruff format`/`ruff format --check` and `ruff check` on the 13 changed Python files:
  clean. As noted in earlier reports, a bare whole-tree `ruff format` still reformats 36
  files including provider-authored modules and `ruff check` reports 42 pre-existing
  errors elsewhere; that churn was not touched, and the packet's rule is honored on the
  changed files, the same reading the earlier lane reports use.
- No provider-authored file was touched (`board/task.py`, `lanes/select.py`,
  `lanes/config.py`, `lanes/sandbox.py`, `observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`).
