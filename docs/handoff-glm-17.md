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
- **Every task row is legible without the id.** Ids like `packet-q5` / `J6` / `M2` say nothing
  about the work. Each row carries `project` (the board name), `id`, `title` — the packet
  heading text from the task's brief (`#### <ID>. <title>`, via `lanes/brief.py`'s heading
  pattern; the task id when the brief has no heading), `focus` — the first sentence of that
  packet section — and `links`: the brief file, the newest `docs/reports/*<id>*.md` when one
  exists, and the attempt directory for active rows. The data node reads only the brief and
  the filesystem for these; nothing is fetched.
- `deployments/local/overlay_build.py` calls it and writes the result under `boards` in
  `overlay.json`; `capacity_web` serves it at `/api/boards` and the page renders one card per
  board (counts in the header, the four lists beneath, the active row's round and gate first).
  Rows show **title first**, the id as a small badge, the project name on the card; clicking a
  row opens a drawer with the focus sentence, the full packet section, lane / model / round,
  the last gate tail, and the links. Above the cards, an **All work** table lists every row
  across boards with columns project · title · state · lane · age, sortable by column and
  filterable by project and state, so one glance answers "what is running, for which project,
  doing what". The theme tokens the page already uses; no new dependency.
- **Local only.** `upload.py` must not send `boards`, and the cloud's `clean_snapshot` must
  reject it if it ever arrives: task ids are the operator's project names.
- Tests: the data node against a temp board with one task in each state and a fake attempt
  directory; a task whose brief carries `#### X1. Some title` reports that title and its
  first sentence as focus, one without a heading reports its id; the digest's duplicate fix;
  `upload.py` strips `boards`; `clean_snapshot` refuses it; a static check that `app.js`
  references `/api/boards`, renders `title`, and contains the All-work table.
- Size: medium–large.

#### J6. Concurrent dispatch within a tick
`board-tick` dispatches the ready tasks of a board one after another and returns when the
last settles, and `tick_boards.py` waits for it before the next board: one 30-minute packet
stops every other board and every idle lane. Accounts now carry a `capacity` (3 on GOAT, 2 on
Cline) that this serialization makes meaningless.
- In `board/runner.py::tick`, run admitted attempts concurrently — a worker per attempt,
  bounded by each account's free capacity as the ledger reports it at admission time — and
  settle each as it finishes; the plan, admission and settlement stay exactly as they are
  (the ledger already serializes reservations). `board-tick` returns when every attempt it
  started has settled; its stdout rows are unchanged.
- `tick_boards.py` moves on to the next board as soon as a board's tick has *dispatched*
  (not settled): run each board's tick in its own thread, joined at the end of the pass, so
  a long packet on one board never delays reviews on another. The ready count is summed
  after the joins.
- Landing (I1) and any base-branch write stay under the existing lock; the packet loop's
  attempt directories are already per attempt, so nothing else is shared.
- Tests: two ready tasks on one board with capacity 2 run overlapped (fake adapters that
  sleep; wall time < the sum); capacity 1 keeps them serial; two boards tick concurrently;
  a failure in one attempt settles it without affecting the other; the stdout rows and
  ledger events are identical to the serial run's.
- Size: medium. Why: seven packets are queued behind one.

#### J7. A brief becomes board work: `board-new --from-brief`
Every repository's backlog lives in `docs/handoff-*.md`; the board holds only what someone
authored by hand, and "is this item done?" is answered differently in each repository
(a CONTRIBUTIONS row, a report file, a commit subject). `board-new --json {from_brief:
<path>, board_dir, project_root, lanes?, gates?, base?}` reads every `#### <id>. <title>`
packet in the brief and authors one `packet` task per item **not already done**, where done
means any of: a `docs/reports/*<id-lower>*.md` exists, a CONTRIBUTIONS row begins with
`| <id> |`, or a commit since the brief's own commit mentions `<id>` in its subject with
the brief's number (`brief 16, I2` / `(brief 14 A1)` / `[handoff-6/B1]` — accept the three
shapes seen in this repository, yt-research-mcp and factory-frontend). Each task's brief file
carries the umbrella brief's section 1 (found by `## 1. Hard rules` prefix, closed by `---`,
as `lanes/brief.py` expects) plus the item; gates default to the repository's declared test
command when the board config names one (`test_argv` in the tick config) and the commit
gate always. Items judged done are listed in the reply with the evidence that decided it;
`dry_run` prints the plan.
- Tests: three "done" signals each recognised; an undone item authored with the right
  lanes and gates; a brief with a rules section that lacks the closing rule is refused
  with the line to add; the CLI round trip.
- Size: medium. Why: the coordinator surveyed six repositories by hand and got two wrong.

---

## Definition of done, per packet
As brief 15. Report at `docs/reports/brief-17-<packet>.md`.
