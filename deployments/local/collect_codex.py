"""Codex usage from chatgpt.com/backend-api/wham/usage (what ``codex /status`` reads), using the
CLI's own OAuth token from the auth.json inside each codex home. All homes share one account's
quota, so the merged reading (``observe``) takes the first home that answers. Writes
codex-observation.json. No token is logged and no refresh is attempted (401/403 -> auth_required).
Credential paths come from the config (``codex_homes``); the tokens themselves never appear here.

The three-seat Codex lane trio (docs/LANES.md: codex-luna at ~/.codex, codex-terra at
~/.codex-seat1, codex-sol at ~/.codex-seat2) needs per-seat visibility on top of that merged
reading -- one seat's login can expire while the others still answer, and only a per-home read
can show which -- so ``observe_seats`` reads every configured home (never stopping at the first
success) and reports each one's own status. It shares one account's quota with ``observe``; it
does not sum or otherwise combine the three readings into three independent pools.
"""

import json
import math
import os
import sys
import traceback
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


def _read_home(fetch, home, now):
    """One home's own reading: {status, windows, plan_type, limit_reached, source_reason, error}."""
    home = Path(home)
    reading = {"home": home.name, "observed_at": now.isoformat(), "status": "unknown", "windows": []}
    try:
        tokens = json.loads((home / "auth.json").read_text())["tokens"]
        headers = {
            "Authorization": "Bearer " + tokens["access_token"],
            "ChatGPT-Account-Id": tokens.get("account_id", ""),
            "User-Agent": "inference-grid-capacity/1",
            "Accept": "application/json",
        }
        usage = parse_usage(fetch(API_URL, headers=headers, timeout=20))
        reading["windows"] = usage["windows"]
        reading["plan_type"] = usage["plan_type"]
        reading["limit_reached"] = usage["limit_reached"]
        if usage["source_reason"]:
            reading["source_reason"] = usage["source_reason"]
        reading["status"] = "ok" if reading["windows"] else "unknown"
        # A home that answered without raising, whether or not it had recognized windows --
        # distinct from "status": "unknown" also covers a non-401/403 HTTP error below, and
        # observe()'s first-that-answers merge must not treat that as a win.
        reading["_answered"] = True
    except urllib.error.HTTPError as exc:
        reading["error"] = "HTTP_" + str(exc.code)
        reading["status"] = "auth_required" if exc.code in (401, 403) else "unknown"
    except Exception as exc:
        # 02-A4: an unhandled exception used to leave status wherever an earlier home
        # iteration set it (often still the "unknown" default), with only the exception
        # class name recorded and no log line at all.
        print(f"{reading['observed_at']} ERROR codex home {home} failed:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        reading["error"] = type(exc).__name__
        reading["status"] = "error"
    return reading


def _configured_homes(config, homes):
    return homes or config.get("codex_homes") or default_homes()


def observe(fetch, config, now=None, homes=None):
    """First home that answers wins. ``fetch(url, headers, timeout)`` returns decoded JSON."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    obs = {"provider": "codex", "observed_at": now.isoformat(), "status": "unknown", "windows": []}
    last_err = None
    for home in _configured_homes(config, homes):
        reading = _read_home(fetch, home, now)
        if reading.pop("_answered", False):
            obs["windows"] = reading["windows"]
            obs["plan_type"] = reading["plan_type"]
            obs["limit_reached"] = reading["limit_reached"]
            if reading.get("source_reason"):
                obs["source_reason"] = reading["source_reason"]
            obs["source_home"] = reading["home"]
            obs["status"] = reading["status"]
            last_err = None
            break
        obs["status"] = reading["status"]
        last_err = reading.get("error")
    if obs["status"] != "ok" and last_err:
        obs["error"] = last_err
    return obs


def observe_seats(fetch, config, now=None, homes=None):
    """Every configured home's own reading, never stopping at the first success.

    One shared account's quota read from N logins: a seat with an expired login shows
    ``auth_required`` here while the others still read ``ok`` -- visibility ``observe``'s
    first-that-answers merge cannot give, since it stops as soon as any home succeeds.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    readings = [_read_home(fetch, home, now) for home in _configured_homes(config, homes)]
    for reading in readings:
        reading.pop("_answered", None)
    return readings


def write(obs, config, seats=None):
    """``seats`` (``observe_seats``'s output), when given, is folded in under ``obs["seats"]`` --
    additive only, so an older reader that knows only the merged shape is unaffected."""
    out = Path(config.get("output_dir", DEFAULT_DIR)) / "codex-observation.json"
    if seats is not None:
        obs = dict(obs, seats=seats)
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
    seats = observe_seats(default_fetch, config)
    write(obs, config, seats=seats)
    print(
        obs["status"],
        obs.get("error"),
        obs.get("source_reason"),
        [(w["id"], w["used_percent"]) for w in obs["windows"]],
        [(s["home"], s["status"]) for s in seats],
    )


if __name__ == "__main__":
    main()
