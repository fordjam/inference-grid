# GLM lane report — the needs-you overlay (2026-09-14)

Lane: glm (glm-5.3-flash via Command Code). Base: `glm/work` at `829e95b`.
One packet, one commit, fast-forwarded onto `glm/work`:

| Packet | Branch | State |
| --- | --- | --- |
| C2 needs-you overlay | `glm/c2-needs-you-operator-blocked-work-on-the-p` | done |

## What landed

- `src/inference_grid/operator_queue.py` — two lists for the capacity dashboard overlay,
  both built only from things the operator already has: the ledger, the board directory
  and the two state files A1's `watch` CLI and the operator's own notebook write. No
  credential, no `lanes.json`, no home directory: every path is an argument.
  - `operator_rows(ledger, board_dirs, watch_state, owner_decisions)` — `{kind, id,
    reason, since}` rows with four kinds: `blocked_task` (a board task `blocked` whose
    reason names `operator`, `owner` or `resolve with evidence` case-insensitively, and
    whose reason does not say `superseded`; unreadable files are skipped, `since` is the
    file's mtime as an absolute UTC instant), `held_attempt` (a ledger attempt in `held`,
    reason taken from the hold event), `alarm` (every alarm currently raised in the watch
    state file; a missing or unparsable file invents no work) and `decision` (every row
    of the operator's `owner-decisions.json`, sorted after the machine-found rows).
  - `accepted_work(ledger, now)` — the goal's own metric, per subscription per ISO week:
    `{week, account, accepted, attempts}`. An attempt counts as accepted when its
    recorded outcome says so (an attempt accepted by independent review, or
    operator-recorded external work with `accepted: true`), when it reached the
    `accepted` state, or — for a review — when its rejected verdict named a real
    finding: `_real_findings` parses the board runner's rejection note and subtracts the
    findings the note itself marks `advisory-only`, so a rejection resting only on the
    brief's size hint is not counted. Weeks bucket on the attempt's last update; only
    the most recent `WEEKS_KEPT` (8) weeks are emitted, which is what the dashboard's
    this-week-against-last comparison and its 50-row upload bound need.
  - `build_overlay(...)` — both lists in one call, ready for the operator's overlay file.
- `src/inference_grid/capacity.py` and its deployment copy — `clean_operator` (strips
  every unknown key, keeps `kind/id/reason/since`, drops rows without kind and id,
  bounds strings at 200 chars, at most 50 rows) and `clean_accepted_work` (strips to
  `week/account/accepted/attempts`, requires both string keys and both counts in
  0..10^9, bools are not counts). `project` now carries `operator` and `accepted_work`
  from the overlay into the snapshot, defaulting to empty lists when no overlay names
  them, and the overlay's own `attempts` handling is unchanged.
- `deployments/capacity/cloud_server.py` — the cloud boundary rejects rather than
  strips: an `operator` or `accepted_work` upload that is not a list, exceeds 50 rows,
  carries an unknown key, has an empty `kind`/`id`/`week`/`account`, a field over 200
  chars or a non-int (or bool) count raises `ValueError` and stores nothing. Valid rows
  pass through the same cleaners into the stored snapshot.
- Both dashboards (`src/inference_grid/capacity_web/` and
  `deployments/capacity/web/`) — a `NEEDS YOU` summary tile next to the existing
  attention tile, a "Needs you" section listing each row with a human kind label
  (Board task / Held attempt / Watch alarm / Your decision), its reason and a waiting
  time computed from `since`, and an "Accepted work" section showing per subscription
  this ISO week against last ISO week ("n of m attempts accepted"), both bounded at 50
  rows with explicit empty states.

## Tests

- `tests/test_operator_queue.py` (12 cases, offline, temp fixtures only): blocked tasks
  named vs not named (operator / owner / resolve-with-evidence in, superseded and
  non-operator reasons out), held attempts listed with their hold reason, alarms and
  owner decisions listed while missing and unparsable state files invent nothing,
  `build_overlay` combining both lists, per-account-per-week bucketing with a rejected
  review counting exactly when its note names a non-advisory finding, the 8-week kept
  window, `_real_findings` parsing, both cleaners (unknown keys stripped, bad shapes and
  bool counts dropped) and `project` carrying both lists through, plus a static check
  that both dashboards reference `snapshot.operator` / `snapshot.accepted_work` and the
  `needs` / `needsyou` / `acceptedwork` elements.
- `deployments/capacity/test_cloud.py` (3 new cases, compact style): valid operator and
  accepted-work uploads round-trip exactly, while wrong shape, unknown keys, blank
  identity fields, oversized fields and non-int/bool/negative counts are rejected.

## Verification

`ruff format --check` and `ruff check` clean on the changed Python files under `src/`
and `tests/`. Full suite (`tests/` + `deployments/capacity/test_cloud.py`) at the base
commit `829e95b` vs the changed tree: failure set byte-identical (88 pre-existing
sandbox failures — process-group kills refused in this environment), passes 430 → 445,
the +15 being exactly the new tests. `deployments/capacity/` files keep their compact
style; `ruff` is not run on that directory (its one-line imports predate this packet).
