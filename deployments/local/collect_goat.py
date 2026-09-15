"""Command Code GOAT quota reading from its billing API; writes goat-observation.json.

The API key is read from the credential path named in the config (``goat_credential_path``);
the key itself is never logged and never appears in this file. The CLI status call is what
supplies the version header, so it is injected (``status_cmd``) the same way the network is.
"""

import json
import os
import subprocess
import urllib.request
import datetime
from pathlib import Path

API_BASE = "https://api.commandcode.ai/alpha/"
# The API reports monthly credits remaining, not a window; the plan's caps are 14 / 35 / 70
# (five-hour / weekly / monthly), and the GOAT console's monthly % is (70 - remaining) / 70.
# No reset time is exposed for the monthly window.
MONTHLY_CAP = 70
DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
CMD_PATH = "/opt/homebrew/bin/cmd"


def parse(credits, now=None, monthly_cap=MONTHLY_CAP):
    """The billing/credits body -> the observation the overlay expects."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    obs = {
        "provider": "command-code",
        "observed_at": now.isoformat(),
        "status": "unknown",
        "windows": [],
        "monthly_remaining": None,
    }
    w = credits["windowLimits"]
    for name, wid in (("fiveHour", "five_hour"), ("weekly", "weekly")):
        window = w[name]
        used, cap, reset = window["used"], window["cap"], window.get("resetAt")
        obs["windows"].append(
            {
                "id": wid,
                "used_percent": used / cap * 100 if cap else None,
                "resets_at": _iso_ms(reset),
            }
        )
    obs["monthly_remaining"] = credits["credits"].get("monthlyCredits")
    remaining = obs["monthly_remaining"]
    if (
        isinstance(remaining, (int, float))
        and not isinstance(remaining, bool)
        and 0 <= remaining <= monthly_cap
    ):
        obs["windows"].append(
            {
                "id": "monthly",
                "used_percent": (monthly_cap - remaining) / monthly_cap * 100,
                "resets_at": None,
            }
        )
    obs["status"] = "ok"
    return obs


def _iso_ms(reset):
    if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0:
        return datetime.datetime.fromtimestamp(reset / 1000, datetime.timezone.utc).isoformat()
    return None


def observe(fetch, config, now=None, status_cmd=None):
    """``fetch(url, headers, timeout)`` returns decoded JSON; ``status_cmd()`` returns the
    ``cmd status --json`` mapping (or raises)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        key = json.loads(Path(config["goat_credential_path"]).read_text())["apiKey"]
        status = (status_cmd or default_status_cmd(config))()
        if not status.get("authenticated"):
            raise ValueError("not authenticated")
        headers = {
            "Authorization": "Bearer " + key,
            "User-Agent": "cli",
            "x-command-code-version": status["version"],
            "x-cli-environment": "production",
        }
        whoami = fetch(API_BASE + "whoami?limits=1", headers=headers, timeout=20)
        if whoami.get("org"):
            raise ValueError("organization billing not configured")
        return parse(fetch(API_BASE + "billing/credits", headers=headers, timeout=20), now)
    except Exception as exc:
        return {
            "provider": "command-code",
            "observed_at": now.isoformat(),
            "status": "unknown",
            "windows": [],
            "monthly_remaining": None,
            "error": type(exc).__name__,
        }


def default_status_cmd(config):
    # `cmd` is a `#!/usr/bin/env node` script; launchd's PATH has no /opt/homebrew/bin,
    # so give it one.
    cmd = config.get("goat_cmd_path", CMD_PATH)
    env = dict(os.environ, PATH="/opt/homebrew/bin:" + os.environ.get("PATH", "/usr/bin:/bin"))

    def run():
        done = subprocess.run(
            [cmd, "status", "--json"], capture_output=True, text=True, timeout=15, env=env
        )
        if done.returncode != 0:
            raise RuntimeError("cmd_status_rc_" + str(done.returncode))
        return json.loads(done.stdout)

    return run


def write(obs, config):
    out = Path(config.get("output_dir", DEFAULT_DIR)) / "goat-observation.json"
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
    config.setdefault("goat_credential_path", str(Path.home() / ".commandcode/auth.json"))
    obs = observe(default_fetch, config)
    write(obs, config)
    print(obs["status"], obs.get("error"))


if __name__ == "__main__":
    main()
