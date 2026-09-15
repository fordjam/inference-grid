"""Read-only local diagnostics; never authenticates or dispatches a provider."""

import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url

from .collector import collection_claims
from .lane_readiness import lane_readiness
from .ledger import (
    DEFAULT_ACCOUNTS,
    accounts,
    attempts,
    classifier_view,
    cooldowns,
    lanes,
    outbox,
    tasks,
)

MAX_BOARD_DIRS = 8
# The versions a lane's binary reports when it can be run at all; the handoff's 20 s
# budget for a `--version` probe.
LANE_VERSION_TIMEOUT = 20
# The one packaged kind that speaks HTTP instead of launching a process; every other
# kind is a CLI whose binary the operator names in lanes.json.
HTTP_KINDS = frozenset({"go_http"})


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


def run_command(argv, timeout):
    """Run one local command through the seam callers may fake.

    Both the launchd check and the lane-binary probe go through here, so a test can
    replace the runner once instead of monkeypatching `subprocess`.
    """
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _launchctl_list(run=None):
    """`launchctl list` output through the run seam; None when unavailable."""
    try:
        done = (run or run_command)(["/bin/launchctl", "list"], 30)
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def version_probe(executable, run=None, timeout=LANE_VERSION_TIMEOUT):
    """`<executable> --version` classified as available or a named unavailability.

    Run through the same seam the launchd check uses, so a fake executable exercises
    every outcome without a network call. A signal is named (`SIGKILL` is what macOS 27
    does to a binary whose signature a postinstall rewrote).
    """
    if not isinstance(executable, str) or not executable or not Path(executable).is_file():
        return {"status": "unavailable", "reason": "missing"}
    try:
        done = (run or run_command)([executable, "--version"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "reason": "timeout"}
    except OSError:
        # A path that exists but cannot be executed (permissions, broken interpreter).
        return {"status": "unavailable", "reason": "missing"}
    if done.returncode < 0:
        try:
            reason = signal.Signals(-done.returncode).name
        except ValueError:
            reason = "signal " + str(-done.returncode)
        return {"status": "unavailable", "reason": reason}
    if done.returncode != 0:
        return {"status": "unavailable", "reason": "exit " + str(done.returncode)}
    text = (done.stdout or "").strip() or (done.stderr or "").strip()
    version = text.splitlines()[0].strip() if text else ""
    return {"status": "available", "version": version}


def lane_binaries(lane_specs, run=None, timeout=LANE_VERSION_TIMEOUT):
    """Probe every configured lane: CLI kinds run `--version`, HTTP kinds are `n/a`."""
    probed = []
    for lane_id in sorted(lane_specs):
        spec = lane_specs[lane_id]
        kind = spec.get("kind") if isinstance(spec, dict) else None
        if kind in HTTP_KINDS:
            probed.append({"lane": lane_id, "kind": kind, "status": "n/a"})
            continue
        executable = spec.get("executable") if isinstance(spec, dict) else None
        probed.append(
            {
                "lane": lane_id,
                "kind": kind,
                **version_probe(executable, run=run, timeout=timeout),
            }
        )
    return probed


def board_config_findings(boards_dir):
    """Named gaps in the operator's board configs: absent dirs and unfilled keys."""
    findings = []
    if boards_dir is None:
        return findings, {}
    configs = {}
    path = Path(boards_dir)
    if not path.is_dir():
        findings.append("boards_dir_missing")
        return findings, configs
    for config_path in sorted(path.glob("*.json")):
        try:
            config = json.loads(config_path.read_text())
        except (OSError, ValueError):
            findings.append(f"board config unreadable: {config_path.name}")
            continue
        configs[config_path.name] = config
        if not Path(str(config.get("board_dir", ""))).is_dir():
            findings.append(f"board dir missing: {config_path.name}")
        if not Path(str(config.get("lanes_path", ""))).is_file():
            findings.append(f"lanes.json missing: {config_path.name}")
        if any(
            isinstance(value, str) and value.startswith("<operator") for value in config.values()
        ):
            findings.append(f"board config unfinished: {config_path.name}")
    return findings, configs


def packet_store(packets_root, held_workspaces):
    """Total bytes and the oldest held attempt under the configured packets root."""
    total = 0
    oldest = None
    root = Path(packets_root) if packets_root else None
    for workspace in held_workspaces:
        path = Path(workspace)
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            total += size
            if oldest is None or size > oldest[0]:
                oldest = (size, path.name)
    if root is not None and not root.is_dir():
        return total, oldest, "packets_root_missing"
    return total, oldest, None


def diagnose(
    url,
    now=None,
    boards=None,
    boards_dir=None,
    packets_root=None,
    launchd_label=None,
    launchctl_list=None,
    lane_specs=None,
    run=None,
):
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
            account_rows = list(
                con.execute(select(accounts.c.id, accounts.c.models, accounts.c.expires)).mappings()
            )
            states = list(con.execute(select(attempts.c.state)).scalars())
            specs = list(con.execute(select(tasks.c.spec)).scalars())
            pending = len(
                list(con.execute(select(outbox.c.attempt).where(outbox.c.sent.is_(None))))
            )
            lane_rows = list(con.execute(select(lanes.c.provider, lanes.c.record)).mappings())
            held_rows = [
                {"attempt": r["id"], "workspace": r["workspace"]}
                for r in con.execute(
                    select(attempts.c.id, attempts.c.workspace).where(attempts.c.state == "held")
                ).mappings()
            ]
        result["database"] = "readable"
        result["held_attempts"] = len(held_rows)
        result["counts"] = {
            "accounts": len(account_rows),
            "collecting": sum(t > now for t in collecting),
            "stuck_collections": sum(t <= now for t in collecting),
            "inference_cooldowns": active_cooldowns.count("inference"),
            "usage_cooldowns": active_cooldowns.count("usage"),
            # An unconfigured alias placeholder (no models) is intentional, not a lost
            # observation; a real account the operator configured going stale is.
            "stale_accounts": sum(
                (not math.isfinite(row["expires"]) or row["expires"] <= now)
                for row in account_rows
                if row["id"] not in DEFAULT_ACCOUNTS or row["models"]
            ),
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
        for row in lane_rows:
            record = row["record"]
            try:
                classified = lane_readiness(classifier_view(record), now)
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
        # A packaged lane module with no record in the ledger is a missing registration,
        # not silence: the operator registers it (docs/LANES.md) and canaries it (L1).
        from .lanes.runner import KINDS

        recorded = {row["provider"] for row in lane_rows}
        result["modules_without_lane"] = sorted(set(KINDS.values()) - recorded)
        for count, finding in (
            (
                sum(lane["state"] not in ("ready",) for lane in result["lanes"]),
                "provider_lanes_not_ready",
            ),
            (
                len(result["modules_without_lane"]),
                "lane_modules_without_records",
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
        # End to end: board configs, the scheduler's launchd presence, and the packet
        # store under the roots the operator passes. Nothing here is ever guessed from
        # the home directory.
        config_findings, configs = board_config_findings(boards_dir)
        result["boards_dir"] = {
            "path": str(boards_dir) if boards_dir else None,
            "configs": sorted(configs),
            "findings": config_findings,
        }
        result["findings"].extend(config_findings)
        if launchd_label:
            loaded = launchctl_list if launchctl_list is not None else _launchctl_list(run)
            loaded_labels = [
                line.split()[-1] for line in (loaded or "").splitlines() if line.strip()
            ]
            result["launchd"] = {"label": launchd_label, "loaded": launchd_label in loaded_labels}
            if launchd_label not in loaded_labels:
                result["findings"].append("scheduler_not_loaded")
        # Every configured CLI lane's binary is asked for its version through the run
        # seam; HTTP lanes have nothing to probe and are named n/a, never silently absent.
        if isinstance(lane_specs, dict) and lane_specs:
            result["lane_binaries"] = lane_binaries(lane_specs, run=run)
            for probed in result["lane_binaries"]:
                if probed["status"] == "unavailable":
                    result["findings"].append("lane_binary_unavailable: " + probed["lane"])
        total, biggest, packets_finding = packet_store(
            packets_root, [h["workspace"] for h in held_rows]
        )
        result["packet_store"] = {
            "root": str(packets_root) if packets_root else None,
            "bytes_under_held_workspaces": total,
            "largest_held": biggest[1] if biggest else None,
            "finding": packets_finding,
        }
        if packets_finding:
            result["findings"].append(packets_finding)
        result["status"] = "attention" if result["findings"] else "checks_passed"
    except Exception:
        result["findings"].append("database_or_schema_unreadable")
    finally:
        if engine is not None:
            engine.dispose()
    return result
