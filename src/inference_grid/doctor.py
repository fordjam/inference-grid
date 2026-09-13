"""Read-only local diagnostics; never authenticates or dispatches a provider."""

import json
import math
import os
from pathlib import Path
import shutil
import time

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url

from .collector import collection_claims
from .lane_readiness import lane_readiness
from .ledger import accounts, attempts, cooldowns, lanes, outbox, tasks

MAX_BOARD_DIRS = 8


def board_counts(board_dir):
    """Task counts by state for one board directory; unreadable files count as invalid.

    Read-only: only top-level task files are read, and nothing is dispatched or changed.
    """
    path = Path(board_dir)
    if not path.is_dir():
        return {"missing": 1}
    from .board.task import validate_task

    counts = {}
    for file in sorted(path.glob("*.json")):
        try:
            task = validate_task(json.loads(file.read_text()))
        except Exception:
            counts["invalid"] = counts.get("invalid", 0) + 1
            continue
        # The board's recorded-change convention: a blocked_reason beginning with
        # "superseded:" closes the predecessor of a retried task, so it is not open work.
        if task["state"] == "blocked" and str(task["blocked_reason"]).startswith("superseded:"):
            counts["superseded"] = counts.get("superseded", 0) + 1
        else:
            counts[task["state"]] = counts.get(task["state"], 0) + 1
    return counts


def executable_state(spec):
    if not isinstance(spec, dict):
        return "invalid"
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or not isinstance(argv[0], str) or not argv[0]:
        return "invalid"
    command = argv[0]
    # Worker cwd is a new per-attempt directory, not this diagnostic process cwd.
    if not Path(command).is_absolute():
        if "/" in command or any(not Path(p).is_absolute() for p in os.get_exec_path()):
            return "unresolved"
    return "present" if shutil.which(command) is not None else "missing"


def diagnose(url, now=None, boards=None):
    now = time.time() if now is None else now
    result = {
        "scope": "ledger_and_adapter_executables",
        "status": "attention",
        "database": "unavailable",
        "provider_auth": "not_checked",
        "broker": "not_checked",
        "findings": [],
        "counts": {},
    }
    if isinstance(boards, list):
        named = [b for b in boards[:MAX_BOARD_DIRS] if isinstance(b, str) and b]
        if named:
            result["boards"] = {name: board_counts(name) for name in named}
    engine = None
    try:
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
            if not Path(parsed.database).is_file():
                result["findings"].append("database_missing_run_init")
                return result
        connect_args = {"connect_timeout": 5} if parsed.get_backend_name() == "postgresql" else {}
        engine = create_engine(url, connect_args=connect_args)
        with engine.connect() as con:
            required = {
                t.name for t in (accounts, attempts, cooldowns, outbox, tasks, collection_claims)
            }
            if not required.issubset(inspect(con).get_table_names()):
                result["findings"].append("schema_missing_run_init")
                return result
            # Never print stored argv, URLs, credentials or exception text.
            active_cooldowns = list(
                con.execute(select(cooldowns.c.endpoint).where(cooldowns.c.until > now)).scalars()
            )
            collecting = list(
                con.execute(
                    select(collection_claims.c.deadline).where(
                        collection_claims.c.token.is_not(None)
                    )
                ).scalars()
            )
            account_rows = list(con.execute(select(accounts.c.expires)).scalars())
            states = list(con.execute(select(attempts.c.state)).scalars())
            specs = list(con.execute(select(tasks.c.spec)).scalars())
            pending = len(
                list(con.execute(select(outbox.c.attempt).where(outbox.c.sent.is_(None))))
            )
            lane_rows = list(con.execute(select(lanes.c.record)).scalars())
        result["database"] = "readable"
        result["counts"] = {
            "accounts": len(account_rows),
            "collecting": sum(t > now for t in collecting),
            "stuck_collections": sum(t <= now for t in collecting),
            "inference_cooldowns": active_cooldowns.count("inference"),
            "usage_cooldowns": active_cooldowns.count("usage"),
            "stale_accounts": sum(not math.isfinite(x) or x <= now for x in account_rows),
            "queued": states.count("queued"),
            "dispatching": states.count("dispatching"),
            "held": states.count("held"),
            "resolved": states.count("abandoned") + states.count("failed"),
            "pending_publications": pending,
            "missing_executables": sum(executable_state(s) == "missing" for s in specs),
            "unresolved_executables": sum(executable_state(s) == "unresolved" for s in specs),
            "invalid_adapter_specs": sum(executable_state(s) == "invalid" for s in specs),
        }
        result["lanes"] = []
        for record in lane_rows:
            try:
                classified = lane_readiness(record, now)
            except ValueError:
                classified = {
                    "provider": str(record.get("provider", "?"))[:40]
                    if isinstance(record, dict)
                    else "?",
                    "state": "invalid",
                    "reason": "invalid_lane_record",
                    "next_check_at": None,
                }
            result["lanes"].append(classified)
        for count, finding in (
            (
                sum(lane["state"] not in ("ready",) for lane in result["lanes"]),
                "provider_lanes_not_ready",
            ),
            (not account_rows, "no_accounts_configured"),
            (result["counts"]["stuck_collections"], "collection_reconciliation_required"),
            (result["counts"]["inference_cooldowns"], "provider_inference_cooldown_active"),
            (result["counts"]["usage_cooldowns"], "usage_collection_cooldown_active"),
            (result["counts"]["stale_accounts"], "refresh_stale_account_observations"),
            (result["counts"]["held"], "native_reconciliation_required_do_not_retry_blindly"),
            (result["counts"]["missing_executables"], "adapter_executable_unavailable"),
            (
                result["counts"]["unresolved_executables"],
                "relative_executable_requires_attempt_context",
            ),
            (result["counts"]["invalid_adapter_specs"], "invalid_adapter_spec"),
        ):
            if count:
                result["findings"].append(finding)
        result["status"] = "attention" if result["findings"] else "checks_passed"
    except Exception:
        result["findings"].append("database_or_schema_unreadable")
    finally:
        if engine is not None:
            engine.dispose()
    return result
