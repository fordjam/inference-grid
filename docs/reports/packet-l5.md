# GLM-5.3-Flash lane report — packet L5: the attempt wall grows with the packet's gates (2026-09-16)

Lane: GLM-5.3-Flash (packet lane). Base: `53d5a2f` (the tip of `glm/work` at branch time).
Branch: `packet/packet-l5`, one commit, not pushed.

## Why

A packet attempt's wall was two numbers that did not add up. `admit_packet` submitted the
ledger spec with `timeout: min(wall_seconds + 40, 3600)`, and `run_packet` handed
`build_loop` the bare `wall_seconds` budget — but the loop's rounds run the declared gates
*and* re-enter the agent after every failing round. The gates' own timeouts and the
re-entry overhead were therefore charged to the agent's hour: a packet whose budget was
3600 s with two 600 s gates left the agent roughly 40 minutes of actual work, and the
ledger would have refused anything honest. The runtime's `tick_timeout` (8 h 30) already
covers a longer attempt; only the two in-loop figures were too tight.

## What landed

**`src/inference_grid/board/packet_task.py`** — one new function, two call sites.

- **`packet_wall_seconds(task)`** — the attempt's wall: `wall_seconds +
  sum(gate timeouts) + 60 × max_rounds`. The gates' timeouts are read with
  `lanes/packet.py`'s 1800 s default, the same figure `_gates_for` runs an undeclared
  gate under, so the computed wall covers what actually runs. The commit gate's own
  60 s is deliberately *not* summed — it is harness overhead, and the brief's test
  expectation (3600 + 1800 + 180 for three 600 s gates and 3 rounds) names it as such.
- **`admit_packet`** submits `timeout: packet_wall_seconds(task)` instead of
  `min(wall_seconds + 40, 3600)`.
- **`run_packet`** passes `wall_seconds=packet_wall_seconds(task)` to `build_loop`,
  so the loop's deadline and the ledger's admission agree on one figure.

**`src/inference_grid/ledger.py`** — the admission bound. The brief names
`ledger.py::_check_spec`; no such function exists at this commit — the timeout bound
lives inline in `Ledger.submit`. The bound there becomes `4 × 3600` when the spec is a
packet spec and stays `3600` otherwise. A spec is recognised as a packet spec by the
`packet-loop:` argv head the packet path has always submitted (`["packet-loop:" + kind,
packet_id, task id]`); no spec shape changed, so nothing about immutability or the
existing ledger rows moves. Provider-authored `board/task.py` is untouched — the
3600 s `wall_seconds` budget cap stands, and the packet's extra wall lives entirely in
the attempt, not the task.

## Boundaries worth the operator's eye

- A packet whose computed wall exceeds 4 × 3600 (up to 8 gates × 3600 s + a full budget
  can reach ~33 000 s) is refused at admission by the ledger's bound, as a task-authoring
  error — the wall is never silently clipped, so the ledger spec and the loop deadline
  can never disagree.
- The runtime's `tick_timeout` (8 h 30) covers the new bound; no runtime knob moved.

## Tests

- **`tests/test_packet_task.py`**, +1 (offline, the `world` fixture):
  `test_the_attempt_wall_covers_the_gates_and_the_reentry_rounds` — a packet with three
  600 s gates, `max_rounds` 3 and a 3600 s budget dispatches through the real tick with
  `build_loop` seeing `wall_seconds = 3600 + 1800 + 180` (a spy wraps the real loop) and
  the ledger task's spec carrying the same `timeout`; the attempt still completes and
  fetches the branch, so the larger wall breaks nothing on the green path.
- **`tests/test_grid.py`**, +2 (offline, the `grid` fixture):
  `test_non_packet_timeout_is_bounded_at_an_hour` (3601 refused) and
  `test_packet_spec_timeout_is_bounded_at_four_hours` (a `packet-loop:` argv admits at
  4 × 3600, refuses at 4 × 3600 + 1).

## Verification

- Full suite (`--ignore=tests/test_calibration.py`; that file fails to *collect* under
  this environment's deny-read policy — a module-level `Path.home()` stat on the
  calibration corpus raises `PermissionError` instead of returning False — at the base
  commit too): the failing set is byte-identical to `53d5a2f`, 88 inherited
  (`/bin/ps` sandbox denials and kin), zero new. The three new tests pass.
- `ruff format` and `ruff check` clean on the changed files. `ruff format` also reflowed
  two pre-existing long lines in `ledger.py` (`record_external`'s signature and an
  `acct` select) that failed `format --check` at the base; disclosed here as the only
  unrelated diff lines.
- No provider-authored file was touched. No new credential access; no absolute home path
  or e-mail address in the diff.
