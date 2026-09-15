"""ClinePass quota reading from api.cline.bot usage-limits; writes cline-observation.json.

The key is read from the env file named in the config (``cline_credential_path``), by looking
for its ``CLINE_API_KEY=`` line; the key itself is never logged and never appears in this file.
"""

import json
import math
import os
import urllib.request
import datetime
from pathlib import Path

API_URL = "https://api.cline.bot/api/v1/users/me/plan/usage-limits"
DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"


def read_key(env_path):
    """The CLINE_API_KEY line of the configured env file, or None when absent."""
    try:
        lines = Path(env_path).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("CLINE_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
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
        return {
            "provider": "clinepass",
            "observed_at": now.isoformat(),
            "status": "unknown",
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
