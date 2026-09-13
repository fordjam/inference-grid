# Handoff brief 11 — Grid backlog for GLM (interactive ZCode or Claude Code on the Z.ai plan)

Follows `docs/handoff-glm-10.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.** Both
items come from the first five cross-repo reviews (factory-frontend, kimi-k3, 2026-09-13 21:07 UTC).

#### A1. Per-commit review packets must stage the file as of that commit
`review-ff-glm-r6-f383c9d` was rejected on one finding that is not a defect: the packet held the
commit's own hunks in `diff.patch` but the file contents at the range **tip**, two rounds later,
so the reviewer compared a test without `pytestmark` (the commit) against the same test with it
(the tip) and reported an inconsistency. In `split: "commit"` mode stage `git show <sha>:<path>`
for each changed path, never the tip; the brief says "files as of this commit". Only the
unsplit (`none`) mode stages the tip.
- Tests: split packet contents equal `git show <sha>:<path>`; unsplit still equals tip.
- Size: small.

#### A2. Detect tool-call markup and refuse it as its own outcome
`review-ff-glm-r6-c8e5c2c` came back as `<|open|>tools<|sep|><|open|>call tool="bash"…` — the
model emitted a tool-calling transcript instead of the JSON verdict; the schema test failed it
correctly but the board only says `failed_tests`. In `lanes/go.py`, a reply whose content starts
with `<|` or contains `call tool=` is a `tool_markup` refusal carrying the first 120 chars, the
attempt is held with that reason, and `board-status`/`--suggest` treat it like a
reasoning overrun (retryable). Add to every generated review brief — `review_brief_text` and
`branch_review`'s — the sentence: "You have no tools; every file you need is in this message.
Do not emit tool calls." Place it just before the OUTPUT FORMAT rule.
- Tests in `tests/test_lane_go.py` (markup → refusal) and brief-text tests.
- Size: small.

## Definition of done
As handoff 5.
