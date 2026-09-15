# GLM lane report — brief 14, E1: the driver's gates as a tested module (2026-09-15)

Lane: DeepSeek-V4.1-Flash via Command Code. Base: `origin/glm/work` at `0c1b599`.
Branch: `glm/e1-lanes-gates-py-the-driver-s-gates-as-a-t`, one commit, not pushed.

The packet is E1 from `docs/handoff-glm-15.md`; the driver names this row and report
"brief 14", so they follow the driver's naming.

## What landed

`src/inference_grid/lanes/gates.py` (new) — the driver's gates as module-level Python:

- Checks, each printing its verdict and returning the exit code: `run_commit(base, trailer)`
  (exactly one commit ahead of base, trailer present, tree clean), `run_scoped_ruff(base, mode)`
  (ruff `format --check`/`check` over the changed and untracked `.py` files only), and
  `run_home_paths(base)` (the runtime home in changed files only).
- Gate builders, each a small function returning a `packet.Gate`: `pytest_gate`,
  `ruff_gate`, `home_paths_gate`, `commit_gate`, and `gates_for(python, base, trailer)`
  composing the five in the original order.
- `__main__` dispatch, so a gate is a command, not a written file:
  `python -m inference_grid.lanes.gates ruff <base> <format|check>`,
  `... home-paths <base>`, `... commit <base> <trailer>`.

`scripts/run_lane.py` — `commit_gate_script` and the old `gates_for` are deleted; the driver
imports `gates_for` from the module and the `gate-scripts` directory with its per-attempt
`.py` files is gone. Gate names, argv tails, timeouts, env (`PYTHONPATH=src`) and order are
unchanged. The git commands were preserved exactly: `base..HEAD` for the commit count,
`base...HEAD` with `--diff-filter=AMR` for the ruff and home-path file sets,
`ls-files --others --exclude-standard` for untracked files, and the same messages and
refusals. The trailer is a parameter end to end (`commit_gate`/`gates_for`/`run_commit`), so
the single model→trailer mapping stays in `run_lane.py` (`TRAILER`/`TRAILERS`/`trailer_for`).
The home path is still read at runtime with `Path.home()`; no literal sits in the source, so
the gate neither writes down the operator's home nor matches its own text.

`tests/test_gates.py` (new, 6 tests) — see below.
`tests/test_run_lane.py` — the commit-gate case moved to the module's tests (the module owns
that behaviour now); the remaining driver tests are untouched.

## Tests

Each new test builds a temp git repo and is offline:

- `test_scoped_ruff_sees_only_changed_and_untracked_python` — an unformatted `.py` committed at
  base is ignored; a changed `.py` and an untracked `.py` are caught; no changes at all passes
  with `no python files changed`; check mode catches a changed lint error.
- `test_home_paths_gate_matches_the_runtime_home_in_changed_files_only` — the runtime home in a
  file the packet did not touch passes; in a changed file it fails and names the match; a
  `/home/example`-style fixture is another machine's home and passes; the gate's own source
  passes.
- `test_commit_gate_refuses_count_trailer_and_dirty_tree` — the three refusals (0 commits,
  missing trailer, dirty tree) then the pass.
- `test_commit_gate_trailer_is_a_parameter` — the commit's own trailer passes, a different one
  refuses.
- `test_gates_for_builds_the_five_module_invoked_gates` — names, `-m` argv, env and trailer.
- `test_the_module_is_the_gate_command` — `python -m inference_grid.lanes.gates commit <base>
  <trailer>` in a temp repo.

## Defects found in existing code

None in the moved logic: behaviour is preserved command for command, and the new tests pin the
parts that had none (the home-path gate and the scoped-ruff file selection were untested).

One trust observation rather than a regression this packet alone introduces: the gates now
resolve from the package under test (`PYTHONPATH=src` against the lane clone), so a packet that
edits `src/inference_grid/lanes/gates.py` executes its own gate code. The old scoped-ruff and
commit scripts were written by the driver outside the clone and could not be edited by the
agent. The packet asked for `python -m inference_grid.lanes.gates <gate> <args>`, so this is
the requested shape; if gate integrity against a self-editing packet matters, the driver
should point the gate processes at its own `src` (absolute) instead of the clone's. That is a
coordinator decision and was not made here.

`scripts/run_lane.py` is not one of the provider-authored "integrated unmodified" files, so
editing it was fair; the code moved out of it is now tested in the package.

## Final gate

- `pytest -q`: before **80 failed, 388 passed, 7 skipped**; after **80 failed, 393 passed,
  7 skipped** — +6 new tests, −1 moved out of `test_run_lane.py`; the failure set (sorted
  `FAILED` node ids) is byte-identical to the baseline captured before any change, all
  pre-existing sandbox `killpg`/`/bin/ps` failures.
- `ruff format` and `ruff check` on `src/inference_grid/lanes/gates.py`, `tests/test_gates.py`,
  `tests/test_run_lane.py` and `scripts/run_lane.py`: clean.
- The literal home path appears nowhere in the changed source; `gates.py` reads `Path.home()`
  at run time.
