# GLM-5.3-Flash lane report — packet L1: a stale observation is re-read at admission, not refused (2026-09-16)

Lane: GLM-5.3-Flash (packet lane). Base: `188bb41` (the tip of `glm/work` at branch
time). Branch: `packet/packet-l1`, one commit, not pushed.

## Why

`board_prepare` refreshes the ledger once per pass and a pass can last an hour, so a lane
record ages past its `quota_freshness_seconds` (900 s) while the collector's observation
file behind it — written minutes ago by `collect_goat`/`collect_cline`/`collect_zai` — is
still fresh. The tick then refuses the lane `quota stale` for the rest of the pass, which
is how J5, J6 and J7 each lost passes on the night of 2026-09-16.

## What landed

**`src/inference_grid/board/runner.py`** — the re-read lives in the tick, before the
refusal can happen:

- `tick` gains three keyword parameters, all fed from the tick config (`board-tick
  --json`, single board or the `boards` list, via `cli.board_tick`):
  - `observations` — the map the brief names: provider (or lane) id → observation file
    path. A lane id is accepted for lanes whose id is not a provider name.
  - `output_dir` — the capacity output directory the map defaults to: a provider the map
    does not name is read from `<output_dir>/<provider>-observation.json`, the collectors'
    own convention (`observation_paths`).
  - `package_src` — the directory the runner imports `board_prepare.py` from
    (`sys.path` insert + `importlib.import_module`, the same mechanism
    `board_prepare.py` itself uses for `inference_grid`). The record builder is imported,
    never copied.
- `refresh_stale_lanes` runs once per pass, right after the first `readiness_view`. For
  each lane classified `stale` whose stored record is genuinely past its own freshness
  window (`quota_observed_at` older than `quota_freshness_seconds`), it reads the
  provider's observation file and rebuilds the lane record through
  `board_prepare.observation_record` (with `window_units=None`: every window carrying a
  numeric `used_percent` counts). A fresh file's record is written back with
  `ledger.record_lane` — preserving the record's `unsupported_until` metadata, so a
  recent 401/403 model exclusion survives the refresh — and the whole readiness view is
  re-classified. A file that is itself older than the freshness window is not evidence:
  the lane stays stale and the file's age becomes the readiness reason, which the tick's
  result row and the dry run's plan row carry instead of the generic `no_ready_lane`
  (`quota stale: observation file <n>s old (freshness window <v>s)`).
- Everything else behaves exactly as before: no `observations`/`output_dir` configured,
  no observation path for the lane, a missing or unreadable file, a non-ok reading, a
  missing `package_src` or an unimportable module — all fall through to today's refusal,
  and a missing lane *record* (never observed at all) is not invented from a file.

**`deployments/local/board_prepare.py`** — `observation_record(config, obs,
window_units, valid)` is the builder extracted from `configure_observation`, which now
calls it; its behaviour (account configuration, stale/non-ok lane records) is unchanged
and pinned by the existing tests. With `window_units=None` the builder counts every
numeric window — the shape the runner needs, since it knows no plan's window caps.

**`src/inference_grid/cli.py`** — `board_tick` threads `observations`, `output_dir` and
`package_src` through, both for a single board and for each entry of a `boards` list.

**`docs/LANES.md`** — the board-prepare section documents the three tick-config keys.

## Tests

`tests/test_board_runner.py`, +4 (all offline; the worker seam is the file's scripted
`execute` stand-in, no subprocess, no network):

- `test_a_stale_lane_record_with_a_fresh_observation_file_admits` — a record 2000 s old
  with a 60 s-old file admits; the ledger record is refreshed (`quota_observed_at` from
  the file, `used_percent_max` 40.0) and the lane re-classifies ready.
- `test_the_observation_paths_default_to_the_capacity_output_dir` — with only
  `output_dir`, the file is found under the provider's own name
  (`opencode-observation.json`) and the tick admits.
- `test_a_stale_observation_file_refuses_naming_the_age` — a 2000 s-old file refuses
  with the age in the row reason, the dry-run plan reason and the readiness view's
  reason; no attempt exists, the task stays ready, the ledger record is untouched.
- `test_a_missing_observation_file_behaves_as_today` — a map entry pointing at an absent
  file refuses `no_ready_lane` exactly as the record alone would; nothing written.

`tests/test_local_board_prepare.py`, +4: the extracted builder with no unit map (every
numeric window counts), a non-ok reading (record unobserved, `observed` still returned),
an observation without windows (nothing counted, `used_percent_max` None), and a missing
observation (`None` in, an unobserved record out).

## Verification

- Full suite (`--continue-on-collection-errors`, since the deny-read policy makes
  `tests/test_calibration.py` fail at collection here): the FAILED/ERROR list is
  byte-identical to the same run at the `188bb41` base — 89 rows, all pre-existing
  sandbox refusals plus the deny-read collection error, zero new, zero repaired. The
  touched files alone: 25 failed / 38 passed, the same 25 as the base plus the 7 new
  passing tests.
- `ruff format` and `ruff check` clean on all changed files.
- No provider-authored file was touched (`observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
  `lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py` all
  unchanged). No new credential access; no absolute home path or personal e-mail in the
  diff.

## Two things worth the operator's eye

- **The account, not just the lane record, can still go stale mid-pass.** The refresh
  records the fresh *lane record* (what readiness and routing read). The ledger account's
  own expiry (`observed + valid`, what `claim` checks with `quota stale or model
  ineligible`) is only refreshed by a real `board_prepare` pass, because rebuilding it
  needs the plan's window caps (`GOAT_WINDOW_UNITS` and friends) that the tick config does
  not carry. On the observed failure mode — lane record stale, account fresh, because
  `configure_account` and `record_lane` are written in the same pass — this packet is the
  fix; an account that expires mid-pass still refuses until the next prepare.
- **`package_src` in the tick config must point at `deployments/local/`** (the directory
  containing `board_prepare.py`), which may differ from the `package_src` other
  deployments scripts use to import `inference_grid`. The import is best-effort: a wrong
  path degrades to today's behaviour, it never fails a tick.
