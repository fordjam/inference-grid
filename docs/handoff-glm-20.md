# Handoff brief 20 — the grid runs unattended overnight (any lane, board-dispatched)

Follows `docs/handoff-glm-19.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.**
Dispatched as `packet` tasks by the board. Branch from `glm/work`. **Every packet here must
fit one attempt: the wall clock is 3600 s for the whole attempt, all rounds included.** If an
item cannot be finished, gated and committed inside an hour, stop at a clean commit that
passes the gates and say what is left in the report; a second packet follows.

## Why this brief exists (2026-09-16, 01:30 UTC)

The operator's instruction: run every plan to 100 %, roll to the next provider at 100, and
fix what stops lanes from running without failing. Tonight's observed failure modes, each
with a packet below: a board whose quota observation aged past 900 s during a long pass is
refused `quota stale` (J5, J6, J7 lost two passes each); a board row whose attempt died with
the runtime stays `dispatched` for ever (hand-reset three times); a cline_cli lane spent a
full hour on J4 producing no tree change and nothing said so until the wall; the
feed at `:8020/api/usage` answers `{"accounts": []}` while the overlay has six providers;
the cline-http lane never passed its canary; and the attempt wall itself (one hour) is the
wrong bound for a packet whose gates alone take three minutes.

---

## Phase L

#### L1. A stale observation is re-read at admission, not refused
`board/runner.py::tick` refuses a lane whose ledger record is older than
`quota_freshness_seconds` with `quota stale`. The observation files the collectors write
(`<output_dir>/<provider>-observation.json`, paths in the tick config's `observations` map
— add that map, defaulting to the capacity output directory) are usually fresher than the
ledger record, because `board_prepare` runs once per pass and a pass can last an hour. Before
refusing, the runner re-reads the provider's observation file through
`deployments/local/board_prepare.py::configure_observation`'s record builder (import it
through `package_src`, do not copy it), records the fresh lane record in the ledger, and
re-classifies; only a file that is itself older than the freshness window is refused, with
the file's age in the reason. Tests: a stale ledger record with a fresh file admits; a stale
file refuses naming the age; a missing file behaves as today.
- Size: small–medium.

#### L2. A `dispatched` task whose attempt is dead is requeued
`tick` skips every task not in `ready`. For each task in `dispatched`, look up its newest
ledger attempt (`<task id>-<stamp>-<hex>`); if it is terminal and not successful (`failed`,
`abandoned`) and no ACTIVE attempt exists for the task, settle the task `ready`, record a
ledger event naming the attempt and reason, and report `requeued` in the tick's rows. A
terminal *successful* attempt on a still-`dispatched` task is reported, not touched. The dry
run reports what it would requeue. Tests with the `world` fixture: requeued and dispatched on
the same tick; a live attempt untouched; dry run changes nothing.
- Size: small.

#### L3. An idle agent round ends early with `agent_idle`
`lanes/packet.py::build_loop` runs a round until the adapter exits or the wall. Add an idle
watchdog: if the clone's tree (`git status --porcelain` plus `HEAD`) is unchanged for
`idle_seconds` (default 900, from the task spec) *and* the transcript has grown by fewer than
2 KB in that window, terminate the round with `agent_reason: agent_idle`, run the gates as
usual, and let the loop's next round re-enter with the idle noted in the prompt ("the previous
round produced no change in 15 minutes; commit what you have or say why"). Two idle rounds in
one attempt hold the attempt `agent_idle` so the operator sees it, instead of `wall_deadline`
an hour later. Tests: a fake adapter that sleeps is cut at the idle window; one that keeps
writing is not; the verdict names `agent_idle`.
- Size: medium. Why: J4 on cline-deepseek and the first Q5 burned an hour each.

#### L4. The feed reports accounts again
`deployments/local/capacity_feed.py` serves `/api/usage` from the overlay; tonight it answers
`{"accounts": [], "attempts": []}` while `overlay.json` carries six providers. Find the
divergence (path, schema key, or a stale process reading an old file), fix it, and add a test
that serves a fixture overlay and asserts the accounts round-trip. The local dashboard's
`/api/overlay` is unaffected; this is the feed the phone reads.
- Size: small.

#### L5. The attempt wall grows with the packet's gates
`board/task.py` bounds `wall_seconds` at 3600 (provider-authored; do not edit) and the ledger
bounds `timeout` at 3600. For `packet` tasks, `board/packet_task.py::dispatch_packet` computes
the attempt's wall as `wall_seconds + sum(gate timeouts) + 60 × max_rounds` — the agent's hour
is the agent's; the gates and the re-entry overhead are not charged to it — and passes that
to `build_loop` and to the ledger spec (raise the ledger's admission bound for `packet`
specs only, in `ledger.py::_check_spec`, to 4 × 3600, with a test that a non-packet spec is
still bounded at 3600). The runtime's `tick_timeout` (8 h 30) already covers it. Tests: a
packet with three 600 s gates and 3 rounds gets 3600 + 1800 + 180; a review task is
unchanged.
- Size: small.

#### L6. cline-http passes its canary, or says why it cannot
The `cline-http` lane (Cline pass over the Go-style HTTP adapter, model `z-ai/glm-5.3-flash`)
has never passed `canary-cline-http`. Run the canary through `board-tick --dry-run` and the
lane's `go.py` path with a fake `send`, find the refusal (endpoint, model id namespace —
Cline pass bills vendor ids to credits and needs `cline-pass/<model>` — or headers), fix it
in `lanes/go.py` under the existing provider switch, and make the canary pass in a test with
a recorded response shape. If the endpoint genuinely does not serve chat completions for
this id, the report says so with the response, and the lane is marked `explicit_only` in
`docs/LANES.md`.
- Size: small–medium.

#### L7. `opencode_cli` runs packets: an implementer lane on the Go plan
OpenCode Zen documents that all its models are hosted in the US under a zero-retention policy
(`opencode.ai/docs/zen`), and the Go plan's table marks GLM-5.3-Flash, Kimi K3 and Qwen3.8
Max "0 days / not used for training" — the only lanes the operator can point at a T1
repository (COT) that requires US/EU hosting and zero retention. Today the Go lanes are
`go_http` (reviews only). Add an `OpenCodeAdapter` to `lanes/packet.py` beside
`CommandCodeAdapter` (`opencode run --model <provider/model> --format json`, non-interactive,
session id from the JSON stream; `resume` via `--session <id>` where the CLI supports it,
else a fresh session as `ClineAdapter` does), add `opencode_cli` to `PACKET_KINDS`, the
sandbox write roots for its state directory (`.opencode/` under the clone), and the
`lanes/config.py`-compatible lane shape documented in `docs/LANES.md` with a
`go-opencode` example (`model: opencode/kimi-k3`, `residency: us`, `retention: zero`,
`retention_source: <url>`). Tests mirror `test_cline_cli_runs_packets_as_fresh_sessions`:
argv shape, session id extraction from a recorded stream, and a fake-CLI packet round trip.
The lane itself is the operator's to add to `lanes.json`; the report says the exact entry.
- Size: medium.

#### L8. Boards that require hosting and retention guarantees
`lanes/config.py` admits exactly its required keys, so lane facts that are policy rather than
transport live in a sidecar the operator keeps beside `lanes.json`: `lanes-meta.json`
(`{"lanes": {"<lane id>": {"residency": "us|eu|unknown", "retention": "zero|days|unknown",
"retention_source": "<url or note>"}}}`, missing lanes = unknown). A board's tick config may
carry `require_lane_meta: {"residency": ["us", "eu"], "retention": ["zero"]}`; `route` drops
every candidate whose sidecar record does not satisfy every listed key (unknown never
satisfies) with reason `lane_policy` naming the key, and the dry run shows it. The sidecar
is read once per tick beside `lanes_path`; a malformed sidecar refuses the whole tick with
the parse error (fail closed). `inference-grid lane-init` and `docs/LANES.md` document the
file. Tests: a board requiring us/zero offers only tagged lanes; unknown is dropped with the
key; a malformed sidecar refuses; no sidecar and no requirement behaves as today.
- Size: small–medium. Why: COT's board rules require US/EU hosting and zero retention; today
  that is enforced only by which lanes a task names.

## Phase M — the grid finds its own work

#### M1. A held attempt drafts its own fix packet
When a `packet` attempt settles `held` or `blocked` with a reason the operator owes nothing
for (`wall_deadline`, `agent_idle`, `rounds_exhausted`, a gate that failed on the same test
three rounds running, a transport refusal that repeated), the runner authors **one** `plan`
task (J1's plan node) on a `tier: plan` lane whose ticket is built from the verdict: the
task's packet section, the last round's gate tails, the last 4 KB of the transcript, and
the question "what change to the packet, the gates or the harness would let this land?".
The plan's `packet.md` becomes a ready `packet` task only when the board sets
`auto_dispatch: true`; otherwise it waits for the operator, listed under *needs-you* with the
drafted title. No plan is drafted twice for the same task + reason. Tests: a held attempt
yields exactly one plan task with the verdict in its ticket; a second identical hold does not;
`auto_dispatch` gates the build.
- Size: medium.

#### M4. Eval cases for review and packet kinds, scored like calibration
`board/calibration.py` scores reviewers on seeded-defect cases (recall / precision, accepted
= every seeded defect recalled and no false positive). Generalise the *case* and the *scoring*
to two kinds, without touching the CLI yet:
- `calibration/case.py::load_case(dir)` reads `case.json` with `kind: review | packet`.
  A `review` case is exactly today's shape (seeded defects, brief, inputs). A `packet` case
  adds `reference/`: a hidden patch (`reference.patch`) and its tests (`reference_tests/`);
  the lane sees the brief and the repository at `base`, never `reference/`.
- `calibration/score.py::score(case, attempt_dir)`: review kind as today; packet kind is
  *accepted* when the lane's commit (the packet branch head) passes every file in
  `reference_tests/` copied over the lane's tree **and** the lane's diff touches none of the
  reference test files; *repairs* counts the reference tests that fail. Both kinds return the
  same record `{kind, accepted, recalled, false_positives, repairs, notes}` so the scorecard
  row `(family, model, category="eval:<kind>")` is written by the existing outcome path.
- `calibration/example/` gains one `packet` case (a 30-line function with a bug, a 5-test
  reference suite) beside the two review cases already there; the operator's private corpus
  is untouched.
- Tests: the packet case scores a correct fake fix accepted, a fix that edits the reference
  tests rejected, a partial fix with `repairs=n`; the review case's score is byte-identical to
  `board/calibration.py`'s for the two example cases (a regression pin); the scorecard row
  carries `eval:review` / `eval:packet`.
- Size: medium — one hour if the review kind is a move, not a rewrite. Why: this is the
  measurement the grid's whole routing rests on and today it covers reviewers only.

#### M5. `inference-grid evals`: the nightly run, on the board
Depends on M4. `inference-grid evals --json {corpus_dir, lanes, board_dir, project_root}`
authors one `calibration_run` task per (lane, case) it has no outcome for in the last 7 days
(read from the scorecard's `eval:*` rows), so a nightly launchd job keeps every lane's
evals current without re-running what is fresh. The task carries the case's kind; the
runner's existing `calibration_run` step calls M2a's `score` and records the outcome.
`inference-grid digest` gains an *Evals* section: per lane, per kind, accepted / cases and
the age of the newest result; a lane with no eval in 7 days is listed under *needs-you*.
`deployments/local/install.py` renders a `com.inference-grid.evals` KeepAlive agent that
runs the command hourly against the operator's corpus path from config (`evals_corpus`),
skipping when nothing is due. Tests: the due-set logic (fresh rows skip, stale rows author,
a new lane authors everything); the digest section; the plist renders with the config path.
- Size: medium. Why: evals that run only when someone remembers are not evals.

#### M3. The autonomy policy, written and enforced
`docs/AUTONOMY.md` and `board/policy.py`: what the grid does alone and what waits.
**Alone:** dispatch ready tasks within each account's admission limit; requeue dead
attempts (L2); draft fix packets (M1); land packets whose gates and independent review
passed on boards with `auto_land`; publish observations. **Waits for the operator:** anything
that changes published research numbers, the holdout register, credentials, provider
configuration, deploys, spend beyond the configured limit, and any packet whose brief
section touches files under a board's `owner_only` prefixes (new key in the board config;
default `[]`). The runner refuses to dispatch an owner-only packet and lists it under
*needs-you* with the prefix that matched. Tests: an owner-only packet is refused with the
prefix; the digest lists it; the policy document's table matches `policy.py`'s constants
(a test reads both).
- Size: small–medium.

---

## Definition of done, per packet
As brief 15. Report at `docs/reports/brief-20-<packet>.md`.
