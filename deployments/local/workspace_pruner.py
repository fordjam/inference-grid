"""02-A5: prune ~/.grid-workspaces attempt clones once their task is resolved.

`~/.grid-workspaces` held 8.8 GB with no pruning and filled the disk on 2026-09-16.
This script deletes attempt workspace directories the ledger already knows are done
with -- landed, superseded, failed or abandoned, anything outside ledger.ACTIVE
("queued", "dispatching", "held") -- oldest first, only once the tree's total size
crosses a hard cap, and writes an alarm file once it crosses a lower watermark first.

--dry-run is the default: nothing is ever deleted unless --execute is passed
explicitly. Reads the ledger's attempts table (workspace, state, updated) --
never writes to it; this script only removes directories on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import create_engine, select

from inference_grid.ledger import ACTIVE, attempts

DEFAULT_ROOT = Path.home() / ".grid-workspaces"
DEFAULT_DATABASE_URL = "sqlite:///" + str(Path.home() / ".local/share/inference-grid/board.sqlite")
DEFAULT_ALARM_PATH = Path.home() / ".local/share/inference-grid/alarms/workspace-usage.json"
# 02-A1: the needs-you page reads this reading every build, never re-walking the tree
# itself (that walk took 30+ seconds against the real ~/.grid-workspaces in review,
# well past overlay_build.py's 20 s subprocess timeout). Written every run regardless
# of dry_run or the alarm threshold -- unlike DEFAULT_ALARM_PATH, which is a distinct
# file with its own, narrower contract ("was over the alarm threshold") this never
# touches.
DEFAULT_READING_PATH = (
    Path.home() / ".local/share/inference-grid/alarms/workspace-usage-reading.json"
)
GIGABYTE = 1024**3
HARD_CAP_BYTES = 4 * GIGABYTE
ALARM_BYTES = 3 * GIGABYTE


def directory_size(path: Path) -> int:
    """Total apparent size of every regular file under path, symlinks not followed."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                continue  # gone between listing and stat: not this pass's problem
    return total


def resolved_workspaces(database_url: str) -> dict[str, float]:
    """{resolved workspace path: attempt's last-updated epoch}, read-only.

    "Resolved" is anything outside ledger.ACTIVE: landed, superseded (a newer
    attempt exists for the same task -- still an ended attempt in its own row),
    failed, abandoned. An attempt still queued/dispatching/held is never a
    candidate, even if its clone looks idle -- and neither is a path an ACTIVE
    row currently holds, even if an older, resolved row also named it: workspace
    paths are reused (ledger.py refuses admission with "workspace busy" for
    exactly this reason, and board/runner.py and board/integrate.py both share
    one ~/.grid-workspaces/inbox/<project> worktree across tasks), so a path
    that shows up as both resolved and active right now is in use.
    """
    engine = create_engine(database_url)
    try:
        with engine.connect() as con:
            rows = con.execute(
                select(attempts.c.workspace, attempts.c.state, attempts.c.updated)
            ).fetchall()
    finally:
        engine.dispose()
    active: set[str] = set()
    resolved_at: dict[str, float] = {}
    for workspace, state, updated in rows:
        if not workspace:
            continue
        resolved = str(Path(workspace).resolve())
        if state in ACTIVE:
            active.add(resolved)
            continue
        # Multiple attempts can share a workspace path over its lifetime (retries);
        # keep the most recent resolution time, since that's when it last ended.
        resolved_at[resolved] = max(resolved_at.get(resolved, 0.0), updated)
    return {path: at for path, at in resolved_at.items() if path not in active}


def candidates(root: Path, resolved: dict[str, float]) -> list[tuple[Path, float]]:
    """(directory, last-resolved-at) for every entry under root the ledger marks resolved.

    Only directories the ledger explicitly resolved are candidates -- an
    unrecognized entry (packets/, inbox/, a manual checkout) is left alone even
    under cap pressure; this prunes attempt clones, not the whole tree.
    """
    if not root.is_dir():
        return []
    found = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        key = str(entry.resolve())
        if key in resolved:
            found.append((entry, resolved[key]))
        else:
            for child in entry.glob("*"):
                if child.is_dir() and not child.is_symlink():
                    child_key = str(child.resolve())
                    if child_key in resolved:
                        found.append((child, resolved[child_key]))
    return found


def write_alarm(path: Path, total_bytes: int, cap_bytes: int, alarm_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "written_at": time.strftime("%FT%TZ", time.gmtime()),
                "total_bytes": total_bytes,
                "alarm_bytes": alarm_bytes,
                "cap_bytes": cap_bytes,
            }
        )
    )
    os.replace(tmp, path)


def write_reading(path: Path, root: Path, total_bytes: int, cap_bytes: int) -> None:
    """The plain reading, every run -- `total_bytes` is already paid for by `prune()`'s
    own `directory_size(root)` call, so this costs nothing extra."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "written_at": time.strftime("%FT%TZ", time.gmtime()),
                "root": str(root),
                "total_bytes": total_bytes,
                "cap_bytes": cap_bytes,
            }
        )
    )
    os.replace(tmp, path)


def prune(
    root: Path = DEFAULT_ROOT,
    database_url: str = DEFAULT_DATABASE_URL,
    cap_bytes: int = HARD_CAP_BYTES,
    alarm_bytes: int = ALARM_BYTES,
    alarm_path: Path = DEFAULT_ALARM_PATH,
    reading_path: Path = DEFAULT_READING_PATH,
    dry_run: bool = True,
) -> dict:
    total = directory_size(root)
    write_reading(reading_path, root, total, cap_bytes)
    report = {
        "root": str(root),
        "total_bytes": total,
        "cap_bytes": cap_bytes,
        "alarm_bytes": alarm_bytes,
        "dry_run": dry_run,
        "alarm_written": False,
        "deleted": [],
        "would_delete": [],
        "freed_bytes": 0,
    }
    if total >= alarm_bytes:
        if not dry_run:
            write_alarm(alarm_path, total, cap_bytes, alarm_bytes)
        report["alarm_written"] = True
    if total < cap_bytes:
        return report
    resolved = resolved_workspaces(database_url)
    ranked = sorted(candidates(root, resolved), key=lambda pair: pair[1])  # oldest resolved first
    remaining = total
    for entry, _resolved_at in ranked:
        if remaining < cap_bytes:
            break
        size = directory_size(entry)
        if dry_run:
            report["would_delete"].append(str(entry))
            remaining -= size
        else:
            shutil.rmtree(entry, ignore_errors=True)
            if entry.exists():
                continue  # permission error or similar: nothing freed, don't claim it was
            report["deleted"].append(str(entry))
            report["freed_bytes"] += size
            remaining -= size
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--alarm-path", default=str(DEFAULT_ALARM_PATH))
    parser.add_argument("--reading-path", default=str(DEFAULT_READING_PATH))
    parser.add_argument("--cap-gb", type=float, default=HARD_CAP_BYTES / GIGABYTE)
    parser.add_argument("--alarm-gb", type=float, default=ALARM_BYTES / GIGABYTE)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually delete and write the alarm file; default is dry-run",
    )
    args = parser.parse_args(argv)
    report = prune(
        root=Path(args.root).expanduser(),
        database_url=args.database_url,
        cap_bytes=int(args.cap_gb * GIGABYTE),
        alarm_bytes=int(args.alarm_gb * GIGABYTE),
        alarm_path=Path(args.alarm_path).expanduser(),
        reading_path=Path(args.reading_path).expanduser(),
        dry_run=not args.execute,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
