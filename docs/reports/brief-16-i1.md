# Report — brief 16 I1, landing as a code node (interactive ZCode, GLM-5.3-Flash)

Branch `glm/i1-landing-as-a-code-node` from `glm/work` (369230c), worktree
`~/.grid-workspaces/inference-grid-zai-i1`. One commit, trailer
`Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>`. Not pushed; the base was not advanced by
me — only by the node under test, in its own scratch worktrees. Nothing from the I2/I3
file list (`board/branch_review.py`, `lanes/route.py`, `board/calibration.py`,
`deployments/local/tick_boards.py`, overlay/cloud) was touched; `board/task.py`
(provider-authored) is untouched — the `landed` transition is validated in `land.py`
and the packet shape in `board/packet_task.py`.

## What landed

- **`board/land.py`** — `land_packet(board_dir, project_root, task_id, base, gates,
  ledger, packets_root, dry_run)` and the `inference-grid land --json
  {board_dir, project_root, task, base, gates, packets_root?, dry_run}` entry:
  1. **Transition guard, in land.py** (the brief's "validate the transition here"):
     category `packet`, state `passed`, no prior `landed`; anything else is refused
     without touching the file.
  2. **The proof**: `verify_merge(project_root, "packet/<task-id>", base, gates)` in
     its scratch worktree. `verify_merge._gate_nodes` gained gate-`env` passthrough
     (behavior-identical for env-less gates) so the proof and the landing run execute
     the same gates.
  3. **The merge**, in a worktree of the base — the existing worktree that owns the
     branch when one exists (the coordinator's `glm/work` worktree; the same rule
     `scripts/run_lane.py` used), a scratch `git worktree` claimed for the landing and
     removed after otherwise. A dirty base worktree, or one whose HEAD moved since the
     verify, blocks instead of committing. `--no-ff`, except when the base is an
     ancestor of the branch head: then the landing is a fast-forward (`how: "ff"`),
     because the verify just proved that bit-identical tree. A clean merge is
     `how: "merge"`.
  4. **Union pair**: conflicts confined to `docs/CONTRIBUTIONS.md`/`docs/LANES.md` are
     resolved by keeping both sides — `git merge-file --union` over the three stages,
     the rows are independent — and the landing continues (`how: "union"`); any other
     conflict blocks the task naming the files. The gates run **once more on the
     resolved tree** before `git commit --no-edit` keeps the merge commit; a gate
     failure aborts the merge and blocks with the gate's tail.
  5. **Serialization**: a lock directory `packets_root/land-<base>.lock` (atomic
     mkdir, pid inside). A second landing reports `landing busy` and leaves the task
     `passed` for the next tick — transient, not a block.
  6. **Settling**: the task gains `landed: {base_head, merge_commit, how}` and settles
     `landed`; the ledger records a `landed` event whose attempt is the packet's
     completed ledger attempt (found by the task-id prefix; the task id itself as the
     key when no attempt exists, so the event is always queryable by kind).
- **`board/packet_task.py`** — `validate_packet_task` accepts the terminal shape:
  state `landed` requires `landed: {base_head, merge_commit, how}` (full 40-hex heads,
  `how ∈ {ff, merge, union}`, `blocked_reason` None). The provider-authored
  `board/task.py` sees a `passed` state on the shared copy, as with the category swap.
- **`board/runner.py::tick`** — new `auto_land` pass after the ready loop: the board is
  reloaded (a packet that passed in this same tick is settled in its file, not in the
  loop's snapshot) and every `passed` un-landed packet is landed with its own
  `spec.gates` onto its own `spec.base`. Results: `landed` / `land_blocked` /
  `land_busy`; the dry run reports `auto_land: would land (ff)` or
  `auto_land: would not land: <reason>` in the plan, with the structured report under a
  `land` key, and its proof runs read-only (no lock, no task write, no ref move).
- **`cli.py`** — `land` registered (choices, `--json`-required set, dispatch, exit 0 on
  landed/would-land); `board_tick` accepts `auto_land` and forwards it both on the
  single-board path and per config in the boards path — `tick_all` already forwards
  arbitrary config keys, so a board config gains `"auto_land": true` and the loop
  closes. No change to `deployments/local/tick_boards.py` (not mine).
- **`docs/BOARD.md`** — a "Landing" section, and the Packet-tasks paragraph now points
  dispatch's "base never advanced" at the landing node.

## Verification

- Baseline before any change: **590 passed, 7 skipped, 0 failed** (failing list empty).
  After: **600 passed, 7 skipped** — the failing list is byte-identical (empty), 10 new
  tests in `tests/test_land.py`: clean fast-forward (base, operator checkout and
  worktree list untouched), a CONTRIBUTIONS-only conflict landed by union with both
  rows present, a source conflict blocked with `mod.py` named, a gate failing on the
  merged tree (blocked at the landing run on the union path, and at the verify on the
  clean path, base untouched both times), the lock (busy, then lands after release),
  the dry run both ways, the landed-shape validation round trip, a non-passed task
  refused, the tick `auto_land` pass real and dry, and the CLI entry.
- Lint: new/edited files are `ruff format`-clean; `ruff check src tests` reports
  exactly the one pre-existing finding `glm/work` has (unused `uuid` in
  `tests/test_external_work.py`). The pre-existing ruff-0.16.7 format drift across the
  tree was left alone (see the brief-14 D1 report for the numbers).

## Notes and limits

- **The `how: "ff"` reading.** The brief says merge with `--no-ff` and also lists
  `how: ff`. I resolved the tension as: `--no-ff` for every real merge; a base the
  branch simply extends fast-forwards, since there is nothing to merge and the gates
  just proved that exact tree in the verify. If the coordinator wants a merge commit
  even there, it is a two-line change in `_land_locked`.
- The lock is per-base under `packets_root` as specified, but it is a directory lock on
  one machine — it serializes ticks on the operator host, not two hosts sharing a
  packets store over NFS.
- The landing run keeps gate logs under `packets_root/<task-id>/land-gates/` and the
  union scratch under the system temp dir; both are plain files, safe to prune.
- `digest` counts tasks by state generically, so `landed` tasks appear once their
  state is in the ledger-driven counts; no dashboard change was needed (and none is in
  this packet's file list).
