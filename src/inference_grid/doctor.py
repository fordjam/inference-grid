"""Read-only local diagnostics; never authenticates or dispatches a provider."""

import math
import os
from pathlib import Path
import shutil
import time

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url

from .ledger import accounts, attempts, cooldowns, outbox, tasks


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


def diagnose(url, now=None):
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
            required = {t.name for t in (accounts, attempts, cooldowns, outbox, tasks)}
            if not required.issubset(inspect(con).get_table_names()):
                result["findings"].append("schema_missing_run_init")
                return result
            # Never print stored argv, URLs, credentials or exception text.
            active_cooldowns = list(
                con.execute(select(cooldowns.c.endpoint).where(cooldowns.c.until > now)).scalars()
            )
            account_rows = list(con.execute(select(accounts.c.expires)).scalars())
            states = list(con.execute(select(attempts.c.state)).scalars())
            specs = list(con.execute(select(tasks.c.spec)).scalars())
            pending = len(
                list(con.execute(select(outbox.c.attempt).where(outbox.c.sent.is_(None))))
            )
        result["database"] = "readable"
        result["counts"] = {
            "accounts": len(account_rows),
            "inference_cooldowns": active_cooldowns.count("inference"),
            "usage_cooldowns": active_cooldowns.count("usage"),
            "stale_accounts": sum(not math.isfinite(x) or x <= now for x in account_rows),
            "queued": states.count("queued"),
            "dispatching": states.count("dispatching"),
            "held": states.count("held"),
            "pending_publications": pending,
            "missing_executables": sum(executable_state(s) == "missing" for s in specs),
            "unresolved_executables": sum(executable_state(s) == "unresolved" for s in specs),
            "invalid_adapter_specs": sum(executable_state(s) == "invalid" for s in specs),
        }
        for count, finding in (
            (not account_rows, "no_accounts_configured"),
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
