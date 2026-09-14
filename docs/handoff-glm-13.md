# Handoff brief 13 — the Grid across every provider, model, board and runner (GLM, interactive ZCode / Z.ai)

Follows `docs/handoff-glm-12.md` (v0.1.0a5 released 2026-09-13). **Section 1 of `docs/handoff-glm.md`
applies unchanged** — and matters more here: every item below touches lane plumbing, and none
of it may read `lanes.json`, a credential, or `~/.local/share/`. Where an item needs a lane
record or a console setting, it stops at "operator step" and says exactly what the operator
runs. Tests use fake CLIs, fake `send`, temp ledgers.

State on 2026-09-13 (from the ledger and the tree, not the docs):

| Surface | Exists | Registered / running | Scorecard evidence |
| --- | --- | --- | --- |
| Lane kinds | `go_http`, `goat_cli`, `cline_cli`, `claude_headless`, `zcode_cli` | — | — |
| Lanes | modules for go, goat, cline, zai, zcode | ledger lanes: `go`, `go-kimi`, `zai`, `zcode`; accounts: go, goat, zai, zcode (no cline) | glm (4 categories), kimi (review only) |
| Providers with no kind | Codex (OpenAI), first-party Claude Max, DeepSeek/Qwen/Grok/MiniMax on Go | — | — |
| Boards | grid, factory-frontend, monarch | ticked by hand | — |
| Runners | `board-tick`, `board-tick-all.py`, `--dry-run` | launchd plist rendered, **not loaded** | — |

The order below is the order of value: each phase makes the next cheaper to prove.

---

## Phase L — every lane registered and canaried

#### L1. `lane-init`: a canary board for any lane id
`inference-grid lane-init --json {lane_id, board_dir}` writes a one-task `canary` board
(`canary-<lane>`: brief "Reply with exactly OK", artifact `reply.txt`, test asserts the
content) and prints the `board-tick --dry-run` and `board-tick` commands. The canary is the
only task allowed to run on an unqualified lane. Records the outcome in the lane's scorecard
under `canary`.
- Tests: board file shape; the runner dispatches a canary on a lane whose readiness is
  `unverified` and nothing else.
- Size: small.

#### L2. Register goat and cline
Both modules are accepted (`lanes/goat.py`, `lanes/cline.py`) and neither is a lane. Write the
two `lanes.json` entries **as documentation only** in `docs/LANES.md` (provider, family, model,
kind, executable, window, categories — no credential path values, just the key name), add the
`cline` account alias to `ledger.init` defaults, and extend `doctor` to list lanes whose module
exists but whose record is absent ("module without lane"). Operator step: paste the entries,
run L1 for each.
- Size: small.

#### L3. A `codex_cli` lane kind
There is no OpenAI lane. Add `lanes/codex.py` for the Codex CLI in non-interactive mode
(`codex exec` or whatever the installed CLI's headless invocation is — read `codex --help`
through the fake-CLI seam in tests; document the real invocation in `docs/LANES.md` and let the
operator confirm it), with the same contract as `zcode.py`: attempt request on stdin, write
sandbox, wall deadline, snapshot supervision, terminal receipt, `classify_codex` from the CLI's
event stream. Family `openai`. This is the lane that lets **T1** packets (vix-rs, COT) run
through the Grid under the existing family rules.
- Tests: fake CLI happy path, deadline hold, refusal on unexpected model, receipt shape.
- Size: medium–large.

#### L4. First-party Claude lane
`claude_headless` exists (used for Z.ai). Add a documented lane spec `claude` with the
Anthropic-native base URL and the Claude Code CLI on the operator's Max login (kind
`claude_headless`, family `claude`), and a `thinking_tokens` → `--max-thinking-tokens` (or
the CLI's equivalent) mapping alongside the effort mapping in `go.py`. Reviews by this lane are
the programme's T1 acceptance family; the runner must never pick it for a task whose
`lanes` do not name it explicitly (add `explicit_only: true` to the lane view for family
`claude` and `openai`; tests on `select_lane` inputs, not on `select.py` itself).
- Size: medium.

#### L5. Every Go model as a lane, gated by evidence
Go serves 50+ models under one credential. Add `docs/LANES.md` entries for the Go models worth
qualifying (`deepseek-v4-flash` after the console opt-in, `qwen3.8-max`, `glm-5.3-flash` on Go
as a *second* GLM route, `minimax-m3`, `grok-4.6`), each with family, and extend
`REASONING_EFFORT` from the endpoint schema per model. Selection must refuse a lane with no
`canary` row in the scorecard for that model (`unqualified`), so registering a model costs
nothing until it earns evidence.
- Tests: a lane with a model absent from the scorecard is `unqualified`; a canary success
  qualifies it.
- Size: medium.

## Phase Q — qualification and scorecard as the admission gate

#### Q1. Category qualification
A lane is qualified per **category**, not globally: `independent_review` needs ≥ 3 accepted
reviews; `pure_function` ≥ 2 accepted; `tests_multi_file` ≥ 2; `canary` ≥ 1. `readiness_view`
carries `qualified_for: [...]` and `select_lane` inputs exclude lanes unqualified for the
task's category. `evaluation` prints the matrix (family × category → attempts / accepted /
qualified).
- Tests on the readiness view and the matrix.
- Size: small–medium.

#### Q2. Qualification packets
`board-new --qualify {lane_id, category}` authors the smallest standard task for that category
from a fixed set in `grid/qualification/` (a pure function with 5 tests; a two-file test
task; a synthetic review with seeded defects — the one used on 2026-09-10 for GOAT/Cline
calibration), so every new lane earns its rows the same way.
- Size: medium.

## Phase B — a board for every project

#### B1. `board-init`
`inference-grid board-init --json {project_root, board_name?}` creates `grid/board/`,
`grid/briefs/`, `grid/tests/test_review_schema.py`, a `grid/README.md` naming the board's
rules (allowed input prefixes, T-tier of the repo), and prints the `boards/<name>.json`
content for the operator to place under `~/.local/share/inference-grid/boards/`. Refuses if
the project has no git repository.
- Tests: temp project; idempotent second run.
- Size: small.

#### B2. Boards for the T0 projects
Run B1 for `yt-research-mcp` (package only; `strategy-lab/` excluded by the README rules) and
`insta-saved`, and author their first review tasks from `--review-branch` on their most
recent GLM branches. Operator: place the two board configs.
- Size: small.

#### B3. T1 boards, gated on L3/L4
`vix-rs` and `COT` boards may be created only after a `claude` or `openai` lane is
qualified for `independent_review`; their `grid/README.md` must state the input allow-list
excludes `data/`, `research/` findings and anything the repo's own rules freeze. Prepare the
READMEs and allow-lists now; do not create the boards.
- Size: small.

## Phase R — runners that run themselves

#### R1. One scheduler tick
Fold `board-tick-all.py` into the package: `inference-grid tick-all --json {boards_dir}` —
prepare (capacity refresh), then `board-tick` with the boards list (handoff-8 B2) ordered by
each board's declared priority, honouring campaign windows (ZCode-class work only inside the
Z.ai window), Cline only after a reset, GOAT only for `tests_multi_file`/`fixtures_multi_file`.
Emits one JSON line per board to a log path given in the JSON. Render the launchd plist
from a template with the interval as a parameter (`launchd/templates/`, same pattern as
vix-rs), never install it.
- Tests: ordering, window gating, the log line.
- Size: medium.

#### R2. Operator digest
`inference-grid digest --json {boards_dir, since}` prints one page: per board — accepted /
passed / blocked / ready counts with the oldest ready task's age; per lane — attempts,
holds, refusals by reason, quota used; the retries `--suggest` would author; the inbox
landings awaiting `inbox-integrate`. Markdown to stdout; this is what the operator reads
each morning instead of `board-status` per board.
- Size: small–medium.

#### R3. `inbox-integrate` apply
Handoff-9 B1 was dry-run only. Implement `dry_run: false`: create `integrate/<task-id>` from
the project's `HEAD`, cherry-pick the inbox commit, run the task's declared tests, and leave
the branch for the operator — never touch `main`, never push. Refuse if the dry-run reports a
conflict.
- Tests: clean case creates the branch with the tests green; conflict case creates nothing.
- Size: medium.

## Phase O — operations

#### O1. `doctor` end to end
`doctor` reports, in one call: every lane kind vs registered lane vs account alias vs
scorecard qualification; every board dir vs `boards/*.json` config; the launchd templates vs
what is loaded (read-only `launchctl list` through the `run` seam); the packet store size and
oldest held attempt. Anything missing is a named line, not silence.
- Size: small–medium.

#### O2. Retire the yt-research dispatch stack (design note only)
`~/projects/yt-research-mcp/scripts/{capacity,routing,runner,codex_dispatch,opencode_dispatch}`
predate the Grid and read a dead ledger. Write `docs/MIGRATION_YT_RESEARCH.md`: what each
script does, which Grid feature replaces it, what has no replacement yet, and the order to
retire them. No edits in that repository.
- Size: small.

## Operator steps this brief cannot do (collected)
1. `lanes.json` entries for goat, cline, codex, claude, go-deepseek and any further Go model —
   from `docs/LANES.md`, credential paths yours.
2. Console: OpenCode Go "Enable models hosted in China" for `deepseek-v4-flash`.
3. Place `boards/*.json` for yt-research-mcp and insta-saved (and later vix-rs, COT).
4. Load the rendered `tick-all` launchd plist.
5. Run `lane-init` canaries, then `--qualify` packets, for each new lane.

## Definition of done
As handoff 5, per item. Report at the end which phases are complete, which items are blocked
on an operator step, and the exact commands for step 5.
