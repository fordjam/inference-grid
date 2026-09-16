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

#### M2. Lane evals as a nightly suite, not a one-off calibration
`board/calibration.py` scores reviewers on a seeded-defect corpus. Generalise it into
`inference-grid evals --json {corpus_dir, lanes, board_dir}`: a corpus directory holds
cases of three kinds — `review` (seeded defects, recall/precision as today), `packet` (a
small brief with a hidden reference patch and its tests: accepted when the lane's commit
passes the reference tests without touching them), `plan` (a ticket with a reference packet
outline: accepted when the drafted packet names the same files and tests). The command
authors one task per (lane, case) on the board with category `calibration_run`, and
`board/calibration.py`'s scoring is extended per kind. Results land in the scorecard as
`(family, model, category=eval:<kind>)` rows that `route`'s blend already reads. The operator's
private corpus stays where it is (`~/.config/inference-grid/calibration/`); the repository
ships `calibration/example/` cases for all three kinds. Tests: each kind scores a passing and
a failing fake lane correctly; the authored tasks validate; scorecard rows carry the kind.
- Size: large (split at the kind boundary if needed: M2a review+packet, M2b plan+CLI).

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
