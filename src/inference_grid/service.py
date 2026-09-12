"""Supervise scheduler/publication cycles; provider workers remain separate."""

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .worker import exited_without_reaping, stop_group


def write_status(path, state):
    temp = path.with_suffix(".partial")
    with temp.open("w") as stream:
        os.chmod(temp, 0o600)
        json.dump(state, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def supervise(state_dir, interval=15, cycle_timeout=30, stop=None, max_cycles=None, command=None):
    for value in (interval, cycle_timeout):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
            raise ValueError("positive bounded timing required")
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("positive max_cycles required")
    stop = stop or threading.Event()
    root = Path(state_dir).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = root / "service.json"
    command = command or [sys.executable, "-m", "inference_grid.service", "cycle"]
    with (root / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("supervisor already running for this state directory") from exc
        state = dict(
            pid=os.getpid(),
            phase="starting",
            cycles=0,
            failures=0,
            last_success_at=None,
            last_error=None,
            next_cycle_at=None,
            scope="scheduler_and_publisher_only",
        )

        def save():
            state["heartbeat_at"] = time.time()
            write_status(status_path, state)

        proc = None
        try:
            while not stop.is_set() and (max_cycles is None or state["cycles"] < max_cycles):
                state.update(phase="running_cycle", next_cycle_at=None)
                save()
                failure = None
                try:
                    proc = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    deadline = time.monotonic() + cycle_timeout
                    next_heartbeat = time.monotonic() + 1
                    while not exited_without_reaping(proc):
                        if stop.is_set():
                            failure = "stopped"
                            break
                        if time.monotonic() >= deadline:
                            failure = "cycle_timeout"
                            break
                        if time.monotonic() >= next_heartbeat:
                            save()
                            next_heartbeat = time.monotonic() + 1
                        stop.wait(0.05)
                    stop_group(proc)
                    if failure is None and proc.returncode != 0:
                        failure = "cycle_failed"
                    proc = None
                except Exception:
                    failure = "cycle_failed"
                    if proc is not None:
                        stop_group(proc)
                        proc = None
                state["cycles"] += 1
                state["last_error"] = failure
                if failure is None:
                    state.update(failures=0, last_success_at=time.time())
                elif failure != "stopped":
                    state["failures"] += 1
                delay = min(max(300, interval), interval * 2 ** min(state["failures"], 8))
                state.update(
                    phase="backoff" if failure else "idle", next_cycle_at=time.time() + delay
                )
                save()
                if max_cycles is not None and state["cycles"] >= max_cycles:
                    break
                wake = time.monotonic() + delay
                while not stop.is_set() and time.monotonic() < wake:
                    stop.wait(min(1, max(0, wake - time.monotonic())))
                    save()
        finally:
            if proc is not None:
                stop_group(proc)
            state.update(phase="stopped", next_cycle_at=None)
            save()
        return dict(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "cycle"])
    parser.add_argument("--state-dir")
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--cycle-timeout", type=float, default=30)
    args = parser.parse_args()
    if not os.environ.get("GRID_DATABASE_URL") or not os.environ.get("GRID_BROKER_URL"):
        parser.error("GRID_DATABASE_URL and GRID_BROKER_URL are required")
    if args.command == "cycle":
        from .ledger import Ledger
        from .scheduler import tick
        from .queue import publish

        ledger = Ledger(os.environ["GRID_DATABASE_URL"])
        tick(ledger)
        publish(ledger)
        return
    if not args.state_dir:
        parser.error("--state-dir is required for run")
    stop = threading.Event()

    def shutdown(*_):
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)
    supervise(args.state_dir, args.interval, args.cycle_timeout, stop=stop)


if __name__ == "__main__":
    main()
