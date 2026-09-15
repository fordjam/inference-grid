"""Refresh the board ledger's accounts and lane records from live readings. No credentials.

Reads the operator's Z.ai quota file and the go-live observation, and reconfigures the
zai/zcode/go accounts plus their lane records. Paths come from the config (``database_url``,
``zai_quota_path``, ``go_live_path``); the inference_grid package is expected installed in the
interpreter that runs this (``package_src`` may point at a checkout when it is not).
"""

import json
import datetime
import sys
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
ADMISSION_LIMIT_PERCENT = 80


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
            "admission_limit_percent": ADMISSION_LIMIT_PERCENT,
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
        "admission_limit_percent": ADMISSION_LIMIT_PERCENT,
        "cooldown_until": None,
        "qualification": "qualified",
        "blocked_until": None,
        "blocker": None,
    }
    lanes["go"] = go_lane
    lanes["go-kimi"] = dict(go_lane, provider="go-kimi")
    for lane, record in lanes.items():
        print(lane, ledger.record_lane(lane, record)["state"])
    print("zcode window", flash_window(now))


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
