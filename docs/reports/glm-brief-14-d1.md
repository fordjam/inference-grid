# Report — brief 14 D1, packet board task kind (interactive ZCode, GLM-5.3-Flash)

Branch `glm/d1-packet-task-kind` from `main` (5479de4), worktree
`~/.grid-workspaces/inference-grid-zai`. One commit. Not pushed; `glm/work` and `main`
untouched. No Phase A–C file was read for content or edited: the packet needed nothing
from `watch.py`, `verify_merge.py`, `policy.py`, `route.py`, `operator_queue.py` or
`deployments/local/`.

## What landed

- **`lanes/brief.py`** — `packet_text`, `hard_rules`, `mentioned_paths`, `compose_prompt`,
  `commit_gate_script` (+ the heading/path/trailer constants) moved verbatim out of
  `scripts/run_lane.py`, which now imports them and keeps working. `tests/test_run_lane.py`
  moved with the code and became `tests/test_lane_brief.py`, plus one test that the
  script still re-exports the moved functions.
- **`board/packet_task.py`** — the `packet` category:
  - `validate_board_task` routes packet tasks to `validate_packet_task` and everything
    else to the provider-authored `board/task.py::validate_task`, which is **not
    edited** (CONTRIBUTIONS confirms its "integrated unmodified" status, and its key set
    has no room for `spec` anyway). The shared key checks are reused by validating a copy
    whose category is one the module accepts; the packet shape — exactly
    `{brief, packet_id, gates, base, max_rounds?}`, `packet_id` a `#### <id>.` heading id,
    `spec.brief` equal to the task's `brief`, gate keys `{name, argv, cwd?, timeout?, env?}`
    with bounds — is checked here. `load_board`/`save_task` use the router.
  - `dispatch_packet`: admission **before** the loop (submit → claim → `start`'s
    workspace lease; the loop's native logs land inside the leased attempt directory,
    which is what the test asserts), then a scratch `--shared` clone of the project
    branched from `spec.base` inside the attempt directory — never the operator
    checkout — and `lanes/packet.py::build_loop` with the declared gates plus the
    generated commit gate (one trailered commit, clean tree). A bad `base` or an unusable
    brief refuses before anything is admitted, so the task stays ready for an operator
    fix instead of burning an attempt.
  - Settling: green gates → the branch is fetched into the project as
    `refs/heads/packet/<task-id>` and the attempt completes with the verdict as receipt
    (`verified_in_lane`, `repairs` = rounds − 1, artifact digest = sha256 of the head
    commit object — verifiable later via `git cat-file`); `record_outcome` carries the
    repairs. Anything else → held with reason `packet loop: <reason>`; the task blocks
    naming it and the branch stays in the attempt clone. The base branch is never
    advanced and no review task is spawned for a packet — the gates ran as code in the
    attempt.
- **Runner integration** — `dispatch` branches to `dispatch_packet` for packet tasks; the
  tick settles passed/held directly (no `run_tests`, no verify-follow-up: the loop already
  spent its bounded rounds, and its hold reason never matches the deadline-follow-up
  guard). Packet tasks may select unverified/unqualified lanes the way canaries do —
  nothing else could ever earn the qualification rows for a brand-new category — with the
  recently-model-refused exclusion preserved. `board-tick --dry-run` plans packet tasks
  like any other (tested).
- **Adapters** — `goat_cli` → `CommandCodeAdapter` with a high-effort module written
  *beside* the attempt, not into the worktree (an untracked file would trip the commit
  gate's clean-tree check; effort `high` follows `run_lane.py`'s packet precedent, not the
  one-shot goat lane's `low`). `zcode_cli` → new `ZcodeAdapter` in `lanes/packet.py`;
  flags relied on: `--prompt`, `--mode yolo`, `--json` (final stdout document carries
  `sessionId`), `--no-color`, `--cwd <worktree>`, resume via `--resume <sessionId>` — the
  invocation and its session-DB evidence are `lanes/zcode.py`'s; no model is passed (the
  CLI serves its login's model). `cline_cli` → refused before any attempt and registered
  in `UNSUPPORTED_ADAPTERS`: its packaged invocation (`lanes/cline.py`) has no
  session-resume flag — per-run state lives under `--data-dir` and no event names a
  session a later run could re-enter. Test says so.
- **Docs** — SPEC.md task contract gains the packet spec shape; BOARD.md gains the
  "Packet tasks" section (and the field table the `spec` row); LANES.md's `external`
  paragraph states `external` is now the fallback for work the board could not run.

## Verification

- Suite before any change: **428 passed, 7 skipped, 0 failed** (the sandbox `killpg`
  failures the brief warns about do not occur here — the affected tests skip). After:
  **438 passed, 7 skipped** — failing-test list byte-identical (empty), 10 tests added
  (`tests/test_packet_task.py` 8, `tests/test_lane_brief.py` moved + 1 re-export case,
  `test_packet_lane.py` +1).
- Lint, scoped honestly: the venv's ruff (0.16.7) *format*-disagrees with the whole
  pre-existing tree (36 of 74 files under `src tests scripts` on untouched `main`) and
  `calibration/corpus-v1/` fails `ruff check` by design — so "ruff clean" was applied
  without reformatting unrelated code: all four new files are `ruff format`-clean, my
  edits leave no new format findings in the touched regions, `ruff check src tests
  scripts` reports exactly the one pre-existing finding it reports on `main`
  (unused `uuid` in `tests/test_external_work.py`). Reformatting 39 drifted files or
  touching the corpus is a separate decision for the coordinator.

## Defects found, not fixed

- `board/runner.py::dispatch` builds `timeout` as `wall_seconds + 40`; a task at the
  budget ceiling (3600) submits 3640, which `ledger.submit` refuses (bounded 1..3600).
  Packet dispatch clamps to 3600; the artifact path does not. One-line fix in the
  coordinator's court.
- `deadline_hold` treats any ledger reason containing "timeout" as a wall-deadline hold;
  a transport refusal that merely mentions the word would trigger the verify-follow-up.
  The A2 item in `docs/handoff-glm.md` already tracks the stricter shape.
