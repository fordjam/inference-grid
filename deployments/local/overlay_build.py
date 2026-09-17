"""Build the loopback dashboard overlay: collector readings plus the grid's attempts.

No credentials, prompts or private paths are written; attempts carry task id, provider,
model, state, time. Every input and output path comes from the config
(``output_dir``, ``board_db``, ``go_live_path``, ``claude_overlay_path``, ``package_src``).
"""

import glob
import json
import os
import sqlite3
import sys
import time
import datetime
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
BOARD_DB = Path.home() / ".local/share/inference-grid/board.sqlite"
LANE_PROVIDER = {
    "zai": "zai",
    "zcode": "zai",
    "go": "opencode",
    "goat": "command-code",
    "cline": "clinepass",
}
# Lane account -> (overlay provider, display source)
ZAI_SOURCE = {"zcode": ("zai", "ZCode"), "zai": ("zai", "Claude Code headless")}


def prior_accounts(overlay_path):
    """Every account from the previous overlay except zai (replaced below)."""
    try:
        prior = json.loads(Path(overlay_path).read_text())
        entries = prior if isinstance(prior, list) else prior.get("accounts", [])
        return [a for a in entries if a.get("provider") != "zai"]
    except (OSError, ValueError):
        return []


def zai_account(config):
    """The collector reading; when it is not ok, the operator's ledger quota file."""
    out_dir = Path(config.get("output_dir", DEFAULT_DIR))
    try:
        z = json.loads((out_dir / "zai-observation.json").read_text())
        if z.get("status") != "ok":
            raise ValueError("zai_observation_not_ok")
        return {
            "provider": "zai",
            "observed_at": z["observed_at"],
            "status": "ok",
            "windows": [
                {"id": w["id"], "used_percent": w["used_percent"], "resets_at": w.get("resets_at")}
                for w in z["windows"]
            ],
        }
    except (OSError, ValueError, KeyError):
        try:
            q = json.loads(
                Path(
                    config.get(
                        "zai_quota_path", Path.home() / ".config/inference-grid/zai-quota.json"
                    )
                ).read_text()
            )
            return {
                "provider": "zai",
                "observed_at": q["observed_at"],
                "status": "ok",
                "windows": [
                    {
                        "id": "five_hour",
                        "used_percent": q["five_hour_used_percent"],
                        "reset_label": "operator console reading",
                    },
                    {
                        "id": "weekly",
                        "used_percent": q["weekly_used_percent"],
                        "reset_label": "operator console reading",
                    },
                ],
            }
        except (OSError, ValueError, KeyError):
            return {"provider": "zai", "observed_at": None, "status": "unknown", "windows": []}


def opencode_account(config):
    """The go-live observation, its reset labels resolved through reset_display."""
    try:
        if config.get("package_src"):
            sys.path.insert(0, str(config["package_src"]))
        from inference_grid.reset_display import reset_instant

        g = json.loads(
            Path(config.get("go_live_path", DEFAULT_DIR / "go-live-observation.json")).read_text()
        )
        ts = datetime.datetime.fromisoformat(g["observed_at"].replace("Z", "+00:00")).timestamp()
        windows = []
        for w in g.get("windows", []):
            try:
                inst = reset_instant(ts, w.get("reset_display"))
            except ValueError:
                inst = None
            windows.append(
                {
                    "id": w["id"],
                    "used_percent": w.get("used_percent"),
                    "resets_at": datetime.datetime.fromtimestamp(
                        inst["resets_at"], datetime.timezone.utc
                    ).isoformat()
                    if inst
                    else None,
                    "reset_label": w.get("reset_display"),
                }
            )
        return {
            "provider": "opencode",
            "observed_at": g["observed_at"],
            "status": "ok" if g.get("status") == "fresh_console_observation" else "unknown",
            "windows": windows,
        }
    except (OSError, ValueError, KeyError, ImportError):
        return None


def codex_rollup(home):
    """The newest rate_limits event in one codex home's session rollouts, or None."""
    home = str(home)
    files = sorted(
        glob.glob(os.path.join(home, "sessions", "*", "*", "*", "*.jsonl")), key=os.path.getmtime
    )

    def find(obj, key):
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for value in obj.values():
                found = find(value, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = find(value, key)
                if found is not None:
                    return found
        return None

    for path in reversed(files):
        snap = None
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    found = find(obj, "rate_limits")
                    if found:
                        snap = {"snapshot": found, "ts": obj.get("timestamp") or obj.get("ts")}
        except OSError:
            continue
        if snap:
            return snap
    return None


def codex_account(config):
    """Codex: all homes share one account. The newest rollup per home is merged so each window
    carries the newest future reset it has, labelled with the reading it came from."""
    homes = config.get("codex_homes") or [Path.home() / ".codex"] + sorted(
        Path.home().glob(".codex-seat*")
    )
    readings = []
    for h in homes:
        h = Path(h)
        snap = codex_rollup(h)
        if not snap:
            continue
        s = snap["snapshot"]
        n = {"reached": s.get("rate_limit_reached_type")}
        for key in ("primary", "secondary"):
            w = s.get(key) or {}
            prefix = {300: "h5", 10080: "wk"}.get(w.get("window_minutes"))
            if (
                prefix
                and isinstance(w.get("used_percent"), (int, float))
                and not isinstance(w.get("used_percent"), bool)
            ):
                n[prefix + "_used"] = w["used_percent"]
                n[prefix + "_resets_at"] = w.get("resets_at")
        readings.append((snap["ts"], n, h.name))
    readings.sort(reverse=True)
    if not readings:
        return None
    latest_ts, latest, _ = readings[0]
    windows = []
    nowts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for prefix, wid in (("h5", "five_hour"), ("wk", "weekly")):
        src = next(
            (
                (ts, n, home)
                for ts, n, home in readings
                if n.get(prefix + "_resets_at") is not None and n[prefix + "_resets_at"] > nowts
            ),
            None,
        )
        resets = None
        label = None
        if src:
            ts, n, home = src
            resets = datetime.datetime.fromtimestamp(
                n[prefix + "_resets_at"], datetime.timezone.utc
            ).isoformat()
            if ts != latest_ts:
                label = "reset from " + home + " reading " + ts[:16] + "Z"
        windows.append(
            {
                "id": wid,
                "used_percent": latest.get(prefix + "_used"),
                "resets_at": resets,
                "reset_label": label,
            }
        )
    return {
        "provider": "codex",
        "observed_at": latest_ts,
        "status": "error" if latest.get("reached") else "ok",
        "windows": windows,
        "source_reason": latest.get("reached"),
    }


def replace_account(accounts, account):
    provider = account["provider"]
    return [a for a in accounts if a.get("provider") != provider] + [account]


def attempts(board_db):
    """The board's attempts, newest first: task, provider, model, state, time. Nothing else."""
    rows = []
    try:
        con = sqlite3.connect("file:" + str(board_db) + "?mode=ro", uri=True)
        for task, state, updated, spec, account in con.execute(
            "select a.task,a.state,a.updated,t.spec,a.account from attempts a join tasks t on t.id=a.task"
        ):
            model = json.loads(spec).get("model", "?")
            lane = account.replace("-account", "")
            provider = LANE_PROVIDER.get(lane, "unknown")
            rows.append(
                {
                    "task": task,
                    "provider": provider,
                    "model": model + " via " + lane,
                    "status": state,
                    "at": datetime.datetime.fromtimestamp(
                        updated, datetime.timezone.utc
                    ).isoformat(),
                }
            )
        con.close()
    except (sqlite3.Error, ValueError):
        return []
    rows.sort(key=lambda a: a["at"], reverse=True)
    return rows


CAPACITY_PANEL_WINDOW_SECONDS = 86400  # "this window": the last 24h of resolved attempts


def capacity_panel(board_db, now=None, window_seconds=CAPACITY_PANEL_WINDOW_SECONDS):
    """02-B5: per subscription, capacity consumed this window by attempts that landed
    vs failed/abandoned. Read-only: no write to board.sqlite anywhere here.

    "Landed" here is attempts.state == "accepted", the ledger's own terminal success
    state -- true board-level "landed" status lives in grid/board/*.json task files,
    out of scope for this sqlite-only, read-only panel (rule: never touch grid/ board
    state). "Consumed" prefers the outcome_recorded event's usage dict for that
    attempt (the actual recorded usage) when one exists, else falls back to the
    attempt's own pre-run estimate dict. A failed attempt's estimate was debited from
    the account's windows (ledger.py's resolve(), outcome="consumed"); an abandoned
    attempt's was released and never debited (outcome="released" -- state="abandoned"
    follows only from that path), so an abandoned attempt always contributes 0.
    """
    now = time.time() if now is None else now
    floor = now - window_seconds
    try:
        con = sqlite3.connect("file:" + str(board_db) + "?mode=ro", uri=True)
        attempt_rows = con.execute(
            "select id, account, state, estimate, updated from attempts "
            "where state in ('accepted','failed','abandoned') and updated >= ?",
            (floor,),
        ).fetchall()
        usage_by_attempt = {}
        for aid, detail_raw in con.execute(
            "select attempt, detail from events where kind='outcome_recorded'"
        ):
            try:
                detail = json.loads(detail_raw) if isinstance(detail_raw, str) else detail_raw
            except (TypeError, ValueError):
                continue
            usage = detail.get("usage") if isinstance(detail, dict) else None
            if isinstance(usage, dict):
                usage_by_attempt[aid] = usage
        con.close()
    except (sqlite3.Error, ValueError):
        return []

    def numeric_sum(mapping):
        if not isinstance(mapping, dict):
            return 0.0
        return sum(v for v in mapping.values() if isinstance(v, (int, float)))

    by_provider = {}
    for aid, account, state, estimate_raw, _updated in attempt_rows:
        lane = account.replace("-account", "")
        provider = LANE_PROVIDER.get(lane, "unknown")
        bucket = "landed" if state == "accepted" else "failed_abandoned"
        if state == "abandoned":
            consumed = 0.0
        else:
            usage = usage_by_attempt.get(aid)
            if usage:
                consumed = numeric_sum(usage)
            else:
                try:
                    estimate = (
                        json.loads(estimate_raw) if isinstance(estimate_raw, str) else estimate_raw
                    )
                except (TypeError, ValueError):
                    estimate = {}
                consumed = numeric_sum(estimate)
        entry = by_provider.setdefault(
            provider,
            {
                "provider": provider,
                "landed_consumed": 0.0,
                "landed_count": 0,
                "failed_abandoned_consumed": 0.0,
                "failed_abandoned_count": 0,
            },
        )
        entry[bucket + "_consumed"] += consumed
        entry[bucket + "_count"] += 1
    return sorted(by_provider.values(), key=lambda r: r["provider"])


def build(config):
    out_dir = Path(config.get("output_dir", DEFAULT_DIR))
    accounts = prior_accounts(
        config.get("claude_overlay_path", "/private/tmp/grid-pwa-build/overlay.json")
    )
    accounts.append(zai_account(config))
    opencode = opencode_account(config)
    if opencode:
        accounts = replace_account(accounts, opencode)
    codex = codex_account(config)
    if codex:
        accounts = replace_account(accounts, codex)
    try:
        cx = json.loads((out_dir / "codex-observation.json").read_text())
        if cx.get("status") == "ok":
            accounts = replace_account(accounts, cx)
    except (OSError, ValueError):
        pass
    for name, provider in (
        ("goat-observation.json", "command-code"),
        ("cline-observation.json", "clinepass"),
    ):
        try:
            obs = json.loads((out_dir / name).read_text())
            accounts = replace_account(accounts, obs)
        except (OSError, ValueError):
            pass
    overlay = {
        "accounts": accounts,
        "attempts": attempts(config.get("board_db", BOARD_DB))[:60],
    }
    overlay.update(operator_lists(config))
    overlay["boards"] = boards_section(config)
    overlay["capacity_panel"] = capacity_panel(config.get("board_db", BOARD_DB))
    return overlay


def boards_section(config):
    """Per board: planned/active/blocked/landed-today rows, from the read-only data node.

    Local only: task ids and titles are the operator's project names, so `upload.py` drops
    this key and the cloud's `clean_snapshot` refuses it. An unreadable package, board or
    ledger yields an empty list — the overlay must still be written.
    """
    try:
        if config.get("package_src"):
            sys.path.insert(0, str(config["package_src"]))
        from inference_grid.boards import boards as read_boards

        board_configs = []
        for entry in config.get("boards") or []:
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            entry.setdefault("lanes_path", config.get("lanes_path"))
            entry.setdefault("packets_root", config.get("packets_root"))
            board_configs.append(entry)
        return read_boards(
            database=config.get("database_url", "sqlite:///" + str(BOARD_DB)),
            boards=board_configs,
            packets_root=config.get("packets_root"),
        )
    except Exception:  # noqa: BLE001 — the overlay must still be written
        return []


def operator_lists(config):
    """The needs-you rows and the weekly accepted-work metric (brief 14 C2), from the ledger,
    the boards' blocked tasks, watch's state file and an optional owner-decisions file.
    Empty lists when the package or the ledger is unavailable: the dashboard renders without."""
    try:
        if config.get("package_src"):
            sys.path.insert(0, str(config["package_src"]))
        from inference_grid.ledger import Ledger
        from inference_grid.operator_queue import build_overlay

        ledger = Ledger(config.get("database_url", "sqlite:///" + str(BOARD_DB)))
        board_dirs = [b["board_dir"] for b in config.get("boards", []) if isinstance(b, dict)]
        return build_overlay(
            ledger,
            board_dirs=board_dirs,
            watch_state=config.get("watch_state"),
            owner_decisions=config.get("owner_decisions"),
        )
    except Exception as exc:  # noqa: BLE001 — the overlay must still be written
        return {
            "operator": [],
            "accepted_work": [],
            "reviewer_recall": [],
            "operator_error": type(exc).__name__,
        }


def write(overlay, config):
    out = Path(config.get("output_dir", DEFAULT_DIR)) / "overlay.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(overlay))
    os.chmod(tmp, 0o600)
    tmp.replace(out)
    return out


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    overlay = build(config)
    write(overlay, config)
    print(
        json.dumps(
            {
                "accounts": [a["provider"] for a in overlay["accounts"]],
                "attempts": len(overlay["attempts"]),
            }
        )
    )


if __name__ == "__main__":
    main()
