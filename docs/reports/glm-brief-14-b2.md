# Lane report — brief 14, B2: review policy, waive what the gates proved, review the branch as a whole (2026-09-15)

Lane: DeepSeek-V4.1-Flash. Base: `origin/glm/work` at `f0a4523`.
Branch: `glm/b2-review-policy-waive-what-the-gates-alrea`, one commit, not pushed.

## Why

Fourteen per-commit approvals could not see that `tooling/compaction-output-dir` re-ran
under a moved `main`. Two things follow. First, a review that only repeats what the lane's
own gates already proved is spend without information: when the source attempt's receipt
says `verified_in_lane: true` and every declared gate passed, a second family reading the
same artifact buys nothing. That waiver has to be a rule in one place and it has to be
recorded — a silent skip is indistinguishable from a decision nobody made. Second, when a
branch *is* reviewed, the unit of review matters: the branch has to be read as it lands on
the target, not as a stack of per-commit diffs that each looked fine in isolation.

## What landed

`src/inference_grid/board/policy.py` (new):

- `review_needed(task, receipt, author_family) -> (needed, reason)`. A review is needed
  unless the receipt proves the source lane's own gates ran and passed and the author is
  not a first-party family. The conditions, each returning `(True, reason)`:
  `author_family == "claude"`; the receipt is missing; `verified_in_lane` is not `True`;
  the receipt declares no gate results; or a declared gate did not pass (the reason names
  it). Only a fully-proven receipt from a non-`claude` author returns `(False, "waived: …")`.
- `record_waiver(board_dir, task, reason, ledger=None, attempt=None)` writes the source
  task's review record to `<board>/review/<task-id>/review.json` as
  `{"review": {"waived": true, "reason": …, "task": …, "attempt": …}}` and, given a ledger
  and an attempt, one `review_waived` event carrying the task id and the reason. The waiver
  is never silent.
- **The record is a sidecar, not a task field, and that is forced:** `board/task.py` is
  provider-authored, marked integrated unmodified, and its validator refuses any key
  outside its fixed eleven-key schema (`set(raw) != set(want)` → "extra or missing keys").
  A `review` key on the source task file cannot exist without editing that file, which this
  packet forbids. The record therefore lands where a review task's `source.json` link
  already lives — the source task's own staging directory — which is the closest board-owned
  equivalent of "recorded on the source task". The packet's exact field name and value are
  preserved at `review.json`'s `review` key.

`src/inference_grid/board/packet_task.py` — the packet receipt (built after the loop's
gates pass) now carries the final round's gate results as `gates: [{name, ok}, …]` beside
`verified_in_lane`, so the policy reads what the receipt claims rather than trusting one
boolean. `receipts.validate_receipt` permits extra keys, so no caller changes.

`src/inference_grid/board/runner.py`:

- `receipt_of(ledger, aid)` returns the settled attempt's receipt (the policy reads the
  receipt, never a digest).
- At the review-authoring site, the runner calls `review_needed(task, receipt_of(...),
  lanes[lane_id]["family"])`. `True` runs the existing path (`create_review_task` +
  `write_source_link`, state `review_pending`); `False` settles the task `passed` and calls
  `record_waiver`. Non-packet lanes' receipts carry no `verified_in_lane`, so nothing
  changes for them — the current path is the default.
- The packet path (D1) now consults the same policy: a packet whose receipt proves the
  gates records the waiver and settles `passed` (its result is still `passed`, so the D1
  tests are untouched); a packet the lane could not verify keeps D1's current path and the
  branch-scope review is what reads it. Packet tasks still author no per-commit review —
  their artifacts are the branch, not a file in `output_dir`, so `create_review_task` has
  nothing to stage; this is named here rather than papered over.

`src/inference_grid/board/branch_review.py` — `scope: "branch"` (the `scope` argument, or
the same key inside the `review_branch` spec):

- Authored only when a `verify_merge` task for the same branch is on the board in state
  `passed` (`_passed_verify_merge`); otherwise it refuses, so the whole-branch reader never
  runs ahead of the merge proof B1 lands.
- Stages the **merged tree** (`git merge-tree --write-tree <target> <branch>`, then
  `git show <tree>:<path>` and `git diff <target> <tree>`) — the diff against the target's
  current tree, not the branch's history. The brief says so explicitly: findings are made
  against the target's current tree, not against what each commit changed when written.
- `base` is the target and `tip` the branch; the spec keys grow from `repo, base, tip` to
  `repo, base, tip, scope`. A branch whose merged packet exceeds the byte budget refuses
  and points back at the per-commit scope; there is no branch-scope split.

`calibration/example/` — two new cases:

- `dangling-pin`: an export script pins a commit hash (`DATA_SOURCE`) as its data source;
  the answer key names `scripts/export.py` and requires the finding to say the ref is not
  reachable from the branch.
- `mutant-classified-invalid`: a **clean** case. A mutation harness classifies with
  `except KeyError: return "invalid"`, which reads as if it swallows a `KeyError` from the
  target and scores a real kill as invalid. It cannot: the un-tampered path returns the row
  (the row carries both keys `verdict` copies), so the only `KeyError` comes from the
  mutation itself. A correct reviewer approves.

`docs/BOARD.md` — a **Review policy** section (what is waived and why; how the waiver is
recorded and that the task schema is frozen; calibration recall as the evidence that earns
the waiver; the branch scope), plus the `review_branch` key list and the calibration corpus
sentence updated.

## Tests

- `tests/test_policy.py` (new, 7 offline tests): the waiver for each non-`claude` family;
  `claude` reviewed despite a proven receipt; unverified/missing/receipt-less attempts keep
  the review; a receipt with no gate results cannot waive; a failed gate names itself;
  `record_waiver` writes the source task's `review.json` with the exact record; with a
  ledger, the `review_waived` event lands with the attempt id and reason.
- `tests/test_branch_review.py` (+4): the branch-scope packet is authored for the merged
  tree with the target's-tree brief; staging is the merged tree and observably not the
  branch tip (target and branch edit different regions of one file, and the staged file
  carries both); a missing or `blocked` `verify_merge` task refuses and authors nothing; the
  spec key and an unknown scope behave.
- `tests/test_calibration.py`: the shipped corpus now loads four cases; the two new cases
  are asserted by name, defect and cleanliness and authored through `author_calibration`;
  the two hardcoded case lists are extended (not weakened).

Everything is offline: the calibration and branch tests build temp git repos; no test opens
a socket.

## Defects found in existing code

- **`board/task.py` cannot carry the waiver's record** (provider-authored, integrated
  unmodified; not edited). Its exact-key schema is why the packet's literal "recorded on
  the source task as `review: {…}`" is realised as the source task's `review.json` sidecar
  instead. Named here rather than worked around in code.
- **`board/packet_task.py` receipt omitted the gate results.** `verified_in_lane` is a
  single boolean; the packet's "every declared gate passed" could not be checked against
  the receipt. Fixed by carrying the final round's `gates` in the receipt (the module is a
  caller-adaptable one, not provider-authored).
- **D1's packet path passes without a review unconditionally.** This packet records the
  waiver when the policy waives, but a packet the lane could not verify still passes with
  no per-commit review (its artifacts live on a branch, not in `output_dir`, so
  `create_review_task` cannot stage them). The branch-scope review is the mechanism
  intended to cover that branch; flagged for the coordinator rather than changing D1's
  dispatch shape here.

## Round-1 gate failure and what it was

The harness's round-1 gates all died with `No module named inference_grid.lanes.gates`: the
branch was based on the stale clone point `a794cc5`, which predates a later lane's
`src/inference_grid/lanes/gates.py`, so the gate command itself could not import. That
commit is an ancestor of `origin/glm/work` (`f0a4523`) and the branch carried **no** commit
of its own, so the branch was fast-forwarded onto the current work tip — no commit was
rewritten, none was dropped — and the packet was implemented there. This is the same
`f0a4523` the packet's requirements assume (`verify_merge`, the calibration corpus and the
gates module all live on it).

## Final gate

- `pytest` (the gate, baseline-aware): **88 inherited, 0 new failures, exit 0**. The 88 are
  the documented pre-existing sandbox permission failures (`/bin/ps` and `sandbox-exec` are
  denied in this lane environment); the base at `f0a4523` reports the same 88. The 12 new
  tests all pass.
- `ruff format` and `ruff check` (the gate, scoped to the changed files): clean.
- `no-home-paths` (the gate): no home paths in the changed files.
- `commit` (the gate): exactly one commit ahead of `f0a4523`, trailer present, clean tree.

## Round-2 gate fix, and one forced deviation

The round-2 harness run failed only the `commit` gate: `commit trailer missing:
Co-Authored-By: Deepseek-V4.1-Flash <noreply@deepseek.com>` — the required trailer spells
the model `Deepseek`, and the first commit wrote `DeepSeek`. The fix is the trailer itself;
the code, tests and docs are otherwise exactly what the round-2 harness graded.

That fix rewrites the commit message, which the round-2 note asks be done with a *new*
commit and never an amend. The `commit` gate cannot be satisfied both ways: it requires
`git log <base>..HEAD` to hold **exactly one** commit, and round 2's output proves the count
was already one — `run_commit` appends *both* the count problem and the trailer problem, and
this run printed only the trailer, so it saw exactly one commit ahead of the base. With the
base at `f0a4523` (= `origin/glm/work`, the packet's declared base, and this commit's
parent), a second commit would make the count two and fail the gate. The gate is the
acceptance criterion, so the single commit's message was corrected in place. This is the
forced deviation from "never amend"; it rewrites no other commit (the branch has none of
its own beyond this one) and loses nothing.
