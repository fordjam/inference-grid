# Handoff brief 8 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-7.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.**
Do handoff 7 first (A3 gates everything). This brief is what lets the Grid review work from
*other* repositories, so cross-family review stops needing the coordinator's own quota.

---

#### B1. `board-new --review-branch`: a review task from a git range
Today a cross-family review of a branch in another repository (e.g. factory-frontend
`glm/r6-followups`, 5 commits) means the coordinator hand-authors a task, stages files and writes
a brief. Add `--json {board_dir, project_root, review_branch: {repo, base, tip}, lanes?, budget?}`:
- run read-only `git -C <repo> diff --name-only <base>..<tip>` and `git log --format=%H%n%an%n%(trailers)`
  through one injectable `run` seam (tests fake it);
- refuse if any changed path matches `board.guard`'s credential patterns or is outside
  `src/`, `api/`, `web/src/`, `tests/`, `tools/`, `docs/`, `scripts/`, `grid/` (a small allow-list
  constant; the coordinator extends it);
- stage the *tip* versions of the changed files plus a generated `diff.patch` under
  `grid/board/review/<task-id>/`, write the task with `inputs` = those staged paths, `tests` =
  `grid/tests/test_review_schema.py`, `artifacts` = `reply.txt`, `category` = `independent_review`;
- set `author_family` from the commits' `Co-Authored-By` trailers using one constant mapping
  (`GLM-5.3-Flash` → `glm`, `Claude` → `claude`, `Codex`/`GPT` → `openai`, `Kimi` → `kimi`,
  `DeepSeek` → `deepseek`); mixed families → refuse and say so; no trailer → `claude`;
- generate the brief from `review_brief_text` with the range, commit subjects and the
  instruction that findings must quote the diff; size hints advisory (handoff-5 B3);
- task id `review-<repo-basename>-<tip-short>`; refuse if it exists.
- Tests: temp git repo with two commits and a trailer; allow-list refusal; mixed-family refusal;
  the staged tree and brief content.
- Size: medium.

#### B2. A second board in the same tick
`boards/*.json` already lists one config per project and `board-tick-all.py` loops them, but
the packaged `board-tick` takes one config. Accept `boards: [config, …]` in the JSON and tick
each in order with a shared readiness view (busy counts carry across boards — a lane busy on
board A is busy on board B). Report per board.
- Tests: two temp boards, one lane of concurrency 1: second board's task reports `lane_busy`.
- Size: small.

#### B3. `evaluation` writes `docs/EVALUATION.md` on request
Handoff-4 B2 renders the scorecard; add `--json {out: "docs/EVALUATION.md", replace_section:
"## Scorecard"}` that replaces exactly that section (creating it at the end if absent) and
leaves the hand-written parts intact.
- Tests: section replaced; other text byte-identical; absent section appended.
- Size: small.

#### B4. README "What works today" from the record
`README.md` still says "experimental alpha … orchestration core". Rewrite that section from
`docs/CONTRIBUTIONS.md` and `docs/BOARD.md`: the board runner, packaged lanes (zai, zcode, go,
go-kimi, cline, goat), cross-family review, retry, `board-status`/`--suggest`, `board-tick
--dry-run`, the first automated inbox landing (2026-09-13). Facts only, each traceable to a
CONTRIBUTIONS row; no claims about providers that have not completed an attempt.
- Size: small. No tests; the coordinator reads it.

## C — out of scope
Same as handoff 1. Creating `boards/factory-frontend.json` is the operator's (it lives under
`~/.local/share`).

---

## Definition of done
As handoff 5.
