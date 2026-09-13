# Handoff brief 10 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-9.md` (A1–A2, B1 done 2026-09-13, `be9baf7`…`bfa1c0d`; 340 tests).
**Section 1 of `docs/handoff-glm.md` applies unchanged.** All three items come from the first
live `--review-branch` split on factory-frontend `glm/r6-followups` (2026-09-13 evening).

#### A1. Exclusions must apply to `diff.patch` too
The fixture-bundle commit (`9809512`) was refused at 127,916 bytes although its staged source
files total ~17 KB: `tests/fixtures/**` is excluded from staging but its hunks stay in
`diff.patch`. Generate the patch with the same exclusion set (`git diff -- . ':!<pattern>'`
through the `run` seam, or filter hunks by path after the fact — pick one, test both shapes) and
count the patch toward the budget after filtering. The brief keeps naming omitted paths.
- Tests: a commit whose only large content is under `fixtures/` fits the budget.
- Size: small.

#### A2. Split authoring is not atomic
The split wrote eight task files, then raised `task file already exists` for the first commit
and left the eight in place. Author into a temp directory, validate every part, then move all
files in one pass; on any refusal nothing is written. A re-run over a range where some commits
already have tasks must skip those (say so) rather than fail.
- Tests: refusal midway leaves the board untouched; re-run skips existing.
- Size: small.

#### A3. Skip commits that need no reviewer
Three of eight parts were docs-only (baseline logs, an observations note). A commit whose
changed paths are all under `docs/` or are `*.md` gets no review task by default; list them in
the range summary as `docs-only, not reviewed`. `include_docs: true` overrides.
- Size: small.

#### A4. Review task ids and briefs carry a double prefix
Generated brief text reads "task review-review-ff-glm-r6-…": `review_brief_text` prepends
`review-` to an id that already has it. Fix the one place; test the brief opening line.
- Size: trivial.

## C — out of scope
Same as handoff 1.

## Definition of done
As handoff 5.
