"""Stall and staleness alarms as a code node (handoff brief 14 A1).

One pass reads only operator-supplied paths named in the spec — the overlay, the
upload status, the ledger database, per-board tick logs — and produces the alarms
that fired, re-fired or cleared since the previous pass, edge-triggered against a
state file. Every alarm carries ``kind``, ``key``, ``since`` and ``detail``:

- ``reading_stale``: an overlay account whose ``observed_at`` is older than the
  threshold, or whose ``status`` is not ``ok`` (the non-ok status is named).
- ``attempt_held``: a ledger attempt in state ``held`` longer than the threshold.
- ``board_stalled``: a board with at least one ``ready`` task whose tick log shows
  ``no_dispatch_passes`` consecutive passes without a ``passed``, ``held``,
  ``refused`` or ``rejected`` result for that board.
- ``upload_stale``: ``upload-status.json`` older than the threshold or with a
  ``status`` other than ``ok`` (a missing file is the stalest case).
- ``cloud_behind``: the cloud health endpoint reports a commit that differs from
  the newest commit touching the deploy bundle.
- ``cloud_health_unversioned``: the cloud's build could not be confirmed from the
  health endpoint — a plain-text body, a JSON body without a usable commit, or an
  endpoint that cannot be reached at all.

The module never reads a credential, never assumes a home directory and never
imports a chat-bot client: notification is whatever the operator points
``spec["notify"]`` at (point it at an email-sending script; there is no vendor
chat-bot integration here). Every alarm also lands on the needs-you page
regardless of ``notify`` — operator_queue.py's ``_alarm_rows`` reads this
module's state file independently of whether a notify script ran or exists.
"""

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .ledger import Ledger

DEFAULT_THRESHOLDS = {
    "reading_stale_s": 1200,
    "held_s": 900,
    "upload_stale_s": 300,
    "no_dispatch_passes": 2,
}
DEFAULT_RENOTIFY_S = 4 * 3600
PRODUCTIVE_RESULTS = ("passed", "held", "refused", "rejected")
NOTIFY_TIMEOUT_S = 30


def _parse_ts(value):
    """An aware ISO timestamp as epoch seconds, else None (absent is not stale)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.timestamp()


def _alarm(kind, key, since, detail):
    return {"kind": kind, "key": key, "since": since, "detail": detail}


def reading_alarms(spec, thresholds, now, errors):
    """One alarm per overlay account that is stale or not ok; an unreadable overlay
    is an error entry, not a per-account guess."""
    path = spec.get("overlay")
    if not path:
        return []
    try:
        overlay = json.loads(Path(path).read_text())
        accounts = overlay.get("accounts", [])
    except (OSError, ValueError) as exc:
        errors.append({"input": "overlay", "error": type(exc).__name__})
        return []
    if not isinstance(accounts, list):
        errors.append({"input": "overlay", "error": "accounts_not_a_list"})
        return []
    alarms = []
    for account in accounts:
        if not isinstance(account, dict) or not isinstance(account.get("provider"), str):
            errors.append({"input": "overlay", "error": "malformed_account"})
            continue
        provider = account["provider"]
        status = account.get("status")
        observed = _parse_ts(account.get("observed_at"))
        stale = observed is None or now - observed > thresholds["reading_stale_s"]
        if status == "ok" and not stale:
            continue
        alarms.append(
            _alarm(
                "reading_stale",
                provider,
                observed,
                {"status": status, "stale": stale},
            )
        )
    return alarms


def held_alarms(spec, thresholds, now, errors):
    """Attempts parked in the ACTIVE held state longer than held_s."""
    url = spec.get("database")
    if not url:
        return []
    try:
        rows = Ledger(url).status()
    except Exception as exc:
        errors.append({"input": "database", "error": type(exc).__name__})
        return []
    return [
        _alarm(
            "attempt_held",
            row["id"],
            row["updated"],
            {"task": row["task"], "reason": row["reason"]},
        )
        for row in rows
        if row["state"] == "held" and now - row["updated"] > thresholds["held_s"]
    ]


def _productive(results):
    for result in results:
        if not isinstance(result, dict):
            continue
        verdict = result.get("result")
        if isinstance(verdict, str) and any(t in verdict for t in PRODUCTIVE_RESULTS):
            return True
    return False


def _board_ready(board_dir):
    ready = 0
    for path in Path(board_dir).glob("*.json"):
        try:
            task = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(task, dict) and task.get("state") == "ready":
            ready += 1
    return ready


def board_alarms(spec, thresholds, errors):
    """A board with ready tasks whose last no_dispatch_passes tick-log passes were
    all unproductive. Passes are the log's JSON lines for that board; the pass
    that started the stall is the alarm's since."""
    alarms = []
    for board in spec.get("boards", []):
        board_dir = board.get("board_dir")
        log_path = board.get("tick_log")
        if not board_dir or not log_path:
            continue
        try:
            lines = Path(log_path).read_text().splitlines()
        except OSError as exc:
            errors.append(
                {"input": "tick_log", "board": board.get("name"), "error": type(exc).__name__}
            )
            continue
        passes = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("board") == str(board_dir):
                passes.append(entry)
        needed = thresholds["no_dispatch_passes"]
        if len(passes) < needed:
            continue
        tail = passes[-needed:]
        if any(_productive(p.get("results", [])) for p in tail):
            continue
        ready = _board_ready(board_dir)
        if not ready:
            continue
        alarms.append(
            _alarm(
                "board_stalled",
                board.get("name") or board_dir,
                _parse_ts(tail[0].get("time")),
                {"ready": ready, "passes": needed},
            )
        )
    return alarms


def upload_alarms(spec, thresholds, now, errors):
    """The uploader's status record: missing, too old, or not ok."""
    path = spec.get("upload_status")
    if not path:
        return []
    try:
        stat = Path(path).stat()
    except OSError:
        return [_alarm("upload_stale", "upload", None, {"missing": True})]
    age = now - stat.st_mtime
    if age <= thresholds["upload_stale_s"]:
        try:
            status = json.loads(Path(path).read_text()).get("status")
        except (OSError, ValueError):
            errors.append({"input": "upload_status", "error": "unreadable"})
            return []
        if status == "ok":
            return []
    else:
        try:
            status = json.loads(Path(path).read_text()).get("status")
        except (OSError, ValueError):
            status = None
    return [
        _alarm("upload_stale", "upload", stat.st_mtime, {"status": status, "age_s": round(age)})
    ]


def git_deploy_commit(deploy_dir):
    """The newest commit touching the deploy bundle, as the packet specifies."""
    out = subprocess.run(
        ["git", "-C", str(deploy_dir), "log", "-1", "--format=%H", "--", "."],
        capture_output=True,
        timeout=30,
        check=False,
    )
    commit = out.stdout.decode().strip()
    return commit or None


def _fetch(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=15) as response:
        return response.headers.get("Content-Type", ""), response.read()


def cloud_alarms(spec, fetch, deploy_commit, errors):
    """cloud_behind when the cloud's commit differs from the deploy bundle's;
    cloud_health_unversioned when the cloud's build cannot be confirmed at all."""
    url = spec.get("cloud_health_url")
    if not url:
        return []
    deploy = None
    if spec.get("deploy_dir"):
        try:
            deploy = deploy_commit(spec["deploy_dir"])
        except Exception as exc:
            errors.append({"input": "deploy_dir", "error": type(exc).__name__})
            return []
        if not deploy:
            errors.append({"input": "deploy_dir", "error": "commit_unavailable"})
            return []
    try:
        content_type, body = fetch(url)
    except Exception as exc:
        return [_alarm("cloud_health_unversioned", "cloud", None, {"error": type(exc).__name__})]
    try:
        document = json.loads(body)
    except ValueError:
        document = None
    if not isinstance(document, dict) or not isinstance(document.get("commit"), str):
        return [
            _alarm(
                "cloud_health_unversioned",
                "cloud",
                None,
                {"content_type": content_type},
            )
        ]
    if not deploy:
        errors.append({"input": "cloud_health_url", "error": "no_deploy_dir_to_compare"})
        return []
    if document["commit"] == deploy:
        return []
    return [
        _alarm(
            "cloud_behind",
            "cloud",
            None,
            {"cloud_commit": document["commit"], "deploy_commit": deploy},
        )
    ]


def evaluate(alarms, state, renotify_s, now):
    """Edge-trigger the current alarm set against the persisted state.

    Returns (delta, new_entries): raised when an alarm first appears, re-raised
    after renotify_s while it persists, cleared when it goes. Entries persist with
    their original since, so a re-raise names the whole duration of the problem.
    """
    current = {alarm["kind"] + "\x1f" + alarm["key"]: alarm for alarm in alarms}
    delta = []
    kept = []
    for entry in state:
        key = entry["kind"] + "\x1f" + entry["key"]
        alarm = current.pop(key, None)
        if alarm is None:
            delta.append({"event": "cleared", **entry})
            continue
        if now - entry["notified_at"] >= renotify_s:
            delta.append({"event": "renotified", **alarm})
            kept.append({**entry, "notified_at": now})
        else:
            kept.append(entry)
    for alarm in current.values():
        delta.append({"event": "raised", **alarm})
        kept.append(
            {
                "kind": alarm["kind"],
                "key": alarm["key"],
                "since": alarm["since"],
                "notified_at": now,
            }
        )
    return delta, kept


def run_notify(path, message):
    """Run one operator notifier with the message on stdin; returns an error name or None."""
    try:
        subprocess.run(
            [path],
            input=message.encode(),
            capture_output=True,
            timeout=NOTIFY_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return type(exc).__name__
    return None


def watch(spec, now=None, fetch=None, deploy_commit=None, notify=None):
    """One watch pass. Returns {"errors": [...], "delta": [...]} — the same text is
    printed by the CLI and piped to every notifier in spec["notify"].

    now, fetch, deploy_commit and notify are seams for tests and embedding: the
    defaults read the wall clock, fetch the health URL with urllib, resolve the
    deploy commit with git, and run each notifier as a subprocess with the message
    on stdin.
    """
    now = time.time() if now is None else now
    if callable(now):
        now = now()
    fetch = _fetch if fetch is None else fetch
    deploy_commit = git_deploy_commit if deploy_commit is None else deploy_commit
    notify = run_notify if notify is None else notify
    thresholds = dict(DEFAULT_THRESHOLDS, **(spec.get("thresholds") or {}))
    renotify_s = spec.get("renotify_s", thresholds.get("renotify_s", DEFAULT_RENOTIFY_S))
    errors = []
    alarms = (
        reading_alarms(spec, thresholds, now, errors)
        + held_alarms(spec, thresholds, now, errors)
        + board_alarms(spec, thresholds, errors)
        + upload_alarms(spec, thresholds, now, errors)
        + cloud_alarms(spec, fetch, deploy_commit, errors)
    )
    state_path = spec.get("state")
    state = []
    if state_path and Path(state_path).is_file():
        try:
            state = json.loads(Path(state_path).read_text()).get("alarms", [])
        except (OSError, ValueError):
            errors.append({"input": "state", "error": "unreadable"})
    delta, kept = evaluate(alarms, state, renotify_s, now)
    output = {"errors": errors, "delta": delta}
    if state_path and delta:
        target = Path(state_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"alarms": kept}))
        tmp.replace(target)
    if delta:
        message = json.dumps(output, indent=2)
        for path in spec.get("notify", []):
            error = notify(path, message)
            if error:
                errors.append({"input": "notify", "path": path, "error": error})
                output["errors"] = errors
    return output
