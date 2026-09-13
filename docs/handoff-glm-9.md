# Handoff brief 9 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-8.md` (7 A1–A3, B1; 8 B1–B4 done 2026-09-13, `da85958`…`4ab1038`;
328 tests). **Section 1 of `docs/handoff-glm.md` applies unchanged.**

The coordinator tried `--review-branch` live on factory-frontend `glm/r6-followups`
(13 commits, base `fb024c8`, tip `109b4aa`): it authored correctly (after the allow-list gained
`web/`, `cd20160`) and produced a packet of **91 inputs, 976 KB, 247 KB diff** — roughly ten times
what the only working reviewer (kimi-k3 at `high`, ~24k tokens proven) can take. The task was
deleted before dispatch. That is the whole of A1.

---

#### A1. Review packets need a byte budget and a natural unit
`board/branch_review.py::review_branch` stages every changed file whole. Change it to:
- **Budget.** `max_input_bytes` (default 120 000 ≈ 30k tokens; a named constant, overridable
  in the JSON). Count the staged bytes before writing anything; over budget → refuse, and print
  the split it would make (below) so the operator can re-run per part.
- **Exclusions.** Never stage generated or lock content: `*/fixtures/*`, `package-lock.json`,
  `uv.lock`, `*.min.*`, `dist/`, anything `git check-attr -a` marks `linguist-generated` or
  `-diff`. Name the excluded paths in the brief ("N generated files omitted: …") so the reviewer
  knows they exist.
- **Hunks, not files.** Stage `diff.patch` plus, for each changed source file, only the file
  *if* it is under a per-file cap (16 KB); larger files are represented by their hunks in the
  patch alone, and the brief says so.
- **Split by commit group.** `split: "commit"` (default when over budget) authors one review task
  per commit whose own packet fits, named `review-<repo>-<short>` in range order, each brief
  quoting that commit's subject and body; `split: "none"` keeps one task and refuses over budget.
  A commit that alone exceeds the budget is refused with the offending file sizes.
- Tests in `tests/test_branch_review.py`: fixture-dir exclusion; per-file cap; over-budget
  refusal names the split; commit split authors N tasks with disjoint inputs; a single
  oversized commit is refused.
- Size: medium.

#### A2. `board-status` across boards
With `boards/factory-frontend.json` now existing, `board-status` should accept `boards: [...]`
like `board-tick` (handoff-8 B2) and print each board's table under its project name.
- Size: small.

#### B1. Inbox → integration checklist
The constraint the programme keeps hitting is integration, not building. Add
`inference-grid inbox-integrate --json {project_root, task_id, dry_run: true}`: read
`grid/inbox/<task-id>.json` on the `grid/inbox` branch, compute the patch of that task's landed
artifacts against the project's current `HEAD`, and write `grid/inbox/<task-id>.integrate.md`
(on the inbox branch, not the operator's tree): the diffstat, whether the patch applies cleanly
to `HEAD` (`git apply --check` through the injectable `run`), the tests the task declared, the
review task and reviewer family, and the exact `git` commands the operator would run. Dry-run
only under this brief; `dry_run: false` is refused with "not implemented".
- Tests with a temp repo where the inbox branch exists (reuse `tests/test_board_end_to_end.py`'s
  setup): clean apply; a conflicting HEAD reports the conflict.
- Size: medium.

## C — out of scope
Same as handoff 1.

## Definition of done
As handoff 5.
