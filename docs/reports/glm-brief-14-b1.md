# GLM lane report — brief 14, B1: verify-merge, a code node before any review (2026-09-15)

Lane: GLM-5.3-Flash via Command Code. Base: `origin/glm/work` at `0eca0af`.
Branch: `glm/b1-verify-merge-a-code-node-before-any-revi`, one commit, not pushed.

## Why

`tooling/compaction-output-dir` would have been blocked the day `main` re-ran the reports.
No model can see that from a per-commit diff; `git merge && pytest` can. This packet makes
that check a code node the board can run itself — before any review, on no lane, at no
quota cost.

## What landed

`src/inference_grid/board/verify_merge.py` (new):

- `verify_merge(repo, branch, target, gates, work_dir=None)` resolves both refs to commits,
  adds a scratch `git worktree add --quiet --detach` under `work_dir` (the system temp dir
  when None; the directory is created when missing) — detached, so a branch already checked
  out in the operator's checkout is never a refusal and never disturbed — and runs
  `git merge --no-commit --no-ff <target>` there.
- On conflict it records the unmerged paths (`git diff --name-only --diff-filter=U`) and
  runs no gate. Otherwise every gate runs as a code node through
  `lanes/packet.py::run_gates` (full output on disk under `gate-logs/`, tail carried).
  The report is `{mergeable, conflicts, gates: [{name, ok, returncode, tail}],
  branch_head, target_head, elapsed_s}`.
- Cleanup is unconditional (try/finally at both levels): `git merge --abort`, then
  `git worktree remove --force`, then `shutil.rmtree` of the scratch directory. No
  worktree registration survives, and the operator checkout's HEAD, index and status are
  never touched.
- `verify_merge_cli(spec)` is the `inference-grid verify-merge --json
  {repo, branch, target, gates, out}` entry: same report, written to `out` when given.
- `validate_verify_merge_task(raw)` accepts the board shape: the standard task keys plus
  `spec: {branch, target, gates}` (ref names validated like packet bases; gates need
  name/argv, optional cwd/timeout, nothing else).

`src/inference_grid/board/packet_task.py` — `validate_board_task` routes category
`verify_merge` to the new validator. **`board/task.py::validate_task` itself is
provider-authored (integrated unmodified) and was not touched**; the packet's
"`validate_task` accepts the category" is satisfied at the caller wrapper, exactly the
precedent `validate_packet_task` set for `packet`. The shared key checks run on a copy
whose placeholders satisfy the packet constraints: a verify_merge task is settled by code
alone, so `inputs`, `artifacts` and `lanes` may legitimately be empty, and the raw task
file keeps those empty lists (the placeholders never reach disk).

`src/inference_grid/board/runner.py`:

- `tick` handles a ready `verify_merge` task before lane selection: it never reaches
  `select_lane`, staging, admission or the ledger — no attempt, no quota, no lane.
  `dry_run` plans it as `{"task", "lane": None, "reason": "verify_merge"}` and runs
  nothing.
- `settle_verify_merge` runs the node with the scratch under
  `<packets_root>/<task-id>/verify/` and settles `passed`, or `blocked` with the
  conflicting paths ("merge conflicts: <paths>") or the first failing gate's tail
  ("gate <name> failed (exit N): <tail>") as `blocked_reason`, truncated to the schema's
  300 chars. A node that cannot run at all (bad ref, git failure) blocks with the error
  and never stops the tick.

`src/inference_grid/cli.py` — `verify-merge` added to the choices and the requires-`--json`
set; it dispatches **before `Ledger(...)` is constructed**, so the command creates no
database, opens no quota and needs no `--database`. It prints the report, writes `out`,
and exits 0 only when the merge is clean and every gate passed.

## Tests

`tests/test_verify_merge.py` (13 tests, all offline against temp git repos, no network):

- A clean branch merges and its gate (a script asserting both sides' files exist) passes;
  `branch_head`/`target_head` are the resolved commits.
- A conflicting branch reports `mergeable: false`, names `main.txt` in `conflicts`, and
  runs no gate.
- A clean merge whose gate exits 1 keeps `mergeable: true` but carries the gate's tail
  and returncode.
- Parametrized over all three cases: after the call the operator checkout's HEAD, index
  (`git write-tree`) and status are byte-identical to before, the scratch directory is
  empty, and `git worktree list` names only the checkout itself.
- `validate_board_task` accepts the verify_merge shape and keeps the empty
  inputs/artifacts/lanes; bad specs (missing keys, unknown keys, escaping ref, empty
  gates, a gate with `env`, a gate without argv) raise `ValueError`.
- The board path through `runner.tick` with a fake gate: `passed` with no ledger attempt
  (`ledger.status()` stays empty) and the checkout untouched; `blocked` on a conflict with
  the path named; `blocked` on a gate failure with the tail in `blocked_reason`; `dry_run`
  plans the task and leaves it `ready`.
- The CLI through `cli.main()`: writes `out`, exits 0 on a passing gate and 1 on a failing
  one.

The real command was exercised end to end against this checkout
(`verify-merge` of the packet branch against `origin/glm/work`, git gate): report correct,
exit 0, no worktree left behind.

## Defects found in existing code

None. One deliberate relaxation, named above: the shared `validate_task` constraints that
inputs/artifacts/lanes must be non-empty do not fit a code-node task, so the caller
wrapper satisfies them with placeholders instead of editing the provider-authored file.

## Final gate

- `pytest -q` before the change: **88 failed, 425 passed, 7 skipped**; after:
  **88 failed, 438 passed, 7 skipped** (+13 new tests). The sorted `FAILED` node-id set is
  byte-identical to the pre-change baseline (all 88 are pre-existing sandbox
  permission/`killpg` failures in this lane environment).
- `ruff format` and `ruff check` on `board/verify_merge.py`, `board/runner.py`,
  `board/packet_task.py`, `cli.py` and `tests/test_verify_merge.py`: clean.
