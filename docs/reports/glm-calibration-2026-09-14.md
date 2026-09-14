# GLM lane report — reviewer calibration (2026-09-14)

Lane: glm (glm-5.3-flash via Command Code). Base: `glm/work` at `78e63a8`.
Branches, one packet each, one commit each, all fast-forwarded onto `glm/work`:

| Packet | Branch | Commit | State |
| --- | --- | --- | --- |
| K1 corpus format and loader | `glm/k1-calibration-corpus` | `08384e7` | done |
| K2 authoring calibration tasks | `glm/k2-calibration-authoring` | `1ff5fa4` | done |
| K3 scoring | `glm/k3-calibration-scoring` | `abfeb0e` | done |
| K4 seed corpus and CLI | `glm/k4-calibration-seed-cli` | `175afb7` | done |

No packet skipped or blocked. Started 21:15 UTC, all four packets committed by 21:37 UTC.

## What landed

- `src/inference_grid/board/calibration.py` — `load_corpus` (validates every case with
  the board's own `check_input`/`check_name` guard, refuses an answer naming a file the
  diff does not touch, never stages `answer.json`), `author_calibration` (writes the
  per-case `independent_review` tasks through `branch_review._plan_task`/`_write_files`
  — no duplicated task-writing path — with `author_family: "calibration"`, a sentinel no
  lane declares, so every listed lane stays eligible under `select_lane`'s family
  exclusion), and `score_calibration` (reads the settled `reply.txt` through the
  runner's own `parse_review`; a defect is recalled when a finding names its file —
  relative path or basename — and carries every `must_mention` keyword
  case-insensitively; unmatched findings are false positives; reports per-lane cases,
  recall, false positives, precision and severity-weighted recall with high=3, medium=2,
  low=1; writes `report.json` and prints markdown tables; touches the ledger only with
  `record=True`, one `record_outcome` per attempt, category `calibration`).
- `calibration/corpus-v1/` — five hand-authored cases: the Playwright mock matching
  `/api/views` exactly while the client PUTs to `/api/views/<name>`; the DELETE branch
  nested under the `path == LIST_PATH` condition; the report claiming fourteen sites
  with a list of thirteen; the abatement base applied to the gross price where the
  docstring excludes grant-funded cost; and a clean case (a correct normaliser with
  tests) to measure false positives.
- CLI: `calibrate --json {board_dir, project_root, corpus_dir, lanes, run_id}` and
  `calibration-score --json {board_dir, run_id, packets_root, record}`, wired in
  `cli.py` the way `board-new` and `external` are; documented in `docs/BOARD.md`
  ("Reviewer calibration") and `docs/LANES.md` under the evidence discussion.
- `tests/test_calibration.py` — 20 tests: corpus loading and refusals, answer keys
  outside the staged inputs, run-id coexistence, the family sentinel through
  `select_lane`, scoring arithmetic (full recall, a miss, a false positive, a clean pass,
  a clean case with a spurious finding, weighted recall), the `record` path against a
  real ledger, the seed corpus loading and authoring 5 tasks, and both CLI commands
  round-tripped through `main()`.

## Defects found in existing code

None. Everything the packets reused behaved as documented: `branch_review._plan_task`
plans-before-writes cleanly and was reusable without modification; `parse_review`
tolerates the fenced replies as advertised; `validate_task` accepted every authored id
and budget unchanged. One environmental note, not a defect: the sandbox this lane ran in
cannot `killpg` child process groups, so 80 process-spawning tests
(`test_board_runner`, `test_grid`, the lane tests, `test_service`,
`test_board_end_to_end`) fail identically before and after this work — the pre-existing
failure list was captured at `78e63a8` and the failure set after all four packets is
byte-identical to it.

## Final gate

- `pytest tests/ -q`: **80 failed, 349 passed, 3 skipped** — the 80 failures are the
  baseline set (environmental `PermissionError` on `os.killpg`, present at `78e63a8`
  before any change); 349 passed includes the 20 new calibration tests and 329 baseline
  passes. No new failures.
- `uvx ruff check src/inference_grid/board/calibration.py src/inference_grid/cli.py
  tests/test_calibration.py`: clean.
- The harness-deselected `tests/test_lane_zai.py::FirstPartyClaudeTests::
  test_thinking_budget_and_first_party_login` was not touched.

## Next lane

The corpus is unmeasured: the next step is dispatching `calib-*` tasks to the reviewer
lanes (one run per lane, `run_id` naming the lane) and scoring — the routing change
itself (selection on recall rather than well-formedness) is not built here.
