# Handoff brief — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Scope: engineering work on this repository that is offline, verifiable with
`~/.local/share/inference-grid/venv/bin/python -m pytest -q` (or the system
`python3.12 -m pytest -q`), and touches no credentials or private data. Read
section 1 before doing anything.

---

## 1. Hard rules

### Never read or edit
- `~/.config/inference-grid/` (credential files, `lanes.json`, quota stamps, `deny-read.json`).
- `~/.local/share/opencode/auth.json`, `~/.commandcode/auth.json`, `~/.zcode/`, any `.env*`.
- `~/.grid-workspaces/` and `~/.local/share/inference-grid/board.sqlite` — live packets and the
  durable ledger the board runner is using. Look, don't touch.
- Personal project data anywhere (`~/insta-saved/data`, `saved.db`, `~/projects/pyprojects/monarch` data files).

### Never run
- `inference-grid board-tick`, `board-tick-all.py`, any `dispatch.py` — they spend subscription quota
  and race the running coordinator. Unit tests only.
- `git push`, tags, releases. Commit locally on `main`; the coordinator pushes after CI.
- Anything that installs packages system-wide or edits launchd.

### Never write
- Credentials, tokens, absolute home paths or e-mail addresses into code, tests, docs or commit
  messages. The only credential access in this package is through `lane["credential_path"]` inside
  `src/inference_grid/lanes/*.py`; do not add another.
- Provider-authored files marked "integrated unmodified" (`observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
  `lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py`) — fix callers instead,
  or write a new module. If one of them is wrong, say so in the report.

### Tests must stay offline
No test may open a network connection. Lane tests use fake CLIs, injected `send` functions and fake
session databases (see `tests/test_lane_zcode.py`, `tests/test_lane_go.py`). macOS-only tests skip
at module level when `/usr/bin/sandbox-exec` is absent.

---

## 2. Orientation

**What this is.** A ledger (`ledger.py`) that admits bounded provider attempts, five packaged
provider lanes (`lanes/`), and a board runner (`board/`) that dispatches tasks, runs their tests,
creates cross-family review tasks, accepts approved work and lands it on a `grid/inbox` branch.
Docs: `docs/BOARD.md` (operating model), `docs/SPEC.md` (task contract, state machine),
`docs/CONTRIBUTIONS.md` (every provider attempt, accepted or not), `docs/EVALUATION.md` (scorecard).

**Conventions.** Match surrounding code. `ruff format` and `ruff check` (config in
`pyproject.toml`) must pass. One commit per item, message explaining why, trailer naming the model
that produced it, e.g. `Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>`. Update `docs/CONTRIBUTIONS.md`
with one row per item you complete, under a new heading with today's date.

**Run tests.**

```
~/.local/share/inference-grid/venv/bin/python -m pytest -q
```

---

## 3. Open items (A first; each has a location, an acceptance test and a size)

### A — defects found by independent reviews, not yet fixed

#### A1. `parse_review` crashes on a fence without a newline
`src/inference_grid/board/runner.py::parse_review`: a reply of exactly "```json{...}```" (no newline
after the fence) makes `split("\n", 1)[1]` raise `IndexError`, which `tick` does not catch, so the
whole tick aborts with the review task left `dispatched`. Found by the Kimi review of the runner.
- Make `parse_review` tolerant of fences with or without newlines and raise `ValueError` for
  anything that is not a verdict object; make `tick` treat any exception from `parse_review` as
  `review_unreadable`.
- Tests: fenced-with-newline, fenced-without-newline, bare object, prose → `ValueError`.
- Size: small.

#### A2. Verify follow-up fires for any hold, not only wall-deadline holds
`tick`'s follow-up branch checks `state != "completed"` and file presence only. A hold caused by a
receipt refusal (unexpected model, escaping artifact name) with files present would be re-run.
Restrict the follow-up to holds whose ledger reason contains `timeout` (the worker's deadline hold
reads `Refused: timeout: provider acceptance may be ambiguous`) or whose lane verdict says
`wall_deadline`; read the reason from `ledger.status()` for the attempt.
- Test: a fake adapter that writes the files and exits 1 immediately must *not* get a follow-up;
  the existing deadline-shaped test must still pass.
- Size: small.

#### A3. Artifact-vs-input basename collision is not flagged
`shadowing_names` checks artifact↔test and test↔input, not artifact↔input. In flat tasks an artifact
named like a staged input silently replaces it in the scratch directory.
- Add the check with the same message style; test it.
- Size: trivial.

#### A4. Go lane HTTP timeout ignores the lane budget
`src/inference_grid/lanes/go.py::run` uses `timeout=150` regardless of `lane["wall_seconds"]`;
Kimi reviews at 16k output tokens exceed it (`transport_error: TimeoutError`). Use
`min(lane["wall_seconds"], 600)` as the transport timeout and record it in the verdict.
- Test with an injected `send` that asserts the timeout it receives.
- Size: trivial.

#### A5. Rejected review findings never reach the source task
When a review is rejected, `tick` blocks the *review* task with "review rejected with N finding(s)"
but the source task stays `review_pending` with no findings attached. Write the findings summary
(location + observed, first three) into the source task's `blocked_reason` and set it `blocked`.
- Test: extend `test_rejected_review_blocks_the_task`.
- Size: small.

### B — features the operating model still lacks

#### B1. `select_lane` should respect `max_concurrency`
Lanes carry `max_concurrency` in `lanes.json` but selection ignores it, so a busy account yields
`refused: account busy` per task instead of skipping to another lane or reporting `lane_busy`.
Count active ledger attempts per account in `readiness_view` (state in queued/dispatching) and mark
the lane `busy` when at or over `max_concurrency`; `select_lane` treats `busy` as not ready and
`tick` reports `lane_busy`. Do not modify `lanes/select.py` (provider-authored); adapt inputs.
- Tests on the runner with two ready tasks and one lane of concurrency 1.
- Size: medium.

#### B2. Doctor should show boards and lanes
`src/inference_grid/doctor.py`: add `boards` (per board dir passed via `--json`: counts by task
state) next to the existing `lanes` classification. Read-only, no dispatch.
- Tests in `tests/test_doctor.py`.
- Size: small.

#### B3. Dashboard feeds for attempts and scorecard
`deployments/capacity/cloud_server.py` and `src/inference_grid/capacity.py`: accept an overlay key
`scorecard` (list from `ledger.scorecard()`) and render an "Evidence" section in both `app.js`
copies: rows per family/model/category with attempts, accepted and acceptance rate. Data only from
the overlay; no ledger access from the server.
- Tests: `deployments/capacity/test_display.cjs` and `test_cloud.py` (upload sanitization must strip
  unknown keys from scorecard rows).
- Size: medium.

#### B4. Board task authoring helper
`inference-grid board-new --json {...}`: writes a validated task file plus an empty brief and, for
`independent_review`, the schema test, refusing to overwrite. Pure file work; tests on a tmp board.
- Size: small.

#### B5. insta-saved board files
Write `~/insta-saved/grid/board/*.json`, briefs and no tests of your own (the repo's
`tests/test_pipeline.py` is the acceptance test) for handoff items A1–A6 in
`~/insta-saved/docs/handoff-glm-flash.md`, as *tree tasks*: inputs are the `igsaved/` package files,
`scripts/*.py` where relevant, `tests/test_pipeline.py`, `pyproject.toml`; artifacts are the files
each item changes at their real paths; lanes `["zcode"]`. Validate each with
`inference_grid.board.task.validate_task`. Do not read anything under `~/insta-saved/data`,
`saved.db` or `ig-archive/`.
- Size: medium (mostly writing briefs precisely).

### C — out of scope
Running boards or lanes, editing `lanes.json` or any credential, changing provider selection
policy, releases, launchd, anything under `~/.grid-workspaces`.

---

## 4. Definition of done, per item
1. Tests added or extended; whole suite passes; lint passes.
2. `git status --short` shows only files under `src/`, `tests/`, `docs/`, `deployments/`, `grid/`.
3. One commit per item, message explaining why, your own trailer.
4. A row in `docs/CONTRIBUTIONS.md`.

Report at the end: items done (commit hashes), items skipped and why, bugs found but not fixed.
