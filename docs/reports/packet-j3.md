# GLM lane report — brief 14, J3: the tick runtime drains before it exits (2026-09-15)

Lane: GLM-5.3-Flash (packet lane). Base: `glm/work` at `4cb6a1a`.
Branch: `packet/packet-j3`, one commit, not pushed.

## Why

Packet-i2 was killed mid-round by an operator config change: the first `SIGTERM` to the
`tick-boards` launchd agent killed the loop and the `inference-grid board-tick` subprocess
inside it, mid-packet. A restart during a packet wastes the round's quota and leaves the
attempt to time out. The loop already had a clean shape for this — every interesting boundary
(board start, pass start) is inside `run` — so the fix is a flag checked at those boundaries,
not a rewrite.

## What landed

`deployments/local/tick_boards.py`:

- `Drain` is the state: `on_signal` is the handler for both `SIGTERM` and `SIGINT`. The first
  signal sets `draining`, stamps `since` and writes `draining` to a state file beside the log
  (`tick-boards.draining` next to `tick-boards.log`), so the operator can see why the restart
  is slow. A second signal within `DRAIN_GRACE_SECONDS` (30) exits at once with `128 + signum`
  (`os._exit` — no unwinding through the tick in flight); a signal after the grace keeps
  draining. `clear()` removes the marker and tolerates its absence; `main()` calls it on
  startup (a restart is not a drain — a marker a forced exit left behind must not outlive the
  process that wrote it) and in a `finally` after `run` returns.
- `install_drain(drain)` registers the handler for both signals; it is a separate function so
  the tests can install and restore real handlers without building a config.
- `run(..., drain=None)` checks the drain at the two boundaries: the `while` condition (no new
  pass, no calibration author, no further sleep) and the top of the board loop (no new board).
  A signal arriving during a board tick changes nothing in that tick — `default_tick` blocks in
  `subprocess.run`, and the board-tick subprocess finishes its own packet rounds first — and the
  loop then skips the remaining boards and exits. With `drain=None` the loop is byte-for-byte
  the old one.
- `main()` wires it: the state file is the config's `log_path` with its suffix replaced
  (`.log` → `.draining`), so it is beside the log wherever the config points it.

`deployments/local/install.py`:

- Every rendered plist gains `ExitTimeOut`, set by `exit_timeout(lanes)`: the longest
  `wall_seconds` in the operator's `lanes.json` plus a minute, so launchd does not `SIGKILL`
  the loop while it drains a board tick whose packets each run up to their lane's wall clock.
  `--lanes <path>` names the lanes file; without one (or with no integer `wall_seconds` in it)
  `LANE_WALL_CAP` (3600, the cap `lanes/config.py` enforces) stands in for the longest lane.
  The key goes on all four runtimes — the template is shared by design ("every plist is the
  same shape") and a minute of extra exit time costs nothing on a runtime that exits at once.
- `render(label, python, script, log, timeout)` takes the timeout; the docstring records why
  (`ExitTimeOut` row next to the KeepAlive rationale).

No provider-authored file was touched (`observation.py`, `goat_outcomes.py`,
`remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
`lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py` all unchanged).
No new credential access; no absolute home path, token or e-mail in the diff.

## Tests

`tests/test_local_collectors.py` — all offline (real signals to the test process only, no
network, no spawn outside the fakes):

- `TickBoardsTests::test_a_signal_lets_the_tick_finish_then_ends_the_loop` — the packet's
  first case end to end: real `SIGTERM` handlers installed (and restored in cleanup), a fake
  tick blocked on an event, a sender thread signalling the process mid-tick. The tick is
  allowed to finish, board `b` never ticks, no sleep is taken, and the state file reads
  `draining`.
- `TickBoardsTests::test_a_second_signal_ends_the_tick_at_once` — the second case: two
  `SIGTERM`s 50 ms apart while the tick is still sleeping, with an injected `exit` that raises
  `SystemExit`; `run` ends through it, the tick never completes, and the exit status is
  `128 + SIGTERM`.
- `DrainTests` — four unit cases: the first signal writes the state file and stamps the clock;
  a second within the grace calls `exit(128 + signum)`; a second after the grace does not;
  `clear` removes the marker and tolerates a missing one.
- `InstallTests` — `render` embeds `<key>ExitTimeOut</key><integer>…</integer>`;
  `exit_timeout` is the longest `wall_seconds` plus a minute (900 → 960, 3600 → 3660), falls
  back to the cap with no lanes file or a non-integer value; the per-runtime `plistlib` case
  asserts `ExitTimeOut` on every rendered plist; a `--lanes` case renders 660 from a
  `wall_seconds: 600` fixture.

## Verification

- `tests/test_local_collectors.py`: **52 passed** (42 before, 10 new).
- Full suite with `--continue-on-collection-errors`, failure/`ERROR` set compared line-by-line
  against a scratch worktree at the base `4cb6a1a`: **byte-identical** — 88 failures plus the
  `tests/test_calibration.py` deny-read collection error, all pre-existing (the sandbox
  `killpg`/`/bin/ps` refusals and the private-corpus deny-read), zero new, passes 541.
- `ruff format` and `ruff check` clean on the three changed files. (A repo-wide
  `ruff format --check` names 36 pre-existing unformatted files, none of them touched here;
  the gate's ruff is scoped to the changed-file set.)
- No provider-authored file touched; the one packet file this packet named
  (`deployments/local/tick_boards.py`) is the one it changed, plus the installer the brief
  named.
