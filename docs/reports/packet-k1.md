# GLM-5.3-Flash lane report — packet K1: branch reviews split by path group when commits cannot split them (2026-09-16)

Lane: GLM-5.3-Flash (packet lane). Base: `7bf3a3b` (the tip of `glm/work` at branch
time). Branch: `packet/packet-k1`, one commit, not pushed.

## Why

The first whole-repository review the operator authored could not run. Its range is a
single commit, so the per-commit split — the only tool `review_branch` had when a packet
blew the byte budget — had nothing to divide: the whole-range packet refused, the
per-commit split refused on the same bytes, and the review never happened. The packet
also needed the opposite knob: sometimes the operator wants only part of a range
reviewed, and there was no way to say so.

## What landed

**`src/inference_grid/board/branch_review.py`** — two changes, both in the existing
authoring path, no new module, provider-authored files untouched.

- **`paths`** (spec key on `review_branch`, so `board-new --json` exposes it; also a
  `review_branch(..., paths=)` kwarg, mirroring how `scope` is accepted): an explicit
  filter. Changed files are filtered to those under the given prefixes before anything
  else happens — staging in every mode (whole range, each split commit, `scope:
  "branch"`'s merged tree) and the diff patch, via a new `prefixes` argument on
  `_filter_patch` that drops every `diff --git` block outside the prefixes. Entries are
  validated as safe relative path strings (`_safe_rel`); a range with no changed file
  under the filter refuses, and a split commit whose files all fall outside it is listed
  in the result as `filtered_out` (`no changed files under the path filter`). The brief
  says what was asked for: "The review was asked for the path group(s) …: only changed
  files under them are staged, and diff.patch covers only them."
- **The directory split**: when a split's commit packet alone exceeds the budget, the
  commit now divides again, one task per top-level directory of its changed files
  (`_path_groups`: `src/`, `tests/`, `docs/`, `scripts/`, … sorted; root-level files are
  one `root` group keyed by their own names, since there is no directory prefix to hand
  the patch filter). Task ids are `review-<repo>-<sha>-<dir>`. Each directory's packet —
  its staged files as of the commit plus its own hunks — is measured against the budget
  before anything is written, in the same plan-then-write discipline the commit split
  already uses: a directory that still exceeds the budget refuses the whole split with
  its byte count and file sizes, never forced, and the board is left untouched. A
  single-directory commit keeps the plain refusal with its file sizes, so the existing
  over-budget refusals read exactly as before. Every directory-split brief names the
  group under review ("restricted to the src files the commit changed") and lists the
  sibling reviews by task id, so the reviewer knows what it is not seeing and where a
  finding outside its group belongs.

`_filter_patch(patch, attrs, prefixes=None)` is the shared mechanic — the `paths` filter
and the per-directory packets both narrow the patch through it, so hunks outside a packet
never count against its budget. `_review_branch_scope` also honours `paths`, for the
merged-tree packet.

## Tests

`tests/test_branch_review.py`, +4 (35 total, all offline):

- `test_a_single_over_budget_commit_splits_by_top_level_directory` — a one-commit range
  over budget with `split: "commit"` yields one task per directory, each packet within
  the budget, each staging and diffing only its own group; the brief names the group and
  carries the sibling task id.
- `test_paths_restrict_staging_and_the_patch` — with `paths: ["src/"]` in the spec, the
  docs/ file is neither staged nor in diff.patch; the brief names the restriction.
- `test_paths_entries_are_validated` — unsafe entries, non-string/empty entries and a
  filter nothing matches all refuse with the reason.
- `test_an_over_budget_directory_is_refused_with_its_byte_count` — one directory still
  over budget after the split refuses with its byte count and file sizes, and nothing is
  written (no task files, no staging, no briefs).

## Verification

- Full suite with the packet gate's baseline-aware runner at the `7bf3a3b` base: failure
  set unchanged from the base (the pre-existing sandbox refusals and the deny-read
  collection error, all `inherited`), zero new, zero repaired. `tests/test_branch_review.py`
  31 → 35 passing.
- `ruff format` and `ruff check` clean on the changed files.
- No provider-authored file was touched (`observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
  `lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py` all
  unchanged). No new credential access; no absolute home path or personal e-mail in the
  diff.

## One thing worth the operator's eye

**The directory split divides by top-level directory only.** A commit whose whole change
lives under one directory (`src/` alone at 200 KB) still refuses, now naming the
directory and its bytes rather than silently forcing a packet the proven reviewer cannot
take — that is the "never forced" half of the packet, and it means the whole-repository
review that motivated this packet runs only if its commit's changes actually spread
across directories small enough to fit. If a real repository trip-wires on this, the next
step is recursive splitting under the offending directory, which this packet deliberately
does not attempt.
