# DeepSeek-V4.1-Flash lane report — brief 20, M1: a held attempt drafts its own fix packet (2026-09-16)

Lane: DeepSeek-V4.1-Flash (packet lane). Base: `a8ce747` (the tip of `glm/work` at
branch time). Branch: `packet/packet-gm1`, one commit, not pushed.

## Why

The board already runs, gates and lands on its own, and J4 spends one more attempt on a
different family when a packet exhausts itself. Neither answers the question a failure
actually raises: *why did this not land?* — a wall deadline, an agent that went idle, a
gate that failed the same way every round, a transport refusal. None of those say the
operator did something wrong; they say something about the packet, its gates or the
harness. Until now the operator read the verdict by hand, worked out the change, and
wrote the next brief. M1 makes the runner draft that brief itself, as a `plan` task (the
J1 plan node), so the grid finds its own work instead of waiting to be handed it.

## What landed

**`src/inference_grid/board/fix_packet.py`** (new) — the decision, the ticket and the
authoring:

- `fix_reason(verdict, *texts)` names the label a fix is owed for, or None when the
  operator owes the work. The verdict's own `reason` is authoritative; a hold with no
  verdict (an adapter that raised before the loop wrote one) falls back to the reason
  named in the ledger and board texts. Eligible: `wall_deadline`,
  `wall_deadline_before_round`, the L3 idle watchdog's `agent_idle`, `rounds_exhausted`,
  `agent_stopped_early`; plus two conditions that can qualify a reason the loop named
  something else — `same_gate_repeated` (every round's gate marks identical, at least one
  failing) and `transport_refused` (the refusal text names a transport error).
- `fix_ticket(task, project_root, verdict, attempt_dir, reason, note)` builds the ticket
  from the verdict: the task's own packet section (`lanes/brief.py::packet_text` over its
  brief), the last round's gate results (name, outcome, reason, exit and tail), the last
  4 KB of the newest `native-<n>.jsonl`, the block and ledger reasons, and the question
  *what change to the packet, the gates or the harness would let this land?* Its
  `paths` are the packet's own citations (`mentioned_paths`), so the scout orients on the
  packet's files rather than guessing them from prose.
- `author_fix_plan(...)` authors one plan task on the first `tier: plan` lane through
  `plan_task.plan_task(..., task_id=)`, and returns why not otherwise (`no lane is marked
  tier plan`, a collision, a validation error).
- **Idempotency is the plan task's own id.** `fix_plan_id(task, reason)` is derived from
  the pair (`plan-fix-<task>-<reason>`, slugged, ≤ 60 chars), so the same task settling
  the same way a second time computes the same id, finds the file already there and
  drafts nothing. No sidecar to keep in step with the task files, and no second file that
  the board-directory readers would have to learn to skip.
- `verdict_for(packets_root, task_id, blocked_reason)` finds the held attempt's verdict
  from the attempt named in the blocked reason, so the pass loop can recover a draft a
  crashed tick never wrote — or one whose plan lane the operator configured after the
  failure. `recover_fix` is that pass-loop half; a dry run names it `fix pending: <plan
  id>`.

**`src/inference_grid/board/runner.py`** — two hooks:

- At the packet settlement, after the task is saved blocked and before J4 runs (so a
  crash between the two still leaves the plan recorded), `fix_reason` reads the verdict
  and `author_fix_plan` authors the plan; the held result row gains `fix: <plan id>`.
- In the pass loop, a blocked packet task that is not superseded asks `recover_fix`; if
  a fix is pending (and not already drafted) the tick authors it and reports
  `fix: <plan id>`, or `fix pending: <plan id>` in a dry run. A task that already has its
  plan falls through to J4's hook below it, so one deferral of a pending failover by a
  tick is the worst interaction between the two.

**`src/inference_grid/board/plan_task.py`** — `plan_task(board_dir, ticket, lane,
task_id=None)`: an explicit id, so the runner's deterministic-id drafting can use the
same authoring path. Purely additive; the CLI's call is unchanged.

**`src/inference_grid/operator_queue.py`** — `_draft_rows`: a packet the plan node
drafted and nobody released is work waiting on a human, so it becomes a
`{kind: "draft", id, reason, since}` needs-you row whose `reason` is the drafted packet's
brief heading (`#### <ID>. <title>`, else the id). `build_overlay`, `capacity.project` /
`clean_operator` carry it unchanged, and both dashboard shells label it
(`draft:'Drafted fix'`).

**Two consequence fixes** — J1's `drafts.json` already sat in the board directory, and
M1 makes drafts routine rather than occasional: `digest` read every `*.json` as a task
and raised `KeyError` on the sidecar (`task["state"]`), and `doctor.board_counts` counted
it as an unreadable task (`invalid`). Both now skip board-owned state; the drafts list is
read for its title, never counted as a task.

## Tests

`tests/test_board_fix_packet.py` (7 cases, all offline). The packet loop runs for real
against a fake agent and an identity sandbox (the seams `test_board_failover.py` uses),
the plan lane's process seam (`runner.execute`) is faked so a plan attempt completes with
a `packet.md` the test controls, and staging, admission, routing, settlement and the board
files run as production runs them.

- The eligibility matrix: every eligible reason; the text fallback for a verdict-less
  hold; `wall_deadline_before_round` winning its own (longer) name; the repeated-gate
  label from a synthetic verdict; the transport fallback; a reason the operator does owe
  for (`gates_passed`, a review rejection) drafting nothing; one round never reading as
  repetition.
- `plan_lanes` and the deterministic id (`plan-fix-…`, ≤ 60 chars, legal board id).
- **A held packet drafts exactly one plan with the verdict in its ticket**: the row
  carries `fix`, the board holds one `plan-fix-*` task on the plan lane with
  `spec {base, paths}`, and its brief carries the reason, the block and ledger reasons,
  the packet's own section and cited path, the last round's gate tail, the transcript
  line and the question — plus the plan node's ask and hard rules, so the packet node
  could run it. A second tick over the same blocked task drafts nothing and the plan
  settles `passed`.
- **A second identical hold drafts nothing**: the packet is reset to `ready`, re-runs,
  holds for the same reason, and the board still holds one plan task.
- **A draft the settlement never wrote is recovered when a plan lane appears**: no plan
  lane at failure time drafts nothing; the dry run names `fix pending: <plan id>` and
  writes nothing; the next real tick authors it.
- **`auto_dispatch` gates the build**: the plan lane's `packet.md` becomes `packet-k1` in
  `drafts.json`; without the flag the tick reports `draft` and the task stays `ready`,
  with it the draft is released and its own gates run (and fail) — the gate opened.
- A superseded predecessor drafts nothing.

Plus 2 cases in `tests/test_operator_queue.py` (the draft row, its title from the brief
heading and the id when the brief is unreadable, its survival through
`build_overlay`/`clean_operator`, and the shell label), 1 in `tests/test_digest.py` (the
digest survives the sidecar and still finds the oldest ready task) and 1 in
`tests/test_doctor.py` (the sidecar is not counted `invalid`; the junk-file case is
unchanged).

## Judgment calls and defects worth recording

- **The reason set is a reading of the brief.** The brief lists `wall_deadline`,
  `agent_idle`, `rounds_exhausted`, "a gate that failed on the same test three rounds
  running" and "a transport refusal that repeated". `rounds_exhausted` already covers the
  third, so the repeated-gate condition is implemented as its own label and its own door
  — it can qualify a verdict the loop named something else (a planned `agent_idle` round
  that also leaves the gates untouched). `agent_idle` does not exist in `build_loop` yet
  (L3 owns it; the watchdog is a separate packet in the same brief), so the label is
  forward-compatible: it starts matching the day L3 lands. "Repeated" for a transport
  refusal is read as "the hold is a transport refusal", not as a count over attempts;
  the repetition across attempts is J4's failover, which already fires on
  `transport_error`.
- **The plan-lane gap is inherited, not created.** `lanes/config.py` is provider-authored
  ("integrated unmodified") and admits exactly its fixed key set, which has no `tier`, so
  a production `lanes.json` cannot declare a plan lane. The runner reads `tier` from the
  lane view with a `build` default, exactly as J1 and J2 shipped it; M1 selects
  `tier: plan` lanes the same way and is therefore inert in production until that key set
  grows (J2's report carries the three-line patch). I did not add a second signal
  (`categories: ["plan"]`) because the runner's own plan-tier refusal reads the raw config
  dict, so a second signal would have to be threaded through two places and would lay a
  trap for J2's owner. `tests/test_board_fix_packet.py` exercises the mechanism through
  the runner's `lanes` argument, as `test_plan_task.py` does.
- **Ordering against J4.** The fix plan is authored before J4's failover at settlement,
  so a crash between them leaves the plan recorded; in the pass loop the fix hook runs
  first and `continue`s, so a task that is both eligible for a fix and pending a failover
  gets its failover one tick later, once the plan file exists. Nothing is lost, and a
  dry run still names both.
- **A pre-existing hazard is fixed, not merely bypassed.** `digest` raised `KeyError` on
  any board holding a `drafts.json` — reachable the day J1 landed, not only under M1.
  `doctor.board_counts` counted the same file `invalid`. Both are one guard; the
  `junk.json` behaviour those tests pin (an unreadable *task file* is `invalid`) is
  unchanged.

## Final gate

- Full suite with `--continue-on-collection-errors`: **88 failed, 577 passed, 7 skipped,
  1 error**. The 89 `FAILED`/`ERROR` node ids are **byte-identical** to a stash-run of the
  same checkout at the base `a8ce747` (`diff` empty in both directions): the pre-existing
  sandbox refusals (`killpg`/`/bin/ps`/`sandbox-exec` denials) and the calibration
  deny-read collection error, zero new, zero repaired. Passes 567 → 577 (the ten new
  tests).
- `ruff format --check` and `ruff check` are clean on every changed file. Repo-wide,
  34 files would be reformatted and `ruff check` reports 41 findings — all pre-existing
  (fixtures under `grid/tests/`, provider-authored files) and untouched here.
- No provider-authored file was touched (`observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
  `lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py` all
  unchanged). No new credential access; no absolute home path or e-mail in the diff.
