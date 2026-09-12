"""Bounded native-agent supervision with controller-side workspace snapshots at each iteration_end.
Authored by ZCode (GLM-5.3-Flash) through a Grid attempt; the only change is the import of the process
helpers from this package. See docs/CONTRIBUTIONS.md.

Controller-side supervision with filesystem snapshots taken at iteration ends."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from .process import exited_without_reaping, stop_group_before_reap


def _take_snapshot(cwd, snapshot_dir, log, iteration):
    root = Path(snapshot_dir).resolve() / ("iter-%s" % (iteration,))
    log_path = Path(log).resolve()
    exclude_root = Path(snapshot_dir).resolve()

    def ignore(directory, names):
        skipped = []
        for name in names:
            candidate = (Path(directory) / name).resolve()
            if (
                candidate == log_path
                or candidate == exclude_root
                or exclude_root in candidate.parents
            ):
                skipped.append(name)
        return skipped

    shutil.copytree(Path(cwd).resolve(), root, ignore=ignore, dirs_exist_ok=True)
    files = sum(len(names) for _, _, names in os.walk(root))
    return {"iteration": iteration, "path": str(root), "files": files}


def bounded_run_with_snapshots(
    command, cwd, log, snapshot_dir, seconds=120, char_limit=10000, iteration_limit=4
):
    chars = 0
    iterations = 0
    reason = "unknown"
    partial = b""
    position = 0
    snapshots = []
    deadline = time.monotonic() + seconds
    with Path(log).open("xb") as out, Path(str(log) + ".stderr").open("xb") as err:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            while True:
                exited = exited_without_reaping(proc, 0.1)
                with Path(log).open("rb") as src:
                    src.seek(position)
                    chunk = src.read(2_000_001)
                    position += len(chunk)
                partial += chunk
                lines = partial.split(b"\n")
                partial = lines.pop()
                for line in lines:
                    try:
                        d = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    e = d.get("event", {})
                    if e.get("type") == "iteration_start":
                        iterations += 1
                    if e.get("type") == "content_start":
                        chars += sum(
                            len(e.get(k, ""))
                            for k in ("text", "reasoning")
                            if isinstance(e.get(k, ""), str)
                        )
                    if e.get("type") == "iteration_end":
                        n = e.get("iteration")
                        if n is not None:
                            snapshots.append(_take_snapshot(cwd, snapshot_dir, log, n))
                if exited:
                    reason = "process_exited"
                    break
                if chars > char_limit:
                    reason = "visible_output_budget"
                    break
                if iterations > iteration_limit:
                    reason = "iteration_budget"
                    break
                if position > 2_000_000:
                    reason = "log_byte_budget"
                    break
                if time.monotonic() >= deadline:
                    reason = "wall_deadline"
                    break
        finally:
            try:
                stop_group_before_reap(proc)
            except PermissionError:
                # Sandboxed environments may deny group signalling and /bin/ps;
                # fall back to stopping and reaping the child directly.
                try:
                    proc.kill()
                except (PermissionError, ProcessLookupError):
                    pass
                proc.wait()
    return {
        "reason": reason,
        "returncode": proc.returncode,
        "visible_chars": chars,
        "iterations": iterations,
        "log_bytes": position,
        "snapshots": snapshots,
    }
