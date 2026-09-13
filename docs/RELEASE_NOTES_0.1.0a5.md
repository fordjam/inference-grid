# Release notes — 0.1.0a5 (2026-09-13)

The board era: bounded provider attempts authored, dispatched, reviewed and integrated from one board. This section is the CHANGELOG entry; the hand-written narrative lives in `docs/CONTRIBUTIONS.md` and `docs/BOARD.md`.

## Added

- Board runner: unattended ticks dispatch ready tasks to selected lanes, run the coordinator's tests on the artifacts, create cross-family review tasks, and land approved work on the project's `grid/inbox` branch — the first automated landing ran 2026-09-13 (`113d427`, `db2cc9f`).
- `board-new`: validated task authoring without overwrites, the authorised retry as one command, and `--review-branch` — independent_review tasks authored from a git range, with byte-budgeted packets (`max_input_bytes`), generated/lock exclusions, per-commit splits, docs-only skip and `exclude_commits` (`5d23a78`, `3834901`, `5a96110`, `be9baf7`, `0e51752`, `816b825`, `c7d7a87`, `5e9151b`).
- `board-status`: one read-only view of a board, resolving live reviews and held attempts down to verdict and bounds; `--suggest` pre-fills retry payloads for transport-dead tasks and skips moved-on chains; `boards: [...]` covers several projects in one call (`18adf19`, `da0971f`, `cb51196`, `31e3b79`, `1703ad9`).
- `board-tick --dry-run`: the dispatch plan — lane or exact skip reason per ready task — before any quota is spent; `boards: [...]` ticks several boards in order sharing busy counts (`dc90166`, `6940d9d`).
- `evaluation`: the scorecard and per-account readiness rendered as a markdown document, with section splice into `docs/EVALUATION.md` on request (`536a6d3`, `73afc37`).
- `inference-grid inbox-integrate`: the landing-to-HEAD checklist — diffstat, `git apply --check`, declared tests, reviewer, exact operator commands — written onto the inbox branch (`bfa1c0d`).
- Cross-family review gate: an approving review from a different model family — never a bare pass — accepts the attempt, resolved through the recorded source link (`77e10c9`, `f247267`).
- Go lane reasoning control: the task's thinking budget caps `max_tokens`, and `reasoning_effort` is sent per the documented endpoint schema with tier policy; `reasoning_overrun`, `tool_markup` and `region_optin_required` are named refusals, and endpoint-refused models are excluded per model for a day (`1a05b81`, `c84bb8d`, `16e9ee5`, `415b391`, `816fc06`).
- Capacity dashboard Evidence section from a sanitized scorecard overlay (`0133841`).

## Changed

- Retry conventions are machine-readable: a `superseded:` blocked_reason closes the predecessor, retry ids count instead of stacking, retrying a review moves its staged source link, and standalone reviews retry as plain copies (`b9ea187`, `95fa5b2`, `77b7fec`, `f247267`).
- Generated review briefs state the recorded policy: size hints are advisory (size-only rejections are tagged `advisory-only`) and the model has no tools (`694a4fe`, `816fc06`).
- README "What works today" rewritten from the contribution record; claims trace to rows (`4ab1038`).

## Fixed

- Declared files sharing a basename (artifact↔test, artifact↔input, test↔test) block before dispatch instead of silently replacing a declared test in the scratch directory (`da85958`).
- A held attempt occupies its account slot: the busy count is re-evaluated after every dispatch, so the next task reports `lane_busy` instead of colliding (`313e90f`, `a1076e6`).
- The Go transport honours the tighter of task and lane budgets, transport-timeout holds name their bounds, and reasoning overruns are distinguished from truncated replies (`cf43143`, `4c7451d`, `1a05b81`).
- A Cline artifact deleted after a complete `iteration_end` snapshot is restored before publication (`293c34d`).
- Split review packets stage files as of each commit rather than the range tip, and authoring is atomic under mid-split refusals (`5f02d46`, `816b825`).

## Operator lane facts

- **kimi-k3 reviews need `reasoning_effort`.** The endpoint's default is `max`, which thought a whole 16,000-token output away twice. The lane sends the effort tier from the task's thinking budget (`lanes/go.py::EFFORT_TIERS`: 6,000-token board reviews → `high`); an operator-added `go-*` lane for kimi-k3 relies on this, and a `finish_reason: length` with no content now surfaces as a `reasoning_overrun` hold instead of a silent burn.
- **DeepSeek V4.1 Flash needs the China-hosting opt-in** enabled for the Go account in the OpenCode console before any attempt can run; until then the lane records `region_optin_required` and excludes the model for a day instead of re-probing. After enabling, add a `go-deepseek` lane to `lanes.json` and canary it.
