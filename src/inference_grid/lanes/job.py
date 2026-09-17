"""A job file for the lane driver: one JSON document, one process per lane.

`scripts/run_lane.py` used to take a lane's whole configuration as flags, so a
three-lane round meant three 200-character command lines that zsh word-splitting
could break. A job file replaces that: the top level names the repo, the brief,
the base, the interpreter and where the packets live; `lanes` lists one entry per
lane, each with its own clone, adapter, model and packet ids.

The driver keeps its per-lane flags — one lane still runs from flags alone — and
`--job <file>` is a second entry: it launches every entry as a subprocess, writes
`lane-<name>.log` beside the packets, and prints a table when they are all done.

Relative paths in the file resolve against the file's own directory (so a job
file committed at the repo root can say `"repo": "."` and
`"brief": "docs/handoff-glm-15.md"`); `~` is the operator's home. `run_job` takes
its spawn as a parameter, so the parent's orchestration is tested without running
a lane. The board's packet tasks (`board/packet_task.py`) may read the same
document instead of duplicating the lane list.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

JOB_KEYS = ("repo", "brief", "base", "python", "packets_root", "database", "lanes")
LANE_KEYS = ("name", "clone", "adapter", "model", "packets")
ADAPTERS = ("command_code",)
DEFAULT_ADAPTER = "command_code"
DEFAULT_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_BASE = "glm/work"
DEFAULT_PACKETS_ROOT = "~/.grid-workspaces/packets"
LANE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCRIPT = REPO_ROOT / "scripts" / "run_lane.py"


class JobError(ValueError):
    """A job file that cannot be run as written; the message names the key."""


def _fail(key: str, message: str) -> None:
    raise JobError(f"{key}: {message}")


def _path(value, key: str, base: Path) -> str:
    if not isinstance(value, str) or not value:
        _fail(key, "expected a non-empty string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def _str(value, key: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(key, "expected a non-empty string")
    return value


def _lane(entry, base: Path, names: set[str]) -> dict:
    if not isinstance(entry, dict):
        _fail("lanes", "each lane must be a JSON object")
    unknown = sorted(set(entry) - set(LANE_KEYS))
    if unknown:
        _fail("lanes", "unknown lane keys: " + ", ".join(unknown))
    missing = [k for k in ("name", "clone", "packets") if k not in entry]
    if missing:
        _fail("lanes", "lane missing keys: " + ", ".join(missing))
    name = _str(entry["name"], "lane name")
    if not LANE_NAME.fullmatch(name):
        _fail("lane name", f"must be a simple identifier, got {name!r}")
    if name in names:
        _fail("lane name", f"duplicate lane {name!r}")
    names.add(name)
    adapter = entry.get("adapter", DEFAULT_ADAPTER)
    if adapter not in ADAPTERS:
        _fail(f"lane {name} adapter", "must be one of " + ", ".join(ADAPTERS))
    model = entry.get("model", DEFAULT_MODEL)
    _str(model, f"lane {name} model")
    packets = entry["packets"]
    if (
        not isinstance(packets, list)
        or not packets
        or not all(isinstance(p, str) and p for p in packets)
    ):
        _fail(f"lane {name} packets", "expected a non-empty list of strings")
    return {
        "name": name,
        "clone": _path(entry["clone"], f"lane {name} clone", base),
        "adapter": adapter,
        "model": model,
        "packets": list(packets),
    }


def load_job(path) -> dict:
    """Read, validate and resolve a job file; `JobError` names what is wrong."""
    path = Path(path).expanduser()
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise JobError(f"cannot read job file {path}: {exc}") from None
    except ValueError as exc:
        raise JobError(f"job file is not JSON: {exc}") from None
    if not isinstance(raw, dict):
        _fail("job", "must be a JSON object")
    unknown = sorted(set(raw) - set(JOB_KEYS))
    if unknown:
        _fail("job", "unknown keys: " + ", ".join(unknown))
    missing = [k for k in ("repo", "brief", "python", "lanes") if k not in raw]
    if missing:
        _fail("job", "missing keys: " + ", ".join(missing))
    base = path.resolve().parent
    lanes = raw["lanes"]
    if not isinstance(lanes, list) or not lanes:
        _fail("lanes", "must be a non-empty list")
    names: set[str] = set()
    database = raw.get("database")
    if database is not None:
        _str(database, "database")
    return {
        "repo": _path(raw["repo"], "repo", base),
        "brief": _path(raw["brief"], "brief", base),
        "base": _str(raw.get("base", DEFAULT_BASE), "base"),
        "python": _path(raw["python"], "python", base),
        "packets_root": _path(raw.get("packets_root", DEFAULT_PACKETS_ROOT), "packets_root", base),
        "database": database,
        "lanes": [_lane(entry, base, names) for entry in lanes],
    }


def lane_argv(job: dict, lane: dict, script=DEFAULT_SCRIPT) -> list[str]:
    """The one-lane driver invocation for a job entry: the flags the script still takes."""
    argv = [
        sys.executable,
        str(script),
        "--repo",
        job["repo"],
        "--clone",
        lane["clone"],
        "--brief",
        job["brief"],
        "--packets",
        *lane["packets"],
        "--base",
        job["base"],
        "--lane",
        lane["name"],
        "--model",
        lane["model"],
        "--adapter",
        lane["adapter"],
        "--python",
        job["python"],
        "--packets-root",
        job["packets_root"],
    ]
    if job["database"]:
        argv += ["--database", job["database"]]
    return argv


def lane_log(job: dict, lane: dict) -> Path:
    """`lane-<name>.log`, beside the packet store the entries write under."""
    return Path(job["packets_root"]) / f"lane-{lane['name']}.log"


def format_table(rows: list[dict], headers=("lane", "packets", "result")) -> str:
    """A fixed-width table; the widest cell in each column sets its width."""
    cells = [[str(row.get(h, "")) for h in headers] for row in rows]
    widths = [max([len(h)] + [len(row[i]) for row in cells]) for i, h in enumerate(headers)]
    lines = []
    for i, row in enumerate([list(headers)] + cells):
        lines.append("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)).rstrip())
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _spawn(argv: list[str], log_path: str) -> int:
    with open(log_path, "w") as out:
        return subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT).returncode


def run_job(job_path, spawn=None, script=DEFAULT_SCRIPT) -> int:
    """Launch one subprocess per lane entry, wait for all, print the table.

    `spawn(argv, log_path) -> returncode` is the seam; the default runs the driver
    with the lane's log holding both streams. Returns 0 only when every lane passed.
    """
    job = load_job(job_path)
    script = Path(script)
    packets_root = Path(job["packets_root"])
    packets_root.mkdir(parents=True, exist_ok=True)
    runs = [(lane, lane_argv(job, lane, script), str(lane_log(job, lane))) for lane in job["lanes"]]
    spawn = spawn or _spawn
    with ThreadPoolExecutor(max_workers=len(runs)) as pool:
        futures = [pool.submit(spawn, argv, log) for _, argv, log in runs]
        results = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # one lane's crash never hides the others
                results.append(exc)
    rows = []
    for (lane, _, _), result in zip(runs, results):
        if isinstance(result, int) and not isinstance(result, bool):
            outcome = "passed" if result == 0 else f"failed ({result})"
        else:
            outcome = f"error: {result}"
        rows.append(
            {"lane": lane["name"], "packets": ", ".join(lane["packets"]), "result": outcome}
        )
    print(format_table(rows))
    return (
        0 if all(isinstance(r, int) and not isinstance(r, bool) and r == 0 for r in results) else 1
    )
