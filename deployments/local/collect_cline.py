"""ClinePass quota reading from api.cline.bot usage-limits; writes cline-observation.json.

The key is read from the file named in the config (``cline_credential_path``): the grid's
``{"api_key": ...}`` JSON, or an env file's ``CLINE_API_KEY=`` line; the key itself is never
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

API_URL = "https://api.cline.bot/api/v1/users/me/plan/usage-limits"
DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"


def read_key(credential_path):
    """The Cline key from the configured file: the grid's ``{"api_key": ...}`` JSON, or an env
    file's ``CLINE_API_KEY=`` line. None when absent; the key itself is never logged."""
    try:
        text = Path(credential_path).read_text()
    except OSError:
        return None
    try:
        document = json.loads(text)
        key = document.get("api_key") if isinstance(document, dict) else None
        return key or None
    except ValueError:
        pass
    for line in text.splitlines():
        if line.startswith("CLINE_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def parse(body, now=None):
    """The usage-limits body -> the observation the overlay expects.

    A reading only counts as ``ok`` when all three windows answered.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    obs = {
        "provider": "clinepass",
        "observed_at": now.isoformat(),
        "status": "unknown",
        "windows": [],
    }
    try:
        seen = set()
        for item in body["data"]["limits"]:
            name = item["type"]
            used = item["percentUsed"]
            reset = item.get("resetsAt")
            if name not in ("five_hour", "weekly", "monthly") or name in seen:
                continue
            if (
                isinstance(used, bool)
                or not isinstance(used, (int, float))
                or not math.isfinite(used)
                or not 0 <= used <= 100
            ):
                continue
            seen.add(name)
            obs["windows"].append(
                {
                    "id": name,
                    "used_percent": used,
                    "resets_at": datetime.datetime.fromisoformat(
                        reset.replace("Z", "+00:00")
                    ).isoformat()
                    if isinstance(reset, str)
                    else None,
                }
            )
        obs["status"] = "ok" if seen == {"five_hour", "weekly", "monthly"} else "unknown"
    except Exception as exc:
        # 02-A4: used to leave status at its "unknown" default with only the exception
        # class name recorded, no log line at all.
        print(f"{obs['observed_at']} ERROR cline parse failed:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        obs["status"] = "error"
        obs["error"] = type(exc).__name__
    return obs


def observe(fetch, config, now=None):
    """``fetch(url, headers, timeout)`` returns the decoded JSON body."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        key = read_key(config["cline_credential_path"])
        if not key:
            raise ValueError("credential_unavailable")
        body = fetch(
            API_URL,
            headers={
                "Authorization": "Bearer " + key,
                "User-Agent": "inference-grid-capacity/1",
            },
            timeout=20,
        )
        return parse(body, now)
    except Exception as exc:
        print(f"{now.isoformat()} ERROR cline observe failed:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return {
            "provider": "clinepass",
            "observed_at": now.isoformat(),
            "status": "error",
            "windows": [],
            "error": type(exc).__name__,
        }


def write(obs, config):
    out = Path(config.get("output_dir", DEFAULT_DIR)) / "cline-observation.json"
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
    config.setdefault(
        "cline_credential_path", str(Path.home() / "projects/yt-research-mcp/.env.local")
    )
    obs = observe(default_fetch, config)
    write(obs, config)
    print(obs["status"], obs.get("error"), [(w["id"], w["used_percent"]) for w in obs["windows"]])


if __name__ == "__main__":
    main()
