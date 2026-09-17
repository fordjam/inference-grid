"""One long-lived scheduler for the capacity collectors and the cloud upload.

launchd holds StartInterval spawns for a GUI-session agent while the display is off
("pended nondemand spawn = interval"), which is exactly when the phone dashboard is the
only view; a process that is already running is not held. So this loop is the job:
KeepAlive, no timers. Same scripts, same cadence: the Go and Claude collectors every
300 s (Claude's own chain runs GOAT, Z.ai, Codex and the overlay build), the
upload every 60 s. Each child is bounded and one job's failure never stops the others.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
POLL = 5


def default_jobs(config):
    here = Path(__file__).resolve().parent
    python = config.get("python", sys.executable)
    out_dir = Path(config.get("output_dir", DEFAULT_DIR))
    return [
        {
            "period": 300,
            "argv": [python, str(here / "refresh_go.py")],
            "log": str(out_dir / "go-collector.log"),
            "error_log": str(out_dir / "go-collector-error.log"),
        },
        {
            "period": 300,
            "argv": [python, str(here / "refresh_claude.py")],
            "log": str(out_dir / "claude-collector.log"),
            "error_log": str(out_dir / "claude-collector-error.log"),
        },
        {
            "period": 60,
            "argv": [
                python,
                str(here / "upload.py"),
                str(config.get("upload_config", out_dir / "client.json")),
            ],
            "log": str(out_dir / "upload.log"),
            "error_log": str(out_dir / "upload-error.log"),
        },
        {
            # watch: stall and staleness alarms, edge-triggered, notifier from the spec
            "period": 60,
            "argv": [
                config.get("inference_grid_bin", "inference-grid"),
                "watch",
                "--json",
                str(config.get("watch_spec", out_dir / "watch-spec.json")),
            ],
            "log": str(out_dir / "watch.log"),
            "error_log": str(out_dir / "watch-error.log"),
        },
    ]


def run(jobs, clock, sleep, spawn, poll=POLL, stop=None):
    """Run each job on its own period. ``clock`` and ``sleep`` are injected so tests drive the
    schedule with a fake clock; ``spawn(job)`` starts one child. One job's exception is
    logged to its error log and never stops the others."""
    due = [0.0] * len(jobs)
    while not (stop and stop()):
        now = clock()
        for i, job in enumerate(jobs):
            if now < due[i]:
                continue
            due[i] = now + job["period"]
            try:
                spawn(job)
            except Exception as exc:
                _log_failure(job, exc)
        sleep(poll)


def _log_failure(job, exc):
    name = Path(job["argv"][1]).name
    stamp = time.strftime("%FT%TZ", time.gmtime())
    try:
        with open(job["error_log"], "a") as err:
            err.write(f"{stamp} capacity-loop: {name} {type(exc).__name__}\n")
    except OSError:
        print(stamp, "capacity-loop:", name, type(exc).__name__)


def default_spawn(job):
    with open(job["log"], "a") as out, open(job["error_log"], "a") as err:
        subprocess.run(job["argv"], stdout=out, stderr=err, timeout=240)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    run(default_jobs(config), time.monotonic, time.sleep, default_spawn)


if __name__ == "__main__":
    main()
