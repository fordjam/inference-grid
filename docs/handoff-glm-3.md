# Handoff brief 3 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-2.md` (A1–A4, B1–B3 done 2026-09-13, `313e90f`…`415b391`; 273 tests).
**Section 1 of `docs/handoff-glm.md` applies unchanged** (no credentials, no ticks or dispatch, no
push, offline tests, provider-authored modules fixed at their callers).

Every item comes from the 2026-09-13 operator round: what had to be done by hand, and what has
never run for real.

---

## B — operating-model features

#### B1. `board-new --retry`: the authorised retry as one command
BOARD.md allows retries "only as new tasks with a recorded change". Today the operator did this by
hand three times: copy the task JSON to `<id>-N`, copy the brief, adjust lanes/budget/author
family, then block the predecessor with `superseded: …` (now machine-readable, handoff-2 A4).
Add to `board-new`: `--json {board_dir, project_root, retry: "<task-id>", change: "<why>",
budget: {...}?, lanes: [...]?, author_family: ...?}` which
- picks the next free suffix (`<id>-2`, `-3`, …; `review-lane-cline` → `review-lane-cline-2`),
- copies the predecessor's task with the overrides applied, state `ready`,
- copies the brief text verbatim to the new brief path (this is the one place `board-new` may
  write a non-empty brief), and for `independent_review` tasks keeps the review schema test,
- rewrites the predecessor's `blocked_reason` to `superseded: <new id> — <change>` and state
  `blocked` (the only edit `board-new` ever makes to an existing file; refuse if the predecessor
  is `accepted`, `passed` or `review_pending`),
- returns both paths.
The `change` string is mandatory; it is recorded only in the predecessor's reason. The new task
starts clean.
- Tests in `tests/test_board_new.py`: suffix selection with existing `-2`; brief copied; overrides
  applied; predecessor blocked with the exact reason; refusal on an accepted predecessor; an
  invalid override writes nothing.
- Size: medium.

#### B2. Offline end-to-end: the inbox landing has never run
`board/runner.py::land_in_inbox` (creates a `grid/inbox` worktree under `~/.grid-workspaces/inbox/`
and commits accepted artifacts) has never executed outside unit tests — the two acceptances today
were operator `accept` calls with no landing, and `scorecard-summary` will be the first real one.
Write `tests/test_board_end_to_end.py`: a temp project that is a real git repo with one commit, a
temp board with one `pure_function` task and a fake lane whose adapter writes the artifact, a temp
ledger; drive `tick` through work-pass → review task created → (fake) approved review → accept →
`land_in_inbox`, with `Path.home()` patched to a temp directory so the inbox worktree lands there.
Assert: the inbox branch exists in the project repo, its head commit contains the artifact at the
expected path and the `grid/inbox/<task-id>.json` record with the right attempt id, reviewer
family and receipt digest; the operator's working tree is untouched (`git status --porcelain`
empty); a second run does not create a second commit.
- If `land_in_inbox` turns out to need `HOME` rather than `Path.home()`, or shells out in a way
  the test cannot redirect, fix the seam (one injectable `home` argument) — that is in scope.
- Size: medium.

#### B3. Transport-timeout holds should say what they were bounded by
Both holds today block the task with `attempt … held; resolve with evidence`, and the operator
had to open `verdict.json` to learn it was a transport timeout at 400 s. The verdict now carries
`task_wall_seconds`, `lane_wall_seconds` and `transport_timeout` (handoff-2 A3). When the tick
blocks a task on a hold whose verdict has a `refusal` starting with `transport_error`, put the
refusal and the three bounds in `blocked_reason` — `attempt <id> held: transport_error:
TimeoutError (transport 400 s, task 600 s, lane 400 s); resolve with evidence` — and have
`board-status` show it. No automatic resolution; that is a policy question (tier 3).
- Tests in `tests/test_board_runner.py` with a fake lane verdict.
- Size: small.

#### B4. `board-status` reads held attempts' verdicts
Extend `board/status.py`: for a `blocked` task whose reason names an attempt id, add
`attempt: {state, refusal, transport_timeout, artifacts_present: bool}` read from the ledger row
and the attempt's `verdict.json` under the packet workspace when it exists. Read-only.
- Tests in `tests/test_board_status.py`.
- Size: small.

## Tier 3 — coordinator decision; propose in the report, do not implement
- Auto-resolving a transport-timeout hold with **no** artifacts as `consumed` (BOARD.md currently
  reserves resolution for the operator, with the verify-only follow-up as the single exception).
- Loading `local.inference-grid.board-tick.plist` in launchd.
- Re-dispatching `review-board-runner-3` and `review-scorecard-summary` with a 900 s budget once
  B1 exists (that is exactly the retry B1 automates).

## C — out of scope
Same as handoff 1: running boards or lanes, `lanes.json`, credentials, launchd, releases, pushes,
anything under `~/.grid-workspaces`.

---

## Definition of done, per item
1. Tests added or extended; whole suite passes; `.venv/bin/ruff check src tests` passes
   (`ruff format --check` has 10 pre-existing drift files; do not reformat files you did not
   otherwise touch).
2. `git status --short` shows only files under `src/`, `tests/`, `docs/`, `grid/`.
3. One commit per item, message explaining why, your own trailer.
4. A row in `docs/CONTRIBUTIONS.md`.

Report at the end: items done (commit hashes), items skipped and why, bugs found but not fixed.
