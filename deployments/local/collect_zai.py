"""Z.ai coding-plan quota from api.z.ai (the endpoint the ZCode app's Usage Stats panel reads).

Writes zai-observation.json for the dashboard and zai-quota.json (config: ``zai_quota_path``)
for the board ledger — the same shape as the operator attestation, plus ``source='api'``.
The API key is read from the credential path named in the config; the key itself is never
logged and never appears in this file.
"""

import json
import math
import os
import sys
import traceback
import urllib.request
import datetime
from pathlib import Path

API_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
# limits[].unit/number identify the window: unit 3 = hours (number 5 -> five_hour),
# unit 6 = weeks (number 1 -> weekly)
WINDOW = {(3, 5): "five_hour", (6, 1): "weekly"}
DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"


def parse_windows(limits):
    """One pass over the API's limits[]: the two known windows, percent used, ms reset time."""
    windows = []
    seen = set()
    for item in limits:
        wid = WINDOW.get((item.get("unit"), item.get("number")))
        used = item.get("percentage")
        reset = item.get("nextResetTime")
        if not wid or wid in seen:
            continue
        if (
            isinstance(used, bool)
            or not isinstance(used, (int, float))
            or not math.isfinite(used)
            or not 0 <= used <= 100
        ):
            continue
        seen.add(wid)
        windows.append(
            {
                "id": wid,
                "used_percent": used,
                "remaining_units": item.get("remaining"),
                "plan_units": item.get("usage"),
                "resets_at": _iso_ms(reset),
            }
        )
    return windows


def _iso_ms(reset):
    if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0:
        return datetime.datetime.fromtimestamp(reset / 1000, datetime.timezone.utc).isoformat()
    return None


def parse(body, now=None):
    """The full API body -> the observation the overlay and ledger expect."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    obs = {"provider": "zai", "observed_at": now.isoformat(), "status": "unknown", "windows": []}
    try:
        if body.get("code") != 200 or not body.get("success"):
            raise ValueError("api_" + str(body.get("code")))
        obs["windows"] = parse_windows(body["data"]["limits"])
        obs["level"] = body["data"].get("level")
        seen = {w["id"] for w in obs["windows"]}
        obs["status"] = "ok" if seen == {"five_hour", "weekly"} else "unknown"
    except Exception as exc:
        # 02-A4: used to leave status at its "unknown" default with only the exception
        # class name recorded, no log line at all.
        print(f"{obs['observed_at']} ERROR zai parse failed:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        obs["status"] = "error"
        obs["error"] = type(exc).__name__
    return obs


def observe(fetch, config, now=None):
    """One API reading. ``fetch(url, headers, timeout)`` returns the decoded JSON body."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        key = json.loads(Path(config["zai_credential_path"]).read_text())["api_key"]
        body = fetch(
            API_URL,
            headers={
                "Authorization": key,
                "Accept": "application/json",
                "User-Agent": "inference-grid-capacity/1",
            },
            timeout=20,
        )
        return parse(body, now)
    except Exception as exc:
        print(f"{now.isoformat()} ERROR zai observe failed:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return {
            "provider": "zai",
            "observed_at": now.isoformat(),
            "status": "error",
            "windows": [],
            "error": type(exc).__name__,
        }


def write(obs, config):
    """Observation for the overlay; when the reading is good, the ledger's quota file too."""
    out_dir = Path(config.get("output_dir", DEFAULT_DIR))
    out = out_dir / "zai-observation.json"
    _write_private(out, json.dumps(obs))
    if obs["status"] == "ok":
        w = {x["id"]: x for x in obs["windows"]}
        observed = datetime.datetime.fromisoformat(obs["observed_at"]).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        quota = {
            "observed_at": observed,
            "five_hour_used_percent": w["five_hour"]["used_percent"],
            "weekly_used_percent": w["weekly"]["used_percent"],
            "weekly_resets_at": w["weekly"]["resets_at"],
            "source": "api",
        }
        _write_private(
            Path(
                config.get("zai_quota_path", Path.home() / ".config/inference-grid/zai-quota.json")
            ),
            json.dumps(quota),
        )
    return obs


def _write_private(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def default_fetch(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def main(argv=None):
    import sys

    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    config.setdefault(
        "zai_credential_path", str(Path.home() / ".config/inference-grid/zai-coding-plan.json")
    )
    obs = observe(default_fetch, config)
    write(obs, config)
    print(obs["status"], obs.get("error"), [(x["id"], x["used_percent"]) for x in obs["windows"]])


if __name__ == "__main__":
    main()
