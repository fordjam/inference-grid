"""02-A1 (open half): the needs-you page's read-only data, beyond `operator_queue`'s list.

`operator_queue.build_overlay` (brief 14 C2) already covers blocked board tasks, held
attempts, watch alarms, drafted fix plans and the operator's own decisions. This module
adds the rest of 02-A1's open half, all of it read-only:

- `land_request_rows` -- board packet tasks sitting `passed`/`accepted`: reviewed, and
  waiting only on the operator's own land/inbox-integrate step.
- `heartbeat_rows` -- the tick loop's heartbeat file and every scheduled collector's
  newest observation, aged against now.
- `data_status_rows` -- a lenient, "if present" reader for other projects' own
  `*_status.json` files (`forward_status.json` and its siblings); a project names its
  paths here, this only reads them and only reports one that is alarmed.
- `disk_usage_row` -- `~/.grid-workspaces` against 02-A5's own 4 GB cap.
- `failure_rows` -- 02-C3: every ledger attempt `failed` or `held`, with a log tail and
  a plain-text suggested next packet. The suggestion is text in the row only -- never an
  authored board task, unlike brief 20 M1's `board/fix_packet.py` (a narrower, automatic
  mechanism that drafts a `plan` task, but only for a bounded set of reasons on blocked
  *packet* tasks; this covers every failed or held ledger attempt).

`needs_you()` combines all of the above with `operator_queue.build_overlay` into one
dict for both the `inference-grid needs-you` command and the capacity dashboard.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .board.fix_packet import (
    REPEATED_GATES,
    TRANSPORT_REFUSED,
    fix_reason,
    transcript_tail,
)
from .operator_queue import build_overlay

GIGABYTE = 1024**3
DEFAULT_WORKSPACE_ROOT = Path.home() / ".grid-workspaces"
DEFAULT_WORKSPACE_CAP_BYTES = 4 * GIGABYTE
DEFAULT_LOG_BYTES = 4096

# 02-A1: a packet reaches passed/accepted only after review (B7 removed every waiver
# path); a canary is the one exception -- lane evidence, not deliverable work, it never
# gets a review task -- so canaries are excluded, and only the "packet" category (never
# the review- meta-task itself, category independent_review) counts as a land request.
LAND_REQUEST_STATES = ("passed", "accepted")


def _iso(ts):
    """An absolute UTC instant from an epoch second; empty string when unknown."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def land_request_rows(board_dirs):
    """Board packet tasks sitting `passed` or `accepted`: reviewed, not yet landed."""
    rows = []
    for board_dir in board_dirs or ():
        board = Path(board_dir)
        if not board.is_dir():
            continue
        for path in sorted(board.glob("*.json")):
            try:
                task = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(task, dict) or task.get("category") != "packet":
                continue
            state = task.get("state")
            if state not in LAND_REQUEST_STATES:
                continue
            try:
                since = path.stat().st_mtime
            except OSError:
                since = None
            rows.append(
                {
                    "kind": "land_request",
                    "id": str(task.get("id") or path.stem),
                    "reason": state,
                    "since": _iso(since),
                }
            )
    return rows


def _extract_instant(data):
    """A best-effort epoch-seconds instant out of a heartbeat/observation JSON blob."""
    if not isinstance(data, dict):
        return None
    for key in ("last_pass_at", "written_at", "observed_at"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def heartbeat_rows(heartbeat_paths, now=None):
    """`{name, age_seconds, since}` for every named heartbeat/observation file.

    `heartbeat_paths` is `{name: path}`, operator-supplied (the tick loop's own
    heartbeat file, plus each scheduled collector's newest observation file) -- no path
    is invented here. A missing or unreadable file reports `age_seconds: None`; the
    file's own mtime is the fallback instant when its JSON carries none of the known
    timestamp fields.
    """
    now = time.time() if now is None else now
    rows = []
    if not isinstance(heartbeat_paths, dict):
        return []
    for name, path in heartbeat_paths.items():
        if not path:
            continue
        p = Path(path)
        instant = None
        try:
            instant = _extract_instant(json.loads(p.read_text()))
        except (OSError, ValueError):
            pass
        if instant is None:
            try:
                instant = p.stat().st_mtime
            except OSError:
                instant = None
        rows.append(
            {
                "name": name,
                "age_seconds": None if instant is None else max(0.0, now - instant),
                "since": _iso(instant),
            }
        )
    return sorted(rows, key=lambda r: r["name"])


def _status_alarm(data):
    """Whether a `*_status.json` blob is alarmed, and why -- lenient, field-name driven.

    These files belong to other projects (forward_status.json and siblings); this repo
    does not own their schema, so the check only trusts a handful of common field names
    and never invents an alarm a file did not itself signal.
    """
    if data.get("engine_recorded") is False:
        return True, "engine_recorded is false"
    if data.get("ok") is False:
        return True, "ok is false"
    status = data.get("status")
    if isinstance(status, str) and status.lower() not in (
        "ok",
        "complete",
        "fresh_console_observation",
    ):
        return True, f"status is {status!r}"
    unavailable = data.get("unavailable_count")
    if (
        isinstance(unavailable, (int, float))
        and not isinstance(unavailable, bool)
        and unavailable > 0
    ):
        return True, f"{unavailable} unavailable"
    return False, ""


def data_status_rows(status_paths):
    """Alarms out of other projects' own `*_status.json` files, if present.

    `status_paths` is `{name: path}`, operator-supplied. A missing or unreadable file,
    or one that is not alarmed, contributes no row -- "if present" never invents work.
    """
    rows = []
    if not isinstance(status_paths, dict):
        return []
    for name, path in status_paths.items():
        if not path:
            continue
        p = Path(path)
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        alarmed, detail = _status_alarm(data)
        if not alarmed:
            continue
        try:
            since = p.stat().st_mtime
        except OSError:
            since = None
        rows.append({"kind": "data_status", "id": name, "reason": detail, "since": _iso(since)})
    return rows


def _directory_size(path):
    """Total apparent size of every regular file under path, symlinks not followed.

    Mirrors `deployments/local/workspace_pruner.py`'s own measure (02-A5) so this page's
    number and the pruner's cap decision never disagree; read-only, nothing is deleted.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                continue
    return total


def disk_usage_row(root=None, cap_bytes=None):
    """Current size of `root` (default `~/.grid-workspaces`) against the 4 GB cap, by
    walking the tree live.

    This is the expensive path -- a real `~/.grid-workspaces` measured 30+ seconds in
    review -- so it is safe only where a caller can afford that: the `inference-grid
    needs-you` command, run on demand. A scheduled hot path (the capacity dashboard's
    overlay build, on a subprocess timeout) must use `cached_disk_usage_row` instead,
    reading 02-A5's pruner's own already-paid-for reading rather than re-walking here.
    """
    cap_bytes = cap_bytes if cap_bytes is not None else DEFAULT_WORKSPACE_CAP_BYTES
    root = Path(root) if root else DEFAULT_WORKSPACE_ROOT
    if not root.is_dir():
        return {"root": str(root), "exists": False, "total_bytes": 0, "cap_bytes": cap_bytes}
    total = _directory_size(root)
    return {
        "root": str(root),
        "exists": True,
        "total_bytes": total,
        "cap_bytes": cap_bytes,
        "over_cap": total >= cap_bytes,
    }


def cached_disk_usage_row(status_path, cap_bytes=None):
    """The same shape as `disk_usage_row`, read from 02-A5's pruner's own reading file
    (`deployments/local/workspace_pruner.py`'s `reading_path`, written on every prune
    pass) instead of walking the tree -- safe for a subprocess with a short timeout.
    A missing or unreadable file reports `exists: False`, same as an absent root.
    """
    cap_bytes = cap_bytes if cap_bytes is not None else DEFAULT_WORKSPACE_CAP_BYTES
    if not status_path:
        return {"root": None, "exists": False, "total_bytes": 0, "cap_bytes": cap_bytes}
    try:
        data = json.loads(Path(status_path).read_text())
    except (OSError, ValueError):
        return {"root": None, "exists": False, "total_bytes": 0, "cap_bytes": cap_bytes}
    if not isinstance(data, dict):
        return {"root": None, "exists": False, "total_bytes": 0, "cap_bytes": cap_bytes}
    total = data.get("total_bytes")
    if type(total) not in (int, float) or isinstance(total, bool) or total < 0:
        return {"root": data.get("root"), "exists": False, "total_bytes": 0, "cap_bytes": cap_bytes}
    reading_cap = data.get("cap_bytes")
    if type(reading_cap) in (int, float) and not isinstance(reading_cap, bool) and reading_cap > 0:
        cap_bytes = reading_cap
    return {
        "root": data.get("root"),
        "exists": True,
        "total_bytes": total,
        "cap_bytes": cap_bytes,
        "over_cap": total >= cap_bytes,
        "since": data.get("written_at") if isinstance(data.get("written_at"), str) else "",
    }


# 02-C3: a plain-text pointer at the next packet, keyed by fix_packet's own reason
# vocabulary (brief 20 M1) so the wording stays consistent with the one place that
# already reasons about *why* a packet stalled -- never authored as a board task here.
_SUGGESTION_BY_REASON = {
    "wall_deadline_before_round": "raise the packet's wall budget, or split it into a smaller packet",
    "wall_deadline": "raise the packet's wall budget, or split it into a smaller packet",
    "agent_idle": "the agent went idle; check for a gate or harness stall before retrying",
    "rounds_exhausted": "raise max_rounds, or narrow the packet's scope",
    "agent_stopped_early": "the session answered without working; tighten the packet's ask",
    REPEATED_GATES: "the same gate failed every round: fix the gate or the harness, not the packet",
    TRANSPORT_REFUSED: "a transport/provider error, not the packet: retry once the lane is healthy",
}
_DEFAULT_SUGGESTION = "review the log tail and file a smaller follow-up packet"


def _suggestion(reason_text):
    matched = fix_reason({}, reason_text)
    return _SUGGESTION_BY_REASON.get(matched, _DEFAULT_SUGGESTION)


def _attempt_dir(packets_root, task_id, attempt_id):
    """The attempt's own transcript directory under packets_root, or None.

    Same shape `board/fix_packet.verdict_for` locates verdicts in
    (`<packets_root>/<task_id>/*/attempts/<attempt_id>/`); here the attempt id is
    already known from the ledger row, so no blocked-reason text needs parsing.
    """
    if not packets_root or not task_id or not attempt_id:
        return None
    try:
        matches = sorted(Path(packets_root).glob(f"{task_id}/*/attempts/{attempt_id}"))
    except (ValueError, NotImplementedError):
        # A task or attempt id starting with "/" makes an absolute, unsearchable
        # pattern (pathlib raises NotImplementedError, not the file-not-found this
        # amounts to); no worse than "not found" here.
        return None
    return matches[0] if matches else None


def failure_rows(ledger, packets_root=None, log_bytes=DEFAULT_LOG_BYTES):
    """Every ledger attempt `failed` or `held`: `{kind, id, task, reason, since,
    log_tail, suggestion}`. `log_tail` is empty when `packets_root` is not given or no
    transcript exists; `suggestion` is always plain text, never an authored packet.
    """
    rows = []
    for row in ledger.status():
        state = row.get("state")
        if state not in ("failed", "held"):
            continue
        task_id = str(row.get("task") or "")
        attempt_id = str(row.get("id") or "")
        reason_text = str(row.get("reason") or "")
        attempt_dir = _attempt_dir(packets_root, task_id, attempt_id)
        tail = transcript_tail(attempt_dir, log_bytes) if attempt_dir else ""
        rows.append(
            {
                "kind": state + "_attempt",
                "id": attempt_id,
                "task": task_id,
                "reason": reason_text,
                "since": _iso(row.get("updated")),
                "log_tail": tail[-log_bytes:],
                "suggestion": _suggestion(reason_text),
            }
        )
    return sorted(rows, key=lambda r: (r["kind"], r["id"]))


def needs_you(
    ledger,
    board_dirs=(),
    watch_state=None,
    owner_decisions=None,
    heartbeat_paths=None,
    data_status_paths=None,
    workspace_root=None,
    workspace_status_path=None,
    workspace_cap_bytes=None,
    packets_root=None,
    now=None,
):
    """Everything the operator needs to see, read-only.

    Brief 14's overlay (`operator_queue.build_overlay`: blocked tasks, held attempts,
    drafted fix plans, watch alarms, the operator's own decisions, accepted work,
    reviewer recall) plus 02-A1's open half -- land requests folded into the same
    `operator` list, heartbeat ages, other projects' data-status alarms, disk use --
    and 02-C3's failed/held rows with a log tail and a suggested next packet.

    Disk use: `workspace_status_path`, when given, reads 02-A5's pruner's own cached
    reading -- the safe choice for a scheduled hot path. `workspace_root` walks the
    tree live instead (only `workspace_status_path` is honored when both are given);
    it costs tens of seconds against a real multi-GB `~/.grid-workspaces` (measured in
    review), so it is only for an on-demand caller (the CLI) that can afford to wait,
    never a build on a subprocess timeout. Neither given reports no reading, never a
    guessed one.
    """
    overlay = build_overlay(
        ledger,
        board_dirs=board_dirs,
        watch_state=watch_state,
        owner_decisions=owner_decisions,
        now=now,
    )
    # land requests and data-status alarms are both "kind/id/reason/since" rows, the
    # same shape every other operator row already has, so they fold into the one list
    # the dashboard's Needs You section already renders rather than adding new ones.
    overlay["operator"] = sorted(
        overlay["operator"] + land_request_rows(board_dirs) + data_status_rows(data_status_paths),
        key=lambda r: (r["kind"], r["id"]),
    )
    overlay["heartbeats"] = heartbeat_rows(heartbeat_paths, now=now)
    if workspace_status_path:
        overlay["disk_usage"] = cached_disk_usage_row(workspace_status_path, workspace_cap_bytes)
    elif workspace_root:
        overlay["disk_usage"] = disk_usage_row(workspace_root, workspace_cap_bytes)
    else:
        cap_bytes = (
            workspace_cap_bytes if workspace_cap_bytes is not None else DEFAULT_WORKSPACE_CAP_BYTES
        )
        overlay["disk_usage"] = {
            "root": None,
            "exists": False,
            "total_bytes": 0,
            "cap_bytes": cap_bytes,
        }
    overlay["failures"] = failure_rows(ledger, packets_root=packets_root)
    return overlay
