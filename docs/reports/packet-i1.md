# GLM lane report — brief 16, I1: landing as a code node (2026-09-15)

Lane: GLM-5.3-Flash via Command Code. Base: `glm/work` at `0f29db0`.
Branch: `packet/packet-i1`, one commit, not pushed.

## Why

Six of seventeen landings needed the coordinator's hands. A `packet` task that settles
`passed` stops there: its branch sits at `packet/<task-id>` and a human merges it. This
packet turns that merge into a code node the board can run itself, in front of the same
merge proof (B1's `verify_merge`) the review line already trusts.

## What landed

`src/inference_grid/board/land.py` (new) — `land(board_dir, project_root, task, base=None,
gates=None, packets_root=None, ledger=None, dry_run=False)`:

- Only a `passed` packet task lands; anything else is refused with the reason and the task
  is left alone for the next tick (the transition is one-way, so a re-land refuses too).
- It runs `verify_merge` (B1) for `packet/<task-id>` against `base` with the task's
  declared gates in a scratch worktree under `<packets_root>/<task>/land/`. A non-unionable
  conflict, a failing gate, or a broken ref settles the task `blocked` with the conflicting
  file list or the gate's tail.
- On mergeable + green gates it merges into `base` with `--no-ff` in a second scratch
  worktree detached at the base head — never the operator checkout — so the base always
  receives a true merge commit keeping both parents, even on a fast-forward. Conflicts
  confined to `docs/CONTRIBUTIONS.md` and `docs/LANES.md` are retried with git's union
  merge driver (`.gitattributes` written into the scratch worktree and removed before the
  commit); rows there are independent, so keeping both sides is always correct. Any other
  conflict aborts and blocks the task with the file list. The declared gates run once more
  on the real merged tree before the merge commit is kept — only the union resolution can
  make that tree differ from the verified one, and the union case is exactly where a bad
  merge would otherwise land.
- `how` records what the landing was: `ff` when the base head was an ancestor of the branch
  (the merge commit's tree is exactly the branch's tree), `union` for a union-resolved
  merge, `merge` otherwise. `--no-ff` is unconditional, so `ff` describes the topology, not
  a missing merge commit.
- The base ref then moves (`git update-ref refs/heads/<base>`). A base checked out in a
  worktree other than the project root is refused — a ref move there would desync someone
  else's checkout. When the project root itself holds the base, it must have no *tracked*
  local changes: the landing syncs that checkout with `reset --hard` after the ref move.
  Untracked files (the board's own task files, typically) are ignored by the check and
  never touched by the reset; only tracked changes could be clobbered, and those refuse.
  This is the real nightly shape: the board lives at `<worktree>/grid/board` and its
  project root holds the base.
- Landings serialize on a lock directory under `packets_root`
  (`<packets_root>/land-locks/<base>`), created atomically with `mkdir`; the pid is
  recorded, a lock whose owner is provably dead is stolen, and the lock is released in a
  `finally`. A held lock is a refusal, not a block — the next tick retries.
- Success writes `landed: {base_head, merge_commit, how}` onto the task, settles it
  `landed`, and records a `packet_landed` ledger event (no attempt attached — a landing
  spends no quota). `--dry-run` runs only the verify probe and reports `would_land`, the
  projected `how`, conflicts and gate results without a lock, a task write or a ledger
  event.

`src/inference_grid/board/packet_task.py` — `validate_packet_task` now accepts the one
extra packet key, `landed`, and the `landed` state (validated against the shared check as
its nearest terminal state, then restored): the record `{base_head, merge_commit, how}` and
the state must come together or not at all, shas are 40-hex, `how` is one of
`ff`/`merge`/`union`. **`board/task.py` is provider-authored (integrated unmodified) and was
not touched** — it refuses both the state and the extra key, so the acceptance lives in the
packet wrapper, exactly the precedent `verify_merge` and `packet` set.

`src/inference_grid/board/runner.py` — `tick(..., auto_land=False)`: with the flag, after
the dispatch pass the board is re-read (so tasks the pass itself settled `passed` are
covered) and every `passed` packet task goes through `land`, one result row each
(`landed`, `land_refused: <reason>` with the task left `passed`, or `land_error` with the
task blocked). Dry runs plan `auto_land` rows without probing or landing.

`src/inference_grid/cli.py` — `land` added to the choices and the requires-`--json` set;
it goes through the normal `Ledger(...)` construction (a landing records an event, so it
needs the database) and exits 0 only when it landed, or when a dry run reports
`would_land`.

`src/inference_grid/board/verify_merge.py` — one additive change, disclosed: `_gate_nodes`
now passes a packet gate's `env` through (`Gate.env`). The verify_merge *task* spec still
refuses `env`; packet gates carry it (the real gates set `PYTHONPATH=src`), and without
this the probe would run them in a different environment than the packet loop did.

`docs/BOARD.md` — the state list gains `landed` and a Landing section describes the node,
the union policy, the lock and `auto_land`.

## Tests

`tests/test_land.py` (15 tests, all offline against temp git repos, no network):

- `validate_board_task` accepts a landed packet task and refuses the record without the
  state, the state without the record, a non-hex sha and a bad `how`.
- A clean fast-forward lands: a fresh `--no-ff` merge commit whose parents are the old
  base head and the branch head, tree identical to the branch's tree, task `landed` with
  the exact record, one `packet_landed` event, lock released, no worktree left.
- A diverged base gets a true merge (`how: merge`, two parents named).
- A CONTRIBUTIONS-only conflict lands by union: the merged file carries both rows, the
  branch tip untouched.
- A source conflict blocks with `main.txt` named, base untouched.
- A gate that passes on the branch but fails on the union-resolved merged tree blocks with
  `failed on the merged tree`, base untouched, no ledger event.
- A live lock (this process's pid) refuses and leaves the task `passed`; a stale lock
  (reaped pid) is stolen and the landing proceeds.
- A dry run reports `would_land`/`how` and touches nothing.
- Only a `passed` packet task lands; a base held by another worktree is refused; the
  project root holding a clean base is landed and synced; a tracked local change refuses.
- Through `runner.tick`: `auto_land=True` lands the passed packet task in the same pass
  (base tree becomes the branch's tree); without the flag it waits.
- The CLI through `cli.main()`: dry run exits 0 touching nothing, a real landing exits 0,
  a refused re-land exits 1.

## Defects found in existing code

None. The one deliberate relaxation is `verify_merge._gate_nodes` carrying `env`, named
above. The `--no-ff`/`how` reading is a judgment call worth recording: the brief says both
"merge with `--no-ff`" and `how: ff|merge|union`, and only "always a merge commit, `ff`
describes the topology" makes both sentences true at once.

## Final gate

- Full suite before the change (scratch worktree at `0f29db0`, same flags): **88 failed,
  481 passed, 7 skipped, 1 collection error**; after: **88 failed, 496 passed, 7 skipped,
  1 collection error** (+15 new tests). The sorted `FAILED`/`ERROR` set is byte-identical
  to the base (the collection error is `tests/test_calibration.py` hitting the lane's
  deny-read on the private calibration corpus — environmental, present at the base).
- `ruff format` and `ruff check` on `board/land.py`, `board/runner.py`,
  `board/packet_task.py`, `board/verify_merge.py`, `cli.py`, `tests/test_land.py`: clean.

## Rounds 2–3 — commit gate

The commit gate failed twice, both times on its own terms rather than the packet's code:

1. Round 2: the clean-tree check tripped on the untracked `.commandcode/` directory — the
   session CLI's own state (the taste store) in the packet clone, never project content,
   the same local-state category `.gitignore` already covers (`.pytest_cache/`,
   `.ruff_cache/`). Fixed by ignoring `.commandcode/`; no test, gate or source file
   touched.
2. Round 3: the same gate then counted **two** commits ahead of `0f29db0` and demands
   exactly one — the packet's own standing contract ("finish with exactly ONE commit on
   this branch"). The two requirements cannot both be met with an additional commit, and
   the gates are the judge, so the two rounds' work (the packet and the `.gitignore` fix)
   is re-committed as the single commit the gate and the brief require. Nothing about the
   tree changed in this reconciliation; only history shape.
