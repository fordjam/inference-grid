# DeepSeek-V4.1-Flash lane report — brief 20, M4: eval cases for review and packet kinds (2026-09-16)

Lane: DeepSeek-V4.1-Flash (packet lane). Base: `a8ce747` (the tip of `glm/work` at
branch time). Branch: `packet/packet-gm4`, one commit, not pushed.

## Why

`board/calibration.py` measured reviewers only: a corpus case was a seeded-defect review
packet, and a score was recall, false positives and accepted = every defect recalled with
none spurious. The number the whole routing blend rests on (route's Laplace score is
`0.5 * acceptance + 0.5 * recall`) therefore said nothing about the lanes that *build*.
M4 generalises the case and the scoring to two kinds without touching the CLI, so M5's
nightly `evals` can score a lane on a packet it was asked to build, through the same
outcome path.

## What landed

**`src/inference_grid/board/calibration/` — the old module becomes a package.**

- `case.py::load_case(dir)` — one case, one kind. `case.json` (`{"kind": "review"}` or
  `{"kind": "packet"}`) declares it; absence means review, so the operator's private
  corpus (written before the key existed) loads byte-for-byte as before. A review case is
  exactly today's shape (brief.txt, diff.patch, answer.json plus the changed files) and
  `case.json` is never staged; a packet case holds `brief.txt`, `base/` (the repository the
  lane starts from) and hidden `reference/` (`reference.patch`, the reference fix, and
  `reference_tests/`, the reference suite, whose paths are repo-relative so the lane's diff
  is judged on the paths the suite runs at). `load_corpus` loads both kinds.
- `score.py::score(case, attempt_dir)` — one record for both kinds,
  `{kind, accepted, recalled, false_positives, repairs, notes}`. Review is today's
  arithmetic (`_score_findings` moved here, unchanged) against `artifacts/reply.txt`;
  packet copies every reference test over the lane's tree at `attempt_dir/work` and runs
  each file on its own, accepted when all pass **and** the lane's diff touches none of
  them, `repairs` counting the files that fail. The reference-test runner is an injected
  seam (`(argv, cwd, env) -> exit code`), the real subprocess by default.
- `score.py::record_outcome(ledger, attempt, record)` — writes one outcome through the
  existing path, category `eval:<kind>`, so the scorecard row is
  `(family, model, eval:review | eval:packet)` and `repairs` rides the ledger's own field.
- `__init__.py` — everything else (`author_calibration`, `calibration_task`,
  `calibration_settled`, `score_calibration`, the run validator, the ledger clock), all
  re-exported so every existing import (`from .calibration import …`,
  `from .board.calibration import …`, `from inference_grid.board import calibration`)
  keeps working. `author_calibration` authors the review cases and reports a packet case as
  `skipped` (authoring it is M5's evals path); `score_calibration(record=True)` now records
  `eval:review` through `record_outcome`; `is_calibration_outcome` accepts `eval:<kind>`
  and the legacy `calibration`.

**`src/inference_grid/board/runner.py`** — `calibration_reports` aggregates by
`is_calibration_outcome`, so a lane's recall blend counts both a review's `eval:review`
and a packet's `eval:packet` outcome (and the legacy `calibration` rows already in a
ledger).

**`calibration/example/`** — the two review cases gain their `{"kind": "review"}` marker,
and one packet case joins them: `rolling-mean-off-by-one` — a ~30-line metrics module
whose trailing-window mean drops the eviction one index too late (`>` for `>=`), with a
five-file reference suite (sliding window, short input, empty input, window of one,
exact-window). The un-fixed base fails two of the five; the reference fix passes all five.
The operator's private corpus is untouched.

## Tests

`tests/test_eval_cases.py` (16 offline cases; the packet suite runs real tiny Python files,
no lane, no network):

- loading: a review case without a marker defaults to review; a marker is accepted and
  `case.json` is never staged; an unknown/malformed marker refuses; a packet case holds
  `base/` and the hidden `reference/` and never leaks reference paths into `base`; a packet
  case missing `reference.patch` refuses; the example corpus holds both kinds with a
  five-test packet suite.
- packet scoring: a correct fix is **accepted** (5/5, repairs 0); the un-fixed base fails
  the suite (repairs 2); a partial fix counts its repairs (repairs 1); a fix that edits a
  reference test is **rejected** (`false_positives` 1) even though the overlay still
  passes that file; a missing lane tree is not accepted; the runner seam is injectable.
- regression pin: for both example review cases the `score()` record's
  `accepted`/`recalled`/`false_positives` are **identical** to `score_calibration`'s row.
- the outcome path: `record_outcome` writes `eval:review` / `eval:packet` rows (and their
  `repairs`), and `calibration_reports` counts them.

`tests/test_calibration.py` — the example authoring case now asserts three cases (two
review authored, the packet case `skipped`) and the recorded category is `eval:review`.
The rest of the file is untouched.

## Verification

- Full suite with `--continue-on-collection-errors`: **88 failed, 583 passed, 7 skipped,
  1 error**. The 88 `FAILED` node ids and the `tests/test_calibration.py` deny-read
  collection error are **byte-identical** to a scratch `git worktree` at the base
  `a8ce747` (`diff` empty): the pre-existing sandbox refusals
  (`killpg`/`/bin/ps`/`sandbox-exec` denials) and the deny-read collection error, zero
  new, zero repaired. Passes 567 → 583 (the 16 new eval cases).
- `ruff format --check` and `ruff check` clean on every changed file.
- No provider-authored file was touched (`observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
  `lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py` all
  unchanged). No new credential access; no absolute home path or e-mail in the diff.

## One thing worth the operator's eye

**A packet case is authored by no one yet.** `author_calibration` (the `calibrate`
command) authors review cases only and names a packet case in `skipped`, because a packet
eval needs a real repository at `base` and the `evals` authoring path in M5. Until M5
lands, the shipped packet case is exercised by `load_case`/`score` and the suite, not by a
board run. `score()` and `record_outcome` are ready for the runner's `calibration_run`
step to call, which is exactly what M5 wires.
