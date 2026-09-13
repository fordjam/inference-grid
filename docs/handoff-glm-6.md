# Handoff brief 6 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-5.md` (A1–A2, B1–B3 done 2026-09-13, `95fa5b2`…`2d2f428`; 299 tests).
**Section 1 of `docs/handoff-glm.md` applies unchanged.** One item, then `docs/handoff-glm-4.md`
(B1–B3 there are still open) in the same session.

---

#### A1. Send the reasoning effort the endpoint actually honours
Handoff-5 B1 established the lane fact: the Go endpoint has no token-count reasoning field for
kimi-k3, only `reasoning_effort` ∈ {`low`, `high`, `max`}, **default `max`** — which is why a
24k-token review packet thought 16,000 tokens away. Capping `max_tokens` alone just fails
sooner. The coordinator's policy for mapping a task's `thinking_tokens` to an effort tier is:

| `thinking_tokens` | `reasoning_effort` |
| --- | --- |
| absent or ≤ 4 000 | `low` |
| 4 001 – 12 000 | `high` |
| > 12 000 | `max` |

Board review tasks carry 6 000 → `high`. Implement in `lanes/go.py::run`. `lanes/config.py`
(provider-authored, integrated unmodified) validates `lanes.json` against an exact key set, so
no new lane key: keep the capability in `go.py` as one named constant mapping model id →
accepted `reasoning_effort` values (`kimi-k3` → `("low", "high", "max")`; every other model →
none until the operator adds it, with the endpoint schema reference in the comment). When the
lane's model is in the map, send the mapped value in the request body and record
`reasoning_effort` in the verdict; otherwise send nothing and record
`reasoning_effort: unsupported`. Keep the `max_tokens` cap from handoff-5 B1. The threshold
table is one named constant with the policy comment; do not spread thresholds through the
code. Document both constants in `docs/BOARD.md` under lane kinds.
- Tests in `tests/test_lane_go.py` with an injected `send` asserting the body at each threshold
  and the unsupported case.
- Size: small.

Then continue with `docs/handoff-glm-4.md` B1–B3 (`board-tick --dry-run`, `evaluation`,
`board-status --suggest`).

## Tier 3 — coordinator
After A1 lands: `board-new --retry review-board-runner-3-2`
and one tick. That review has now failed four times (two wall timeouts, one overrun, one
reasoning-at-max); with `high` it should complete.

## C — out of scope
Same as handoff 1. `lanes.json` stays the operator's.

---

## Definition of done
As handoff 5: tests, full suite, `.venv/bin/ruff check src tests`, one commit per item with your
trailer, a `docs/CONTRIBUTIONS.md` row, no push.
