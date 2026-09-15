"""Codex usage from chatgpt.com/backend-api/wham/usage (what ``codex /status`` reads), using the
CLI's own OAuth token from the auth.json inside each codex home. All homes share one account, so
the first home that answers wins. Writes codex-observation.json. No token is logged and no
refresh is attempted (401/403 -> auth_required). Credential paths come from the config
(``codex_homes``); the tokens themselves never appear here.
"""

import json
import math
import os
import urllib.error
import urllib.request
import datetime
from pathlib import Path

API_URL = "https://chatgpt.com/backend-api/wham/usage"
WINDOW = {18000: "five_hour", 604800: "weekly"}
DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"


def default_homes():
    return [Path.home() / ".codex"] + sorted(Path.home().glob(".codex-seat*"))


def _iso(ts):
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    return None


def parse_usage(body):
    """The usage body -> {windows, plan_type, limit_reached, source_reason}."""
    windows = []
    rl = body.get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key)
        if not isinstance(w, dict):
            continue
        wid = WINDOW.get(w.get("limit_window_seconds"))
        used = w.get("used_percent")
        if not wid:
            continue
        if (
            isinstance(used, bool)
            or not isinstance(used, (int, float))
            or not math.isfinite(used)
            or not 0 <= used <= 100
        ):
            continue
        windows.append({"id": wid, "used_percent": used, "resets_at": _iso(w.get("reset_at"))})
    reason = (body.get("rate_limit_reached_type") or {}).get("type")
    return {
        "windows": windows,
        "plan_type": body.get("plan_type"),
        "limit_reached": bool(rl.get("limit_reached")),
        "source_reason": reason,
    }


def observe(fetch, config, now=None, homes=None):
    """First home that answers wins. ``fetch(url, headers, timeout)`` returns decoded JSON."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    obs = {"provider": "codex", "observed_at": now.isoformat(), "status": "unknown", "windows": []}
    last_err = None
    for home in homes or config.get("codex_homes") or default_homes():
        home = Path(home)
        try:
            tokens = json.loads((home / "auth.json").read_text())["tokens"]
            headers = {
                "Authorization": "Bearer " + tokens["access_token"],
                "ChatGPT-Account-Id": tokens.get("account_id", ""),
                "User-Agent": "inference-grid-capacity/1",
                "Accept": "application/json",
            }
            usage = parse_usage(fetch(API_URL, headers=headers, timeout=20))
            obs["windows"] = usage["windows"]
            obs["plan_type"] = usage["plan_type"]
            obs["limit_reached"] = usage["limit_reached"]
            if usage["source_reason"]:
                obs["source_reason"] = usage["source_reason"]
            obs["source_home"] = home.name
            obs["status"] = "ok" if obs["windows"] else "unknown"
            break
        except urllib.error.HTTPError as exc:
            last_err = "HTTP_" + str(exc.code)
            obs["status"] = "auth_required" if exc.code in (401, 403) else "unknown"
        except Exception as exc:
            last_err = type(exc).__name__
    if obs["status"] != "ok" and last_err:
        obs["error"] = last_err
    return obs


def write(obs, config):
    out = Path(config.get("output_dir", DEFAULT_DIR)) / "codex-observation.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(obs))
    os.chmod(tmp, 0o600)
    tmp.replace(out)
    return obs


def default_fetch(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def main(argv=None):
    import sys

    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    obs = observe(default_fetch, config)
    write(obs, config)
    print(
        obs["status"],
        obs.get("error"),
        obs.get("source_reason"),
        [(w["id"], w["used_percent"]) for w in obs["windows"]],
    )


if __name__ == "__main__":
    main()
