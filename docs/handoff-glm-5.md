# Handoff brief 5 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-4.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.**
Items A1–A2 were found by the coordinator on 2026-09-13 while running the first retry round
with `board-new --retry`; the round's outcome is recorded in `docs/CONTRIBUTIONS.md` and may add
more items below it.

---

## A — defects found running `board-new --retry` live

#### A1. `--retry` does not move the source link to the retry review
`retry_task` (handoff-3 B1) supersedes `review-scorecard-summary` with
`review-scorecard-summary-2`, but `grid/board/review/scorecard-summary/source.json` still named
the old review (or, for links written before handoff-2 A2, named nothing), so
`find_source_link` would have fallen through to the prefix rule, looked for a source called
`scorecard-summary-2`, and the approval would have accepted nothing — the exact failure handoff-2
A2 was meant to end. The coordinator edited the link by hand. Make `retry_task` do it: when the
retried task is an `independent_review`, find the link whose `review_task` equals the
predecessor id (or whose directory name equals the prefix-derived source, for legacy links), set
`review_task` to the new id, and append the predecessor to a `review_task_history` list. Refuse
the retry if the source directory is missing.
- Tests in `tests/test_board_new.py`: named link moved; legacy link (no `review_task`) gains
  one; history accumulates over two retries; a non-review retry touches no link.
- Size: small.

#### A2. Retry suffixes stack instead of counting
`next_retry_id("review-board-runner-3")` produced `review-board-runner-3-2`, not
`review-board-runner-4`. A retry of a retry should increment the existing numeric suffix when
the id already ends in `-<n>` and that predecessor is itself a retry (its `blocked_reason` starts
with `superseded:` or its own predecessor exists on the board); a first retry of a plain id still
gets `-2`. Do not rename existing tasks.
- Tests: `x` → `x-2`; `x-2` → `x-3`; the existing legacy id `x-3-2` is left as it is and its
  retry is `x-3-3`. Write the rule in the docstring and test exactly that; the requirement is
  that a chain of retries reads as a sequence.
- Size: small.

## B — what the live round exposed (2026-09-13, 17:22–17:28 UTC)

#### B1. The Go lane sends no reasoning budget, so kimi thinks its whole output away
`review-board-runner-3-2` on `go-kimi` returned in ~5 minutes with `finish_reason: length`:
`completion_tokens: 16000`, `reasoning_tokens: 16000`, **no content**. `lanes/go.py::run` sends a
fixed `max_tokens=16000` and nothing else; the task's `budget.thinking_tokens` (6000) never
reaches the request. The earlier "transport timeouts" on the same packets were very likely the
same overrun hitting the 400 s bound first. Fix at the lane, without touching provider-authored
modules:
- forward the task's `thinking_tokens` in the attempt request (as A3 did for `wall_seconds`) and
  send it in whatever field the Go endpoint honours for the model — read the endpoint's
  documented request schema in `deployments/` or the lane record's `api` notes; if the endpoint
  has no such field for the model, say so in the verdict (`reasoning_budget: unsupported`);
- set `max_tokens` to `thinking_tokens + output allowance` (the reply is a JSON object; 4 000
  tokens of content is generous) rather than a constant;
- classify `finish_reason: length` with empty content as its own refusal
  (`reasoning_overrun`), carrying `reasoning_tokens` and `max_tokens`, so the hold reason and
  `board-status` say what happened instead of "did not stop normally".
- Tests in `tests/test_lane_go.py` with an injected `send` asserting the request body and a
  `length`/empty-content response.
- Size: medium.

#### B2. Busy is evaluated once per tick, so the second task still collides
Handoff-2 A1 made `readiness_view` count held attempts — at tick start. In this round the first
task's attempt went held mid-tick and the second task on the same account was still dispatched
and refused `account busy`. Either re-evaluate the busy count after every dispatch, or dispatch
at most one task per account per tick and report `lane_busy` for the rest. Prefer the first; it
is one query.
- Tests in `tests/test_board_runner.py`: two ready tasks, one lane of concurrency 1, first
  attempt held → second reports `lane_busy`, no ledger refusal.
- Size: small.

#### B3. Review briefs must say that size hints are advisory
`review-scorecard-summary-2` completed cleanly (5.5k reasoning tokens, `stop`) and was
**rejected on one finding: the artifact is 51 lines against the original brief's 45-line
hint**. CONTRIBUTIONS (2026-09-12) already records the coordinator's policy: size hints are
advisory; behaviour and the coordinator tests are the contract. The generated review brief
(`runner.py::review_brief_text`) never says so, so a literal reviewer enforces the hint. Add one
sentence to the generated brief — any line/size/length budget in the original brief is advisory
and exceeding it is not a finding — and, in `parse_review`'s caller, tag findings whose
`expected` or `observed` text mentions only a line/size limit as `advisory` in the blocked
reason so the operator can see a rejection that rests on nothing else. Do not auto-approve;
the retry path (`board-new --retry` with the amended brief) is how the operator answers it.
- Tests: brief text contains the sentence; a rejection whose single finding is size-only is
  blocked with `advisory-only` in the reason.
- Size: small.

## C — out of scope
Same as handoff 1.

---

## Definition of done, per item
1. Tests added or extended; whole suite passes; `.venv/bin/ruff check src tests` passes; do not
   reformat files you did not otherwise touch.
2. `git status --short` shows only files under `src/`, `tests/`, `docs/`, `grid/`.
3. One commit per item, message explaining why, your own trailer; a row in `docs/CONTRIBUTIONS.md`.
