# GLM lane report — brief 14, J4: failover on failure, not race (2026-09-15)

Lane: GLM-5.3-Flash (packet lane). Base: `3955647` (the tip of `packet/packet-j4` at
branch time; the coordinator's own commits only wrote the brief and the re-queue note).
Branch: `packet/packet-j4`, one commit, not pushed.

## Why

A packet that exhausts its rounds on one family has told us the most expensive way
possible that this family, this model, this day — something is not working. The board's
only answer was an operator-authored `board-new --retry` on the *same* family, hours
later. The hedge a race-N dispatch buys is available cheaper: one extra attempt, spent
only after something already failed, on a family that fails differently. J4 makes the
runner author it.

## What landed

**`src/inference_grid/board/failover.py`** — the decision and the authoring:

- `failover_candidate(task, board)`: a `blocked` packet task is due its one
  different-family retry when it does not set `"failover": false` (packet tasks default
  on), carries no `failover_from` (a failover task never fails over again), is not
  itself superseded, and its reason names `rounds_exhausted`, `agent_stopped_early` or
  `transport_error` — the loop's two exhaustion names plus the transport-refusal marker
  the runner writes. The scan of the loaded board for a successor (`failover_from ==
  task id`) is the crash guard: a tick that died between writing the successor and
  superseding the predecessor never authors a second one.
- `author_failover(...)`: picks the id with `new.next_retry_id` (the existing chain
  convention, `x` → `x-2`), copies the brief verbatim under the new id, and writes the
  successor with `lanes` set to `failover_candidates` — every configured lane whose
  `family` differs from the failed one, declares `packet`, and is a kind that can run a
  packet (`PACKET_KINDS`). Route picks among them at dispatch, so the failed family is
  excluded by construction and no second attempt is ever authored on it. The successor
  is written first, then the predecessor is rewritten to
  `superseded: <new id> — failover <from> -> <families>` (the existing recorded-change
  way), then the `failover` ledger event names the swap (`from_task`, `to_task`,
  `from_family`, `to_families`). With no different-family lane configured, nothing is
  written and the task stays blocked with its reason — authoring retries when a lane
  appears.
- `pending_failover_note(ledger, task, lanes)` — the dry run's one-line report,
  `failover pending: <from family> -> <families>`. The from-family is resolved through
  the ledger (`failed_family`: the attempt named in the blocked reason → its task spec),
  since the failing tick may be long gone.

**`board/packet_task.py`** — the two packet-only keys validate here, the same way
`landed` already does (provider-authored `board/task.py` untouched): `failover` must be
a bool (default true, and the validated task always carries it explicitly) and
`failover_from` a non-empty str of at most 60 chars; both are stripped before the shared
schema check and restored after.

**`board/runner.py`** — two hooks. At the packet settlement (`state != "completed"`),
after the task is saved blocked, `author_failover` runs with the failed family known
directly from `lanes[lane_id]["family"]` and the verdict/ledger reasons passed as extra
eligibility text (a transport refusal lives in the ledger hold reason, not the loop
verdict); the held result row gains `failover: <new id>` when authored. In the pass
loop, a blocked task that is already a failover candidate is acted on: the dry run
appends the pending-failover plan row, a real tick authors it (a blocked task never
reaches the dispatch path, so settlement-time authoring alone would leave it pending
forever — this is also what recovers a crash-gap and what answers a candidate family
that was configured after the failure).

**`board/new.py`, `doctor.py`** — three-line consequence of the explicit key: packet
task files now carry `failover`, so `retry_task` and `doctor.board_counts` validate
through the category-aware `validate_board_task` instead of the fixed-schema
`validate_task` (which would have refused every saved packet file, landed ones
included). Non-packet behaviour is identical — the wrapper delegates to `validate_task`.

## Tests

`tests/test_board_failover.py` (6 tests, offline — the packet task tests' fake agent,
patched adapter and sandbox seams, two fake lanes on two ledger accounts):

- `test_a_held_glm_packet_reappears_once_on_deepseek` — the held packet is superseded
  with `failover glm -> deepseek`, the successor is `ready` on `["ds-lane"]` with
  `failover_from` set and a verbatim brief copy, one `failover` event carries the exact
  swap; with the retry's gates fixed, the second tick passes it on the deepseek account.
- `test_the_reverse_held_deepseek_packet_reappears_on_glm` — the same in the other
  direction: `failover deepseek -> glm`, successor lanes `["glm-lane"]`.
- `test_a_failover_task_never_fails_over_again` — the failover task fails too: no
  `j4-packet-3`, no second event, its lanes never named the failed family.
- `test_failover_false_stays_put` — `failover: false` blocks with its reason, no
  successor, no event.
- `test_the_dry_run_reports_the_pending_failover` — with no different-family lane at
  failure time the task stays blocked and eligible; the dry run reports
  `failover pending: glm -> deepseek` (the family resolved from the ledger attempt),
  writes nothing, and the next real tick authors `j4-packet-2`.
- `test_packet_failover_keys_validate` — the default filled in, `false` and
  `failover_from` preserved, non-bool `failover` and empty/non-str `failover_from`
  refused.

## Verification

- Full suite with `--continue-on-collection-errors` (the deny-read collection error on
  the private calibration corpus is pre-existing): **88 failed, 560 passed, 7 skipped,
  1 error**. The 89 `FAILED`/`ERROR` node ids are **byte-identical** to a scratch
  worktree at the base `3955647` (`diff` empty): the pre-existing sandbox refusals,
  zero new, zero repaired. Passes 554 → 560 (the six new tests).
- `ruff format` and `ruff check` clean on every changed file (repo-wide format naming
  pre-existing unformatted files, several of them provider-authored integrated
  unmodified, was left alone).
- No provider-authored file was touched (`observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
  `lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py` all
  unchanged). No new credential access; no absolute home path or e-mail in the diff.
