# GLM lane report — brief 14, F1: `doctor` executes every lane binary (2026-09-15)

Lane: DeepSeek-V4.1-Flash via Command Code. Base: `origin/glm/work` at `829e95b`.
Branch: `glm/f1-doctor-executes-every-lane-binary`, one commit, not pushed.

The packet is F1 from `docs/handoff-glm-15.md`; the driver names this row and report
"brief 14", so they follow the driver's naming.

This branch carried no commit ahead of the base when this round began (the previous
session left the working tree clean and `git log origin/glm/work..HEAD` empty, which is
exactly what the commit gate's "found 0" reported). There was nothing to amend or
squash, so the packet is landed here as a single new commit — one commit ahead, the
count the gate wants. No test was weakened, skipped or deleted.

## What landed

**`src/inference_grid/doctor.py`** — the probe, sharing one seam with the launchd check:

- `run_command(argv, timeout)` is the seam: the single place `subprocess.run` is called
  for a local probe. The launchd check (`_launchctl_list`) now goes through it, and so
  does the new lane probe, so a caller can replace the runner once instead of
  monkeypatching `subprocess`.
- `version_probe(executable, run=None, timeout=20)` runs `<executable> --version` and
  classifies the outcome: `{"status": "available", "version": …}` (first non-empty
  stdout line, stderr fallback), or `{"status": "unavailable", "reason": …}` where the
  reason is `missing` (not a file / not executable), `timeout` (the 20 s budget,
  `LANE_VERSION_TIMEOUT`), `exit <n>` (non-zero exit), or a **named signal** —
  `SIGKILL` is what macOS 27 does to a binary whose signature a postinstall rewrote.
- `lane_binaries(lane_specs, run=None, timeout=20)` walks the operator's lane config:
  CLI kinds get a `version_probe`; the HTTP kind (`go_http`) reports `n/a` by name
  rather than being silently absent. Rows are sorted by lane id.
- `diagnose(..., lane_specs=None, run=None)`: when lane specs are supplied, the report
  gains `lane_binaries` and every `unavailable` lane appends
  `lane_binary_unavailable: <lane>` to `findings`, so `status` becomes `attention`.
  With no specs the section is absent and no finding is added — the existing passing
  report is unchanged.

**`src/inference_grid/cli.py`** — `doctor` reads `lanes_path` from its `--json` argument
and validates it with `lanes.runner.load_lanes` (the same loader and the same unguarded
validation `board-tick` already uses), then passes the specs to `diagnose`. Credential
values never enter the report: only the lane id, kind, status and version string do.

**`docs/LANES.md`** — the Cline note: Cline's postinstall rewrites `bin/.cline`, breaks
its signature on macOS 27, and the kernel SIGKILLs every launch before it prints; point
`CLINE_BIN_PATH` at the platform package's real binary (the launcher honours it first),
and `doctor` now names the SIGKILL as `lane_binary_unavailable` instead of leaving it to
be found later.

## Where lane facts come from

`kind` and `executable` are properties of the operator's `lanes.json`
(`lanes/config.py`); the ledger's `lanes` table stores only the readiness record, which
has no kind. So the probe takes the validated lane specs — the doctor CLI loads them
from the `lanes_path` the operator passes in `--json`, exactly as `boards_dir` and
`packets_root` are operator-passed roots. Nothing is guessed from the home directory.

## Tests

`tests/test_doctor.py`, three new tests (11 → 14 in the file):

- `test_lane_version_probe_names_each_outcome` — fake executables for every outcome:
  a script that prints a version (`available`), `exit 3` (`exit 3`), `kill -9 $$`
  (`SIGKILL`), a sleep with a short injected timeout (`timeout`), and an absent path
  (`missing`).
- `test_lane_binaries_probe_cli_kinds_and_skip_http` — a `go_http` lane is `n/a`, a CLI
  lane with a working binary is `available` with its version, a CLI lane with a deleted
  binary is `unavailable` `missing`; rows are sorted.
- `test_doctor_probes_cli_lane_binaries_and_names_unavailable` — end to end through
  `diagnose` with a temp ledger: the healthy set is `checks_passed` with the `n/a` and
  `available` rows; deleting the binary turns the report to `attention` with
  `lane_binary_unavailable: <lane>` named. The probe runs a real local subprocess on the
  fake script — no network, no operator config.

## Defects found in existing code

None introduced. Two observations for the coordinator, not fixed here:

- `lanes/config.py` (provider-authored, integrated unmodified) still refuses the
  `opencode_cli` kind its accepted set has not grown to accept — the same one-line patch
  D2 documented. Unrelated to this packet, but a `lanes_path` naming that lane cannot be
  loaded by `doctor` either until it is applied.
- The doctor's `--json` must name `lanes_path` for the probe to run; there is no default
  (the private config is never read from the home directory). If it is omitted, the
  `lane_binaries` section is simply absent — consistent with `boards_dir`/
  `packets_root`, but worth a line in the operator doc if the check is expected to be
  always-on.

## Final gate

- `ruff format` and `ruff check` on `src/inference_grid/doctor.py`,
  `src/inference_grid/cli.py`, `tests/test_doctor.py`: clean.
- Full suite: before **88 failed, 411 passed, 7 skipped**; after **88 failed, 414
  passed, 7 skipped** (three new tests). The failing node-id set is byte-identical to a
  scratch worktree at `829e95b` run with the same interpreter — all 88 are the
  pre-existing sandbox `killpg`/`sandbox-exec` failures, zero new, zero repaired.
