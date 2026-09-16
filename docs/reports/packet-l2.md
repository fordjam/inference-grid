# DeepSeek-V4.1-Flash lane report — packet L2: a `dispatched` task whose attempt is dead is requeued (2026-09-16)

Lane: DeepSeek-V4.1-Flash (packet lane). Base: `188bb41` (the tip of `glm/work` at branch
time). Branch: `packet/packet-l2`, one commit, not pushed.

## Why

The brief names the failure it observed: "a board row whose attempt died with the runtime
stays `dispatched` for ever (hand-reset three times)". A pass marks a task `dispatched`
before it admits the attempt (J6) and settles the board from the worker, so a runtime that
dies in between — or an operator `resolve` after a hold the board never sees — leaves the
task in `dispatched` beside a ledger attempt that is already terminal and unsuccessful.
`tick` dispatches only `ready` tasks, so the task never runs again; the operator had been
resetting it by hand.

## What landed

**`src/inference_grid/board/runner.py`** — the requeue, in the existing tick loop; no new
module and no provider-authored file touched.

- **`LEDGER_ATTEMPT`** — the tail the ledger appends to a board task id to name an
  attempt's own task: `<board id>-<UTC stamp>-<random>`, with an optional `-verify` on the
  deadline-hold follow-up. A candidate is kept only when this exact tail matches, so a task
  whose id is merely a prefix of another (`copy` vs `copy-ok`) never claims the other's
  attempts.
- **`ledger_attempts(ledger, task_id)`** — every ledger attempt for one board task, newest
  first (the last transition orders "newest"; the ledger task id breaks a tie the way the
  admissions ran).
- **`requeue_dead(path, task, ledger, dry_run=False)`** — reads the newest attempt. If any
  attempt is still ACTIVE (`queued`/`dispatching`/`held`) the task is left alone. If the
  newest is terminal and unsuccessful (`failed`/`abandoned`) the task is settled `ready`
  and a `requeued` ledger event names the attempt id, its ledger task id and its terminal
  state; the row reports `requeued`. If the newest is terminal *successful*
  (`completed`/`accepted`) the row reports `dispatched: attempt <state>` and nothing is
  written — the board state is the operator's to settle. A dry run returns the same rows
  and writes neither the task file nor the event.
- **The tick wiring** — the handler sits just before the `state != "ready"` gate, so a
  `dispatched` task is (re)settled before anything reads the ready path, and the rest of
  the pass is untouched.

## Boundary worth the operator's eye

A `dispatched` task with **no** ledger attempt at all — a crash between `save_task(...
"dispatched")` and the first admit, or a submit that never reached `claim` — is not
requeued: the brief's condition is a terminal attempt, and "no attempt" is a different
strand. Such a row still needs the operator's hand; the report leaves it named rather than
inventing a requeue the packet did not ask for.

## Tests

`tests/test_board_runner.py`, +5 (offline, the `world` fixture):

- `test_a_dispatched_task_with_a_dead_attempt_is_requeued[failed|abandoned]` — a dead
  attempt (`consumed` → `failed`, `released` → `abandoned`) in the ledger's own id shape
  returns the task to `ready` with one `requeued` event naming the attempt, and the board's
  other `ready` task still dispatches on the same tick.
- `test_a_live_attempt_leaves_the_dispatched_task_alone` — a queued (ACTIVE) attempt gets
  no row and the task stays `dispatched`.
- `test_a_terminal_successful_attempt_is_reported_not_touched` — a `completed` attempt is
  reported and the task is not moved.
- `test_a_dry_run_reports_the_requeue_and_changes_nothing` — the plan carries the
  `requeued` row, the task file is byte-identical and no event or attempt was written.

## Verification

- Full suite at the base: the failing set is byte-identical to `188bb41` — 25 inherited
  (`/bin/ps` sandbox denials in this environment), zero new, zero repaired. The five new
  tests pass.
- `ruff format --check` and `ruff check` clean on the changed files.
- No provider-authored file was touched (`board/task.py` among them). No new credential
  access; no absolute home path or e-mail in the diff.
