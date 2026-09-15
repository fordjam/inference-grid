# GLM lane report — brief 14, E2: the pytest gate remembers the base (2026-09-15)

Lane: DeepSeek-V4.1-Flash via Command Code. Base: `origin/glm/work` at `10276b5`.
Branch: `glm/e2-baseline-aware-pytest-gate`, one commit, not pushed.

The packet is E2 from `docs/handoff-glm-15.md`; the driver names this row and report
"brief 14", so they follow the driver's naming.

## What landed

`src/inference_grid/lanes/gates.py` — the pytest gate is now baseline-aware:

- `run_pytest(base, cache_dir, python=None, suite=None)` is the gate check. It reads
  the base's failing node ids, runs the suite on the packet's branch, prints
  `inherited: [...]` (base ∩ branch), `repaired: [...]` (base − branch) and names every
  **new** failure (branch − base); it returns 1 only when a new one exists. The output
  is what the `GateResult` keeps, so `inherited:`/`repaired:` ride in the gate result.
- `baseline_failures(base, cache_dir, python, suite=None)` resolves `base` to a commit,
  checks out that commit in a scratch `git worktree` under `cache_dir`, runs the suite
  there once, and caches the failing node ids as
  `<cache_dir>/pytest-baseline-<sha>.json` (atomic `os.replace`). A second packet on the
  same base reads the cache and pays nothing; a new base commit gets a new key.
  The scratch worktree is always removed (`git worktree remove --force`, then
  `shutil.rmtree`), so no stale worktree registration is left behind.
- `failing_node_ids(output)` parses pytest's short summary (`FAILED path::test[params] -
  message`) up to the ` - ` separator, so parametrised ids with spaces survive.
- `run_suite(python, work)` is the default suite runner: the whole suite, `PYTHONPATH=src`
  relative to `work`. **`-x` is gone** — comparing failures needs every node id, not the
  first one; a baseline is worthless otherwise.
- `pytest_gate(python, base, cache_dir)` builds `python -m inference_grid.lanes.gates
  pytest <base> <cache_dir>`; `main` dispatches it; `gates_for` threads the cache dir.

`scripts/run_lane.py`:

- `gates_for` is called with the attempt store (`--packets-root`) as the cache dir, so
  every packet that shares a base pays for the baseline once.
- The "Capture the list of failing tests BEFORE you change anything" sentence is deleted
  from the composed prompt; it now says the harness captures the base's failures and
  names pre-existing ones `inherited`, so the agent does not duplicate the work by hand.

Note on the packet's wording: the instruction to be deleted was **not** in
`docs/handoff-glm.md`'s conventions — section 2 of that file carries no baseline
sentence. It lived in the driver's `compose_prompt` (`scripts/run_lane.py`), which is
what the agent actually reads, and that is where it was deleted. Nothing in
`docs/handoff-glm.md` needed a change.

## Tests

`tests/test_gates.py`, all offline against a temp git repo with an injected suite (no
pytest subprocess, no network):

- `test_failing_node_ids_takes_the_id_before_the_message` — the parser keeps a
  parametrised id with a space and ignores a passing run.
- `test_pytest_gate_names_inherited_failures_and_fails_only_on_new_ones` — the old
  sandbox failure is named `inherited: [...]`, the packet's failure appears under
  `new failures:` and the gate returns 1.
- `test_pytest_gate_passes_when_every_failure_is_inherited` — the same failure on both
  sides passes and is named.
- `test_pytest_gate_reports_an_inherited_failure_that_now_passes` — a base failure that
  passes on the branch is named `repaired: [...]`.
- `test_the_baseline_is_cached_per_base_commit` — a second packet on the same base runs
  the base suite once (branch suite twice) and leaves one cache file.

`test_gates_for_builds_the_five_module_invoked_gates` is updated for the new pytest argv.

## Defects found in existing code

None in the moved logic. One behavioural consequence worth naming: dropping `-x` means
a failing packet now runs the whole suite instead of stopping at the first failure. That
is required for a set comparison and is the point of the packet; the base suite is paid
once per base commit, not per round.

## Final gate

- `pytest -q` before the change: **80 failed, 393 passed, 7 skipped**; after:
  **80 failed, 398 passed, 7 skipped** (+5 new tests). The sorted `FAILED` node-id set is
  byte-identical to the pre-change baseline (all 80 are pre-existing sandbox
  `killpg`/`/bin/ps` failures).
- The real gate was also exercised end to end: `python -m inference_grid.lanes.gates
  pytest origin/glm/work <cache>` reported all 80 failures as `inherited`, zero `new`
  and exited 0, leaving only the cache JSON and no extra `git worktree`.
- `ruff format` and `ruff check` on `src/inference_grid/lanes/gates.py`,
  `tests/test_gates.py` and `scripts/run_lane.py`: clean.
