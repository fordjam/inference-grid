# Integrating `glm/board-audit` — readiness check, 2026-09-13

State on 2026-09-13, before any merge. Verified, not performed: this document
does not merge anything.

## Diff

```
$ git diff --stat main..glm/board-audit    (tail)
 tests/test_board_audit.py             |  881 +++++++
 tests/test_capacity_report.py         |  483 ++++
 tests/test_programme_status.py        |   97 +
 17 files changed, 10079 insertions(+), 14 deletions(-)
```

The branch adds two new scripts (`scripts/board_audit.py`,
`scripts/capacity_report.py`), their three new test files (the only files
touched under `tests/`), a small Evidence-section change to
`scripts/programme_status.py`, and six documents. The 14 deleted lines are
inside `scripts/programme_status.py` (the capacity()/render() rework).
Nothing under `src/`, `strategy-lab/`, or the dispatchers is touched.

## Fast-forward

```
$ git merge-base --is-ancestor main glm/board-audit && echo ff
ff
$ git merge-base main glm/board-audit   # == main's tip
c401181
```

`main` has not moved since the branch was cut (branch point == `main` tip ==
`c401181`), so the merge is a pure fast-forward: no merge commit, no
conflicts possible.

## Test counts

| tree | default suite (`uv run pytest -q`) | collected |
| --- | --- | --- |
| `glm/board-audit` (this tree) | **1,316 passed, 2 skipped** | 1,318 of 1,359 (41 integration/slow deselected by `addopts`) |
| `main` | **1,229 passed, 2 skipped** | 1,231 |

`main`'s counts are derived, not guessed: the branch's only changes under
`tests/` are the three new files above, which collect 87 tests, and `main`
equals the branch point — so main collects 1,318 − 87 = 1,231, of which the
default suite runs 1,229. That reproduces exactly the 1,229-test baseline
handoff 1 recorded, which is the cross-check.

## The one command the operator runs

```
git checkout main && git merge --ff-only glm/board-audit
```

`--ff-only` is the safety: if `main` has moved by the time this runs, the
command refuses instead of merging, and this document should be regenerated
first.
