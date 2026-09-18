#!/usr/bin/env python3
"""02-A9 concurrency gate (the WindowServer watchdog kill, 2026-09-18): a launch check
for the local queue runner, run before every headless start. Stdlib only, so it runs
under either the checkout's venv or the bare system `/usr/bin/python3`.

Reads swap used (`sysctl -n vm.swapusage`), free disk on /System/Volumes/Data
(`df -g`), whether a real pytest process is running, and how many headless runs are
already in flight (`pgrep -f "claude -p"` + `pgrep -f "codex exec"`, unanchored --
queue_run.sh launches via `caffeinate -i claude -p ...`, so an anchored `^claude -p`
pattern never matches it; confirmed against a live process in review).

The pytest check is not a plain `pgrep -f pytest` count: queue_run.sh embeds a run's
whole prompt into its own argv (`claude -p "$(cat prompt)"`), and prompts routinely
mention "pytest", so a bare substring match reports a live headless run as a pytest
process (confirmed live in review: a running session with no pytest anywhere made
this reading true). Every `pgrep -f pytest` match is instead cross-checked against its
own full command line (`ps -o command= -p <pid>`) and ignored if it is actually one of
the headless-run processes counted separately above.

Thresholds come from ~/.local/share/inference-grid-capacity/config.json (the same
file the capacity collectors read), keys `gate_swap_gb` (default 4), `gate_disk_gb`
(default 20), `gate_max_runs` (default 1) -- read only, this script never writes
there.

Exit 0 means launch is allowed. Exit 1 prints one `blocked: ...` line on stdout naming
every failing condition and its reading, e.g.:
    blocked: swap 6.2GB > 4GB; pytest running; runs 1 >= 1
`--json` prints the readings, config and blocked reasons as one JSON line instead.
`--wait SECONDS` polls every 60s until allowed or the deadline, then exits 0/1 as above.

Every reading goes through an injectable `run` callable (`run(argv) -> (returncode,
stdout)`) so tests use fixtures, never the real machine.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".local/share/inference-grid-capacity/config.json"
GATE_DEFAULTS = {"gate_swap_gb": 4.0, "gate_disk_gb": 20.0, "gate_max_runs": 1}
DISK_VOLUME = "/System/Volumes/Data"
POLL_SECONDS = 60


def default_run(argv):
    """(returncode, stdout) for argv -- never raises on a non-zero exit: `pgrep`
    exits 1 for "no matching process", a legitimate empty reading, not a failure. A
    missing binary or a timeout reports as (None, "") -- read_state treats that the
    same as any other failed reading (an unknown, never a guessed value)."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def load_gate_config(config_path=None):
    """`gate_swap_gb`/`gate_disk_gb`/`gate_max_runs`, from the capacity config's own
    keys when present and a positive number; the built-in default otherwise. A missing
    or unreadable config file is not an error here -- the defaults are this script's
    own documented behaviour, not an invented fallback."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    config = dict(GATE_DEFAULTS)
    if isinstance(data, dict):
        for key, default in GATE_DEFAULTS.items():
            value = data.get(key)
            if type(value) in (int, float) and not isinstance(value, bool) and value > 0:
                config[key] = int(value) if key == "gate_max_runs" else float(value)
    return config


_SWAP_USED_RE = re.compile(r"used\s*=\s*([\d.]+)([MGmg])")


def _parse_swap_gb(output):
    """The "used" field of `vm.swapusage`'s output (e.g. "used = 604.25M"), in GB."""
    match = _SWAP_USED_RE.search(output or "")
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2).upper()
    return value / 1024 if unit == "M" else value


def _parse_disk_free_gb(output):
    """`df -g`'s last line, 4th field (Avail, already in GB blocks)."""
    lines = (output or "").strip().splitlines()
    if len(lines) < 2:
        return None
    fields = lines[-1].split()
    if len(fields) < 4:
        return None
    try:
        return float(fields[3])
    except ValueError:
        return None


def _pgrep_pids(run, pattern):
    rc, out = run(["pgrep", "-f", pattern])
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _pgrep_count(run, pattern):
    return len(_pgrep_pids(run, pattern))


# queue_run.sh launches a headless run as `caffeinate -i claude -p ... <prompt text>
# ... --output-format text` (never a bare `claude -p`), so an anchored `^claude -p`/
# `^codex exec` pattern never matches it -- confirmed against a real launched process
# in review. Unanchored substring matches on the full command line instead.
_HEADLESS_RUN_PATTERNS = ("claude -p", "codex exec")


# A pgrep substring match is necessary but not sufficient: any shell whose command
# text merely contains the words (a monitor loop running `pgrep -f "claude -p"`, a
# `bash -c` wrapper, an editor) also matches. Confirmed live on 2026-09-18: with no
# headless run anywhere the count was 1, the match being the operator's own shell.
# So every matched pid is cross-checked against its real command line, and only a
# process whose executable is claude/codex (optionally wrapped by caffeinate) counts.
# caffeinate forks and execs, so a `caffeinate -i claude -p ...` launch shows as
# two processes: the caffeinate parent and the `claude -p ...` child (verified in
# review). Counting both would double every run, so only the child counts: the
# executable itself must be claude or codex, never a wrapper.
_HEADLESS_EXEC_RE = re.compile(r"^(?:\S*/)?(?:claude\s+-p|codex\s+exec)\b")


def _is_headless_run_process(command):
    return bool(_HEADLESS_EXEC_RE.match(command.strip()))


def _headless_run_count(run):
    # One pid can match several patterns (a claude prompt whose text mentions
    # "codex exec" is hit by both pgreps), so pids are collected into a set first.
    pids = set()
    for pattern in _HEADLESS_RUN_PATTERNS:
        pids.update(_pgrep_pids(run, pattern))
    count = 0
    for pid in sorted(pids):
        rc, command = run(["ps", "-o", "command=", "-p", pid])
        if rc == 0 and _is_headless_run_process(command):
            count += 1
    return count


def _is_headless_run_command(command):
    return any(pattern in command for pattern in _HEADLESS_RUN_PATTERNS)


def _pytest_running(run):
    """Whether a real pytest suite is running. `pgrep -f pytest` alone also matches a
    headless run's own inline prompt text mentioning "pytest" -- queue_run.sh embeds
    the whole prompt into the launched process's argv (`claude -p "$(cat
    prompt)"`), and these prompts routinely instruct an agent to run pytest;
    confirmed live in review: a running headless session made this reading `True`
    with no pytest process anywhere. Every `pgrep -f pytest` match is cross-checked
    against its own full command line and dropped if it is actually one of the
    headless-run processes counted separately by `_headless_run_count`.
    """
    for pid in _pgrep_pids(run, "pytest"):
        rc, command = run(["ps", "-o", "command=", "-p", pid])
        if rc != 0:
            continue
        if _is_headless_run_command(command):
            continue
        return True
    return False


def read_state(run=None):
    """Every reading this gate needs, through the injectable `run`. A reading that
    could not be taken (a non-zero/None returncode from `sysctl`/`df`, an unparsable
    line) reports `None`, never a guessed number -- `evaluate` treats an unknown
    reading as passing that one check rather than blocking on a guess, since this
    gate exists to block on evidence, never on the absence of it.
    """
    run = run or default_run
    rc, out = run(["sysctl", "-n", "vm.swapusage"])
    swap_gb = _parse_swap_gb(out) if rc == 0 else None
    rc, out = run(["df", "-g", DISK_VOLUME])
    disk_free_gb = _parse_disk_free_gb(out) if rc == 0 else None
    return {
        "swap_gb": swap_gb,
        "disk_free_gb": disk_free_gb,
        "pytest_running": _pytest_running(run),
        "running_runs": _headless_run_count(run),
    }


def evaluate(readings, config):
    """Every failing condition, as `"name value comparator threshold"` strings, in a
    fixed order (swap, disk, pytest, runs) so `blocked: ...`'s wording never reorders
    between runs. An unknown reading (`None`, sysctl/df failed) never blocks by
    itself -- a broken check must not be indistinguishable from a genuinely full
    machine."""
    reasons = []
    swap = readings.get("swap_gb")
    if swap is not None and swap > config["gate_swap_gb"]:
        reasons.append(f"swap {swap:.1f}GB > {config['gate_swap_gb']:.0f}GB")
    disk = readings.get("disk_free_gb")
    if disk is not None and disk < config["gate_disk_gb"]:
        reasons.append(f"disk {disk:.1f}GB < {config['gate_disk_gb']:.0f}GB")
    if readings.get("pytest_running"):
        reasons.append("pytest running")
    runs = readings.get("running_runs", 0)
    if runs >= config["gate_max_runs"]:
        reasons.append(f"runs {runs} >= {config['gate_max_runs']}")
    return reasons


def check(config, run=None):
    readings = read_state(run)
    return readings, evaluate(readings, config)


def wait_until_allowed(config, wait_seconds, run=None, sleep=None, clock=None):
    """Poll `check` every `POLL_SECONDS` until allowed or `wait_seconds` has elapsed
    from the first reading. `sleep`/`clock` are injectable so tests run this loop
    without a real 60s sleep. Returns the last `(readings, reasons)` pair."""
    sleep = sleep or time.sleep
    clock = clock or time.time
    deadline = clock() + wait_seconds
    while True:
        readings, reasons = check(config, run)
        if not reasons or clock() >= deadline:
            return readings, reasons
        sleep(POLL_SECONDS)


def main(argv=None, run=None, sleep=None, clock=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print readings, config and blocked reasons as JSON")
    parser.add_argument("--wait", type=float, default=None, help="poll every 60s up to SECONDS before giving up")
    parser.add_argument("--config", type=Path, default=None, help="capacity config path (default: %(default)s)")
    args = parser.parse_args(argv)
    config = load_gate_config(args.config)
    if args.wait is not None:
        readings, reasons = wait_until_allowed(config, args.wait, run=run, sleep=sleep, clock=clock)
    else:
        readings, reasons = check(config, run)
    if args.json:
        print(json.dumps({"readings": readings, "config": config, "blocked": reasons}))
    elif reasons:
        print("blocked: " + "; ".join(reasons))
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
