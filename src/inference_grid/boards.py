"""`inference-grid boards`: the local dashboard's read-only view of a board's work.

The operator could see planned and active work only by reading board JSON and the ledger
by hand; this node turns both into one JSON document per board:

    {name, planned: [...], active: [...], blocked: [...], landed_today: [...]}

- *planned* is the dry-run plan row for each `ready` task: the candidate lane, a human
  reason when it cannot dispatch (`account busy`, `quota stale`, `no_lane_for_category`)
  and the task file's age.
- *active* is each `dispatched` task's live attempt: the lane, model, the round it is on
  out of the task's bound, minutes elapsed and the last gate result read from the
  attempt's newest `gates-*` directory.
- *blocked* carries the reason and whether it is operator-owed (the words
  `operator_queue.OPERATOR_REASON_WORDS` names, `superseded` excluded).
- *landed_today* is the tasks settled `landed` or `passed` since local midnight.

Every row is legible without the id: `project` (the board's name), `id`, `title` (the
packet heading text from the task's brief, `#### <ID>. <title>`, else the id), `focus`
(the section's first sentence), the full `section` for the dashboard's drawer, and
`links` (the brief file, the newest `docs/reports/*<id>*.md`, and the live attempt's
directory). Only the brief and the filesystem are read for those; nothing is fetched.

Reads only: this node never dispatches, never writes a task file and never spends quota.
The planning call is `board.runner.tick(..., dry_run=True)`, which stops at route.
"""

from __future__ import annotations

import datetime
import re
import time
from pathlib import Path

from sqlalchemy import select

from .board.runner import load_board
from .board.runner import tick as board_run
from .lanes.brief import PACKET_HEADING
from .lanes.runner import load_lanes
from .ledger import ACTIVE, Ledger
from .ledger import tasks as tasks_table
from .operator_queue import EXCLUDED_REASON_WORDS, OPERATOR_REASON_WORDS
from .board_configs import ordered_configs

MAX_BOARDS = 20
MAX_ROWS = 200
MAX_FIELD = 4000
GATE_TAIL_CHARS = 600

# The readiness states that make a plan refusal worth naming as a quota problem.
_STALE_STATES = ("stale",)


def _first_sentence(text):
    """The first sentence of `text`, whitespace-flattened and bounded; None when empty."""
    flat = " ".join((text or "").split())
    if not flat:
        return None
    match = re.search(r"^(.+?[.!?])(?:\s|$)", flat)
    return (match.group(1) if match else flat)[:MAX_FIELD]


def brief_identity(brief_path, packet_id, task_id):
    """(title, focus, section) from the task's brief, never fetched.

    The title is the `#### <ID>. <title>` heading text — the heading `packet_id` names,
    else the first one — and the task id when the brief carries no packet heading. The
    focus is that section's first sentence; the section is the whole section, heading
    included, so the dashboard's drawer can show it. An unreadable brief is the task id
    with no focus and no section.
    """
    if brief_path is None:
        return task_id, None, None
    try:
        text = Path(brief_path).read_text()
    except OSError:
        return task_id, None, None
    matches = list(PACKET_HEADING.finditer(text))
    chosen = None
    for match in matches:
        if packet_id and match.group(1) == packet_id:
            chosen = match
            break
    if chosen is None and matches:
        chosen = matches[0]
    if chosen is None:
        return task_id, _first_sentence(text), None
    end = len(text)
    for later in matches[matches.index(chosen) + 1 :]:
        end = later.start()
        break
    phase = text.find("\n## ", chosen.end())
    if phase != -1 and phase < end:
        end = phase
    section = text[chosen.start() : end].strip()
    body = section.split("\n", 1)[1] if "\n" in section else ""
    return chosen.group(2), _first_sentence(body), section


def _report_link(project_root, task_id):
    """The newest `docs/reports/*<id>*.md`, or None when the project carries none."""
    if not project_root or not task_id:
        return None
    reports = Path(project_root) / "docs" / "reports"
    if not reports.is_dir():
        return None
    # Report files lowercase the packet id (`packet-j5.md`); the id is not.
    names = {task_id}
    names.add(task_id.lower())
    found = {path for name in names for path in reports.glob("*" + name + "*.md")}
    if not found:
        return None
    return str(max(found, key=lambda path: path.stat().st_mtime))


def _links(project_root, task, attempt_dir=None):
    return {
        "brief": str(Path(project_root) / task["brief"])
        if project_root and task.get("brief")
        else None,
        "report": _report_link(project_root, task.get("id")),
        "attempt": str(attempt_dir) if attempt_dir else None,
    }


def _row_base(project, task, project_root, attempt_dir=None):
    brief_path = Path(project_root) / task["brief"] if project_root and task.get("brief") else None
    packet_id = (task.get("spec") or {}).get("packet_id")
    title, focus, section = brief_identity(brief_path, packet_id, task["id"])
    return {
        "project": project,
        "id": task["id"],
        "title": title,
        "focus": focus,
        "section": section,
        "links": _links(project_root, task, attempt_dir),
    }


def _age_seconds(path, now):
    try:
        return round(max(0.0, now - path.stat().st_mtime), 1)
    except OSError:
        return None


def plan_reason(entry, readiness):
    """The human reason a planned row cannot dispatch, or None when a lane is chosen.

    `route` answers in its own vocabulary (`lane_busy`, `budget_unfit`, `no_ready_lane`);
    the operator reads `account busy`, `quota stale` and `no_lane_for_category` for the
    three the dashboard names, and the raw reason otherwise.
    """
    if entry.get("lane"):
        return None
    candidates = entry.get("candidates") or []
    dropped = entry.get("dropped") or []
    states = {readiness.get(row["lane"], {}).get("state") for row in candidates}
    if entry.get("reason") == "lane_busy" or (states and states == {"busy"}):
        return "account busy"
    if any(state in _STALE_STATES for state in states):
        return "quota stale"
    if not candidates and not dropped:
        return "no_lane_for_category"
    return entry.get("reason") or "no_ready_lane"


def _planned_row(project, task, project_root, path, entry, readiness, now):
    row = _row_base(project, task, project_root)
    candidates = entry.get("candidates") or []
    row["lane"] = entry.get("lane") or (candidates[0]["lane"] if candidates else None)
    row["reason"] = plan_reason(entry, readiness)
    row["age"] = _age_seconds(path, now)
    return row


def _attempt_rows(ledger):
    try:
        return ledger.status()
    except Exception:  # noqa: BLE001 — an uninitialized ledger invents no attempts
        return []


def _live_attempt(attempt_rows, packets_root, task_id):
    """The ACTIVE ledger attempt whose workspace is `<packets_root>/<task_id>/…`."""
    if not packets_root:
        return None
    root = Path(packets_root)
    for row in attempt_rows:
        if row.get("state") not in ACTIVE or not row.get("workspace"):
            continue
        try:
            relative = Path(row["workspace"]).relative_to(root)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] == task_id:
            return row
    return None


def _ledger_model(ledger, ledger_task_id):
    try:
        with ledger.engine.connect() as con:
            spec = con.execute(
                select(tasks_table.c.spec).where(tasks_table.c.id == ledger_task_id)
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — a missing task table reads as no model
        return None
    return spec.get("model") if isinstance(spec, dict) else None


def _gate_dirs(attempt_dir):
    if not attempt_dir.is_dir():
        return []
    return sorted((p for p in attempt_dir.glob("gates-*") if p.is_dir()), key=lambda p: p.name)


def _gate_tail(path):
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - GATE_TAIL_CHARS))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _last_gate(gates):
    """The newest `gates-*` directory's last gate log, as `{name, tail}`."""
    if not gates:
        return None
    logs = sorted(gates[-1].glob("gate-*.log"))
    if not logs:
        return None
    name = logs[-1].stem
    return {
        "name": name[len("gate-") :] if name.startswith("gate-") else name,
        "tail": _gate_tail(logs[-1]),
    }


def _last_activity(attempt_dir, fallback):
    """The newest mtime among the attempt's own files and its gate logs (never its clone)."""
    newest = fallback
    if not attempt_dir.is_dir():
        return newest
    files = [p for p in attempt_dir.iterdir() if p.is_file()]
    for gates in attempt_dir.glob("gates-*"):
        if gates.is_dir():
            files.extend(p for p in gates.iterdir() if p.is_file())
    for path in files:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _active_row(project, task, project_root, ledger, attempt_rows, packets_root, now):
    live = _live_attempt(attempt_rows, packets_root, task["id"])
    attempt_dir = Path(live["workspace"]) / live["id"] if live else None
    row = _row_base(project, task, project_root, attempt_dir)
    row["lane"] = row["model"] = row["round"] = row["max_rounds"] = None
    row["minutes"] = row["age"] = None
    row["gate"] = None
    if live is None:
        return row
    row["lane"] = (live.get("account") or "").replace("-account", "") or None
    row["model"] = _ledger_model(ledger, live.get("task"))
    packet = task.get("category") == "packet"
    gates = _gate_dirs(attempt_dir)
    if packet:
        row["round"] = max(1, len(gates))
        row["max_rounds"] = (task.get("spec") or {}).get("max_rounds", 3)
    row["gate"] = _last_gate(gates)
    activity = _last_activity(attempt_dir, live.get("updated") or 0.0)
    elapsed = max(0.0, now - activity)
    row["minutes"] = round(elapsed / 60, 1)
    row["age"] = round(elapsed, 1)
    return row


def _operator_owed(reason):
    lowered = (reason or "").lower()
    if any(word in lowered for word in EXCLUDED_REASON_WORDS):
        return False
    return any(word in lowered for word in OPERATOR_REASON_WORDS)


def _blocked_row(project, task, project_root, path, now):
    row = _row_base(project, task, project_root)
    reason = task.get("blocked_reason") or ""
    row["reason"] = reason
    row["operator_owed"] = _operator_owed(reason)
    row["age"] = _age_seconds(path, now)
    return row


def _landed_row(project, task, project_root, path, now):
    row = _row_base(project, task, project_root)
    row["state"] = task["state"]
    row["age"] = _age_seconds(path, now)
    return row


def _local_midnight(now):
    local = datetime.datetime.fromtimestamp(now)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _layers(config, project_root):
    """The lanes view the dry run routes on, or None when the config names no lanes file."""
    lanes_path = config.get("lanes_path")
    if not lanes_path or not project_root:
        return None, None
    try:
        return load_lanes(lanes_path), lanes_path
    except (OSError, ValueError):
        return None, None


def board_snapshot(ledger, config, packets_root=None, now=None):
    """One board's planned/active/blocked/landed_today lists; read-only.

    `config` is a board-tick config (the same shape `tick-all` reads), plus an optional
    `name`. Missing `project_root` is derived from the `<project>/grid/board` convention;
    a missing lanes file (or a dry run that raises) leaves the plan empty and every ready
    task is reported as `no_lane_for_category` instead — the dashboard still renders.
    """
    now = time.time() if now is None else now
    board_dir = config.get("board_dir")
    project_root = config.get("project_root")
    if not project_root and board_dir:
        project_root = Path(board_dir).parent.parent
    packets = config.get("packets_root") or packets_root
    name = config.get("name") or (Path(project_root).name if project_root else None)
    board = load_board(board_dir) if board_dir else {}
    plan, readiness = {}, {}
    lanes, lanes_path = _layers(config, project_root)
    if lanes is not None:
        try:
            answer = board_run(
                board_dir,
                project_root,
                ledger,
                lanes,
                lanes_path,
                config.get("accounts_by_lane"),
                packets,
                now=now,
                dry_run=True,
                # The board's hosting/retention requirement plans the lane_policy drops
                # the real tick would make.
                require_lane_meta=config.get("require_lane_meta"),
            )
            plan = {row["task"]: row for row in answer.get("plan", [])}
            readiness = answer.get("readiness", {})
        except Exception:  # noqa: BLE001 — a plan that cannot run invents no plan
            plan, readiness = {}, {}
    attempt_rows = _attempt_rows(ledger)
    midnight = _local_midnight(now)
    planned, active, blocked, landed = [], [], [], []
    for task_id, (path, task) in board.items():
        state = task["state"]
        if state == "ready":
            entry = plan.get(task_id) or {
                "task": task_id,
                "lane": None,
                "reason": "no_lane_for_category",
                "candidates": [],
                "dropped": [],
            }
            planned.append(_planned_row(name, task, project_root, path, entry, readiness, now))
        elif state == "dispatched":
            active.append(_active_row(name, task, project_root, ledger, attempt_rows, packets, now))
        elif state == "blocked":
            blocked.append(_blocked_row(name, task, project_root, path, now))
        if state in ("landed", "passed"):
            try:
                settled = path.stat().st_mtime
            except OSError:
                settled = None
            if settled is not None and settled >= midnight:
                landed.append(_landed_row(name, task, project_root, path, now))
    return {
        "name": name,
        "planned": planned[:MAX_ROWS],
        "active": active[:MAX_ROWS],
        "blocked": blocked[:MAX_ROWS],
        "landed_today": landed[:MAX_ROWS],
    }


def boards(ledger=None, boards_dir=None, boards=None, database=None, packets_root=None, now=None):
    """Every board's snapshot, in order. `boards_dir` reads tick-all configs; `boards`
    accepts the operator's own list (the overlay config's `boards`). `database`, when
    given, is the ledger to read — the same URL string `--database` takes.
    """
    if database:
        ledger = Ledger(database)
    if boards is None:
        configs = ordered_configs(boards_dir) if boards_dir else []
    else:
        configs = [config for config in boards if isinstance(config, dict)]
    if ledger is None:
        return []
    return [
        board_snapshot(ledger, config, packets_root=packets_root, now=now)
        for config in configs[:MAX_BOARDS]
    ]
