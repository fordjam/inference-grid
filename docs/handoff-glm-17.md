# Handoff brief 17 — the plan node, tiers, drain and failover (any lane, board-dispatched)

Follows `docs/handoff-glm-16.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.**
Dispatched as `packet` tasks by the board. Branch from `glm/work`.

## Why this brief exists (2026-09-15 morning)

Nineteen packets have landed; the board runs, gates, lands and measures on its own. What is
still only the coordinator's: writing the packets (every brief so far was hand-written), the
choice of which model tier does what (implicit — SOTA plans and reviews, workhorses build —
written nowhere), and what happens when a packet exhausts its rounds (an operator re-queues
it). And one operational defect: restarting the `tick-boards` runtime to add two boards
killed `packet-i2` in its third round; the ledger held it correctly, but a config change
should not cost the packet in flight.

Race-N (run every packet on N lanes, first green wins) was considered and **rejected as
wasteful**: our packets already get three gated rounds, and last night's failures were
harness bugs that would have failed identically on every lane. Two narrow forms keep the
value: failover to another family only after a failure (J4), and a second-family review only
for changes the operator marks high-stakes (deferred).

---

## Phase J

#### J1. The plan node: a packet drafted from a ticket, by a lane, from the scout's facts
`board-new --json {ticket: {title, body, repo, paths?}, lane, board_dir}` runs the scout
(`lanes/scout.py::orient`, code) over the named paths (or paths guessed from the body by
plain grep of identifiers), then dispatches **one** `plan` task on `lane` whose only artifact
is `packet.md`: a packet section in the brief format (`#### <id>. <title>`, location,
acceptance tests, size, operator step) plus a `gates` list and a `tests` list as a fenced JSON
block. The runner validates the block, writes the task's brief and a `packet` task file
(`state: ready`, `lanes` from the plan's declared category via `route`'s defaulting), and
settles the plan task `passed`. Nothing is dispatched to build until the operator (or
`auto_dispatch: true` on the board) flips it; the default is to draft, not to build.
- Category `plan` is declared by lanes the operator marks `tier: plan` (J2); the runner
  refuses to plan on any other tier.
- Tests (`tests/test_plan_task.py`): the scout section reaches the prompt; a fake lane's
  `packet.md` becomes a valid packet task; a malformed block holds the plan task with the
  validation error; nothing builds without the flag.
- Size: medium–large. Why: briefs are the last thing only the coordinator produces.

#### J2. Model tiering, written down
Lane records gain `tier: plan | build | review` (documentation of the intent that already
exists: SOTA for plan and review, workhorses for build). `lanes/config.py` is provider-authored
— read the key in `lanes/route.py`, default `build` when absent. `route` prefers a lane whose
tier matches the task's category (`plan` → plan, `independent_review` → review, else build)
and reports tier mismatches in `dropped` with the reason; the dry run explains it.
`docs/LANES.md` gets a tier column and the one-paragraph policy.
- Tests: tier preference with ties broken by the existing score; mismatch reported; absent
  tier defaults to build.
- Size: small.

#### J3. The tick runtime drains before it exits
`deployments/local/tick_boards.py` handles `SIGTERM`/`SIGINT` by finishing the board tick in
progress (the packet loop's rounds included) and then exiting; a second signal within 30 s
exits at once. While draining it writes `draining` to a state file beside the log so the
operator can see why the restart is slow. `launchctl`'s `ExitTimeOut` for the plist is set
by `install.py` to the longest packet wall clock plus a minute, so launchd does not `SIGKILL`
mid-round.
- Tests: a fake tick that sleeps is allowed to finish after a signal; the second signal
  ends it; the plist carries `ExitTimeOut`.
- Size: small. Why: `packet-i2` was killed mid-round by an operator config change.

#### J4. Failover on failure, not race
When a `packet` task settles `held` or `blocked` with reason `rounds_exhausted`,
`agent_stopped_early` or a transport refusal, and the task carries `"failover": true`
(default true for packet tasks; the operator can turn it off), the runner authors **one**
retry task on a lane of a *different family* that declares the category, with
`failover_from: <task id>` recorded on both, the predecessor marked `superseded:` in the
existing way, and the ledger event naming the family swap. No second attempt is ever
authored on the same family by this path, and a failover task never fails over again.
- Tests: a held packet on `deepseek` re-appears once on `glm`; the reverse; no third; a
  task with `failover: false` stays put; the dry run reports the pending failover.
- Size: small–medium. Why: the hedge race-N buys, at the cost of one extra attempt only
  when something already failed.

#### J5. The boards on the local dashboard
The operator sees planned and active work only by reading board JSON and the ledger by hand;
`inference-grid digest` is markdown with counts (and lists boards twice when a config
directory holds duplicates — fix that while here: one entry per board name). Add a
**Boards** section to the local dashboard (`src/inference_grid/capacity_web`, served by
`deployments/local/capacity_web.py`), fed by a new data node:
- `inference-grid boards --json {boards_dir, database, packets_root}` → JSON: per board
  `{name, planned: [...], active: [...], blocked: [...], landed_today: [...]}` where
  *planned* is the dry-run plan row for each `ready` task (candidate lane, `reason` when it
  cannot dispatch — `account busy`, `quota stale`, `no_lane_for_category` — and age),
  *active* is each `dispatched` task's live attempt (lane, model, round N of max, minutes
  elapsed, the last gate result read from the attempt's newest `gates-*` directory),
  *blocked* carries the reason and whether it is operator-owed (reuse
  `operator_queue.OPERATOR_REASON_WORDS`), *landed_today* the tasks settled `landed` or
  `passed` since midnight local. Reads only; never dispatches; never spends quota.
- `deployments/local/overlay_build.py` calls it and writes the result under `boards` in
  `overlay.json`; `capacity_web` serves it at `/api/boards` and the page renders one card per
  board (counts in the header, the four lists beneath, the active row's round and gate first).
  The theme tokens the page already uses; no new dependency.
- **Local only.** `upload.py` must not send `boards`, and the cloud's `clean_snapshot` must
  reject it if it ever arrives: task ids are the operator's project names.
- Tests: the data node against a temp board with one task in each state and a fake attempt
  directory; the digest's duplicate fix; `upload.py` strips `boards`; `clean_snapshot`
  refuses it; a static check that `app.js` references `/api/boards`.
- Size: medium.

---

## Definition of done, per packet
As brief 15. Report at `docs/reports/brief-17-<packet>.md`.
