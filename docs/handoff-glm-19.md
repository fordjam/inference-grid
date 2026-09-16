# Handoff brief 19 — reviews that fit the reviewer (any lane, board-dispatched)

Follows `docs/handoff-glm-18.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.**
Dispatched as `packet` tasks by the board. Branch from `glm/work`.

## Why this brief exists (2026-09-16)

travel-brain's main is one 51-file landing commit (672 KB staged, ~168K tokens). The
branch reviewer refuses a packet that size by design (the proven reviewer takes ~24K tokens)
and its only split is per commit, so the operator forced `max_input_bytes` up — and the
review then failed on both adapters (`adapter exited without a successful terminal receipt`
on cline and go-kimi). The refusal was right; the missing tool is a split that is not by commit.

---

## Phase K

#### K1. Branch reviews split by path group when commits cannot split them
`board/branch_review.py::review_branch` gains `paths: [<prefix>, ...]` (an explicit
filter: only changed files under those prefixes are staged, the diff patch is restricted to
them) and, when `split` is requested and the range is a single over-budget commit, an
automatic split into one task per top-level directory of the changed files (`src/`,
`tests/`, `docs/`, `scripts/`, `migrations/`...), each measured against the budget before
anything is written and each named `review-<repo>-<sha>-<dir>`. Directories that still
exceed the budget are refused with their size, never forced. `board-new --json` exposes
`paths`. The brief text names the group under review and lists the sibling reviews so the
reviewer knows what it is not seeing.
- Tests (`tests/test_branch_review.py`): a one-commit range over budget with `split` yields
  one task per directory, each within budget; `paths` restricts staging and the patch; an
  over-budget directory is refused with its byte count; sibling reviews are named in the brief.
- Size: medium. Why: the first whole-repository review the operator authored could not run.

---

## Definition of done, per packet
As brief 15. Report at `docs/reports/brief-19-<packet>.md`.
