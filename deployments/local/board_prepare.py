"""Refresh the board ledger's accounts and lane records from live readings. No credentials.

Reads the operator's Z.ai quota file, the go-live observation and the goat/cline quota
observations, and reconfigures the zai/zcode/go/goat-account/cline accounts plus their lane
records. The goat and cline lane ids and models come from the config (``goat_lanes`` /
``cline_lanes``, lists of ``{lane, model}``); with either absent that account is left alone.
Paths come from the config (``database_url``, ``zai_quota_path``, ``go_live_path``,
``goat_observation_path``, ``cline_observation_path``); the inference_grid package is expected
installed in the interpreter that runs this (``package_src`` may point at a checkout when it
is not).
"""

import json
import datetime
import sys
import time
from pathlib import Path

from inference_grid.ledger import Ledger, Refused
from inference_grid.flash_window import flash_window

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
DATABASE_URL = "sqlite:///" + str(Path.home() / ".local/share/inference-grid/board.sqlite")

# Policy: the Z.ai reading is operator-attested (no usage API) unless it came from the
# collector; it keeps its true timestamp and is treated as valid for 24 h attested, 15 min
# from the API. ZCode consumes no plan quota inside the campaign window.
ZAI_API_VALID = 900
ZAI_ATTESTED_VALID = 86400
ZAI_PLAN = {"five_hour": 2000, "weekly": 10000}
GO_WINDOW_UNITS = {"five_hour": 12, "weekly": 30, "monthly": 60}
GO_VALID = 900
# The GOAT plan's caps are 14 / 35 / 70 credits (five-hour / weekly / monthly, per
# collect_goat.py); ClinePass publishes no unit caps, so its windows read against a
# 100-unit scale per window. Both observations keep their value for 15 minutes.
GOAT_WINDOW_UNITS = {"five_hour": 14, "weekly": 35, "monthly": 70}
CLINE_WINDOW_UNITS = {"five_hour": 100, "weekly": 100, "monthly": 100}
GOAT_VALID = 900
CLINE_VALID = 900
ADMISSION_LIMIT_PERCENT = 80


def admission_limit(config):
    """The used-percent at which a lane stops admitting work: `admission_limit_percent`
    in config.json, else 80. The operator set it to 100 on 2026-09-16 — a plan is spent
    when it is spent, and the lane rolls to the next provider at 100, not at 80."""
    value = config.get("admission_limit_percent", ADMISSION_LIMIT_PERCENT)
    return int(value) if 0 <= int(value) <= 100 else ADMISSION_LIMIT_PERCENT


def _ts(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def configure(config, ledger):
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    lanes = {}
    quota = json.loads(
        Path(
            config.get("zai_quota_path", Path.home() / ".config/inference-grid/zai-quota.json")
        ).read_text()
    )
    zt = _ts(quota["observed_at"])
    zrem = {
        window: ZAI_PLAN[window] * (100 - quota[window + "_used_percent"]) / 100
        for window in ("five_hour", "weekly")
    }
    zai_valid = ZAI_API_VALID if quota.get("source") == "api" else ZAI_ATTESTED_VALID
    # the Z.ai plan serves both the zai headless lane and the zcode lane
    for lane in ("zai", "zcode"):
        try:
            ledger.configure_account(
                lane + "-account",
                1,
                zrem,
                zt + zai_valid,
                ["glm-5.3-flash"],
                [lane],
                observed_at=zt,
            )
        except Refused as exc:
            print(lane, "account", exc)
        lanes[lane] = {
            "provider": lane,
            "auth": "ok",
            "quota_observed_at": zt,
            "quota_freshness_seconds": zai_valid,
            "used_percent_max": float(
                max(quota["five_hour_used_percent"], quota["weekly_used_percent"])
            ),
            "admission_limit_percent": admission_limit(config),
            "cooldown_until": None,
            "qualification": "qualified",
            "blocked_until": None,
            "blocker": None,
        }
    go = json.loads(
        Path(config.get("go_live_path", DEFAULT_DIR / "go-live-observation.json")).read_text()
    )
    gt = _ts(go["observed_at"])
    grem = {
        w["id"]: GO_WINDOW_UNITS[w["id"]] * (1 - (w["used_percent"] + 1) / 100)
        for w in go["windows"]
        if w["id"] in GO_WINDOW_UNITS
    }
    try:
        ledger.configure_account(
            "go-account",
            1,
            grem,
            gt + GO_VALID,
            ["glm-5.3-flash", "kimi-k3"],
            ["go", "go-kimi"],
            observed_at=gt,
        )
    except Refused as exc:
        print("go account", exc)
    go_lane = {
        "provider": "go",
        "auth": "ok" if go.get("owner_verified") else "unknown",
        "quota_observed_at": gt,
        "quota_freshness_seconds": GO_VALID,
        "used_percent_max": float(max(w["used_percent"] for w in go["windows"])),
        "admission_limit_percent": admission_limit(config),
        "cooldown_until": None,
        "qualification": "qualified",
        "blocked_until": None,
        "blocker": None,
    }
    lanes["go"] = go_lane
    lanes["go-kimi"] = dict(go_lane, provider="go-kimi")
    for lane, record in lanes.items():
        print(lane, ledger.record_lane(lane, record)["state"])
    configure_goat(config, ledger, now)
    configure_cline(config, ledger, now)
    print("zcode window", flash_window(now))


def _read_observation(config, key, default):
    try:
        return json.loads(Path(config.get(key, default)).read_text())
    except OSError:
        return None


def observation_record(config, obs, window_units, valid):
    """One observation file -> (lane record, observed, used): the builder configure_observation uses.

    Shared with the board runner, which re-reads a provider's observation file at admission
    time (brief L1) instead of copying this: one observation shape, one policy. With
    ``window_units=None`` every window carrying a numeric ``used_percent`` counts. The
    record's ``quota_observed_at`` is None when the file is missing, not ok, or carries no
    counted window; ``observed`` is the file's own timestamp, the freshness test's other
    operand; ``used`` is the per-window used-percent map the account's remaining units
    come from.
    """
    observed = _ts(obs["observed_at"]) if obs else None
    if window_units is None:
        used = {
            w["id"]: w["used_percent"]
            for w in (obs or {}).get("windows", [])
            if isinstance(w.get("used_percent"), (int, float))
            and not isinstance(w["used_percent"], bool)
        }
    else:
        used = {
            w["id"]: w["used_percent"]
            for w in (obs or {}).get("windows", [])
            if w.get("id") in window_units
            and isinstance(w["used_percent"], (int, float))
            and not isinstance(w["used_percent"], bool)
        }
    ok = obs is not None and obs.get("status") == "ok" and used
    record = {
        "auth": "ok",
        "quota_observed_at": observed if ok else None,
        "quota_freshness_seconds": valid,
        "used_percent_max": float(max(used.values())) if used else None,
        "admission_limit_percent": admission_limit(config),
        "cooldown_until": None,
        "qualification": "qualified",
        "blocked_until": None,
        "blocker": None,
    }
    return record, observed, used


def configure_observation(
    config, ledger, account, obs, window_units, valid, lanes_config, now, capacity=1
):
    """One observation file -> configure_account plus a lane record per configured lane.

    A missing, non-ok or older-than-``valid`` observation leaves the account untouched and
    records every configured lane stale.
    """
    if not lanes_config:
        return
    lane_ids = [entry["lane"] for entry in lanes_config]
    models = list(dict.fromkeys(entry["model"] for entry in lanes_config))
    record, observed, used = observation_record(config, obs, window_units, valid)
    ok = record["quota_observed_at"] is not None
    if ok and observed + valid > now:
        remaining = {w: window_units[w] * (100 - used[w]) / 100 for w in used}
        try:
            ledger.configure_account(
                account,
                capacity,
                remaining,
                observed + valid,
                models,
                lane_ids,
                observed_at=observed,
            )
        except Refused as exc:
            print(account, exc)
    for lane in lane_ids:
        print(lane, ledger.record_lane(lane, dict(record, provider=lane))["state"])


def configure_goat(config, ledger, now=None):
    configure_observation(
        config,
        ledger,
        "goat-account",
        _read_observation(config, "goat_observation_path", DEFAULT_DIR / "goat-observation.json"),
        GOAT_WINDOW_UNITS,
        GOAT_VALID,
        config.get("goat_lanes") or [],
        time.time() if now is None else now,
        # concurrent attempts the subscription tolerates; the operator sets it from experience
        capacity=int(config.get("goat_capacity", 1)),
    )


def configure_cline(config, ledger, now=None):
    configure_observation(
        config,
        ledger,
        "cline",
        _read_observation(config, "cline_observation_path", DEFAULT_DIR / "cline-observation.json"),
        CLINE_WINDOW_UNITS,
        CLINE_VALID,
        config.get("cline_lanes") or [],
        time.time() if now is None else now,
        # concurrent attempts the subscription tolerates; the operator sets it from experience
        capacity=int(config.get("cline_capacity", 1)),
    )


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    if config.get("package_src"):
        sys.path.insert(0, str(config["package_src"]))
    ledger = Ledger(config.get("database_url", DATABASE_URL))
    ledger.initialize()
    configure(config, ledger)


if __name__ == "__main__":
    main()
