# Handoff brief 12 — Grid: prepare release v0.1.0a2 (GLM, interactive ZCode / Z.ai)

Follows `docs/handoff-glm-11.md` (10 A1–A4, 11 A1–A2 done 2026-09-13, `ca6103e`…`816fc06`; 350 tests).
**Section 1 of `docs/handoff-glm.md` applies unchanged** — in particular: no tag, no push, no
upload. You prepare; the operator releases.

The alpha release on GitHub is `v0.1.0a1`. Since then: the board runner, six packaged lanes,
cross-family review with retry, `board-status`, `--dry-run`, `--review-branch` with budgets and
splits, `evaluation`, `inbox-integrate`, the first automated inbox landing and the first
cross-repository reviews. None of that is released.

#### A1. `--review-branch`: `exclude_commits` and document `max_input_bytes`
Commit `8c2a962` (mechanical marker moves across many test files, 159 KB) needed a raised budget;
the operator first put `max_input_bytes` inside `review_branch` where it is ignored. Add
`exclude_commits: [sha, …]` (listed in the range summary as `excluded by operator`), and make
the CLI refuse unknown keys inside `review_branch` naming the accepted ones. Document both in
`docs/BOARD.md`.
- Size: small.

#### B1. CHANGELOG from the record
Write `CHANGELOG.md` (Keep-a-Changelog shape): an `Unreleased` → `0.1.0a2` section built from
`docs/CONTRIBUTIONS.md` and `git log v0.1.0a1..HEAD` — Added / Changed / Fixed, one line each,
every line traceable to a commit hash. No marketing.
- Size: small.

#### B2. Offline install check
From a clean temp venv (`python3.12 -m venv`), `pip install` the wheel built by
`python -m build --wheel` (add `build` to the dev extras if absent), then run
`inference-grid doctor` and `inference-grid board-tick --dry-run` against a temp board and
temp ledger. Everything the README quickstart claims must work from the wheel alone. Fix
`MANIFEST.in`/`pyproject.toml` includes as needed; record the exact commands and output in
`docs/RELEASE_CHECK_0.1.0a2.md`.
- Tests: `tests/test_packaging.py` asserts the wheel contains every `src/inference_grid` module
  and the lane runner entry point.
- Size: small–medium.

#### B3. `docs/EVALUATION.md` regenerated
Run `inference-grid evaluation` with `replace_section` (handoff-8 B3) against the live ledger
path given on the command line and commit the result; keep the hand-written parts.
- Size: trivial.

#### B4. Version bump and release notes
`pyproject.toml` version → `0.1.0a2`; `docs/RELEASE_NOTES_0.1.0a2.md` = the CHANGELOG section
plus the two lane facts operators need (kimi-k3 needs `reasoning_effort`; DeepSeek V4.1 Flash
needs the Go console opt-in). Do not tag.
- Size: trivial.

## Definition of done
As handoff 5. Report the exact `git tag` and `gh release` commands the operator should run.
