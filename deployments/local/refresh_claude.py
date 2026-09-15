"""Claude usage poller: api.anthropic.com/api/oauth/usage against the CLI's keychain token,
then this cycle's collectors and the overlay build (collectors first so the overlay carries
this cycle's readings).

An expired keychain token draws a 429 from the usage endpoint, not a 401, so ``expiresAt`` is
checked locally and reported as what it really is (``auth_required``). On any failure the last
good windows are carried forward with ``stale: true`` and their own timestamp, so the dashboard
shows a stale number rather than nothing. The OAuth token is read through the keychain service
named in the config and is never logged; no credential value appears in this file.
"""

import json
import math
import os
import subprocess
import urllib.error
import urllib.request
import datetime
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
KEYCHAIN_SERVICE = "Claude Code-credentials"


def retry_seconds(value, now):
    """A Retry-After header (seconds or HTTP date) -> seconds to wait, or None."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def observe(fetch, credentials_text, prior, state, now=None):
    """One poll attempt.

    ``fetch(url, headers, timeout)`` returns the decoded JSON body; ``credentials_text`` is the
    keychain payload (the CLI's stored JSON); ``prior`` is the previous overlay entry for claude;
    ``state`` is the persisted poll state (carries the backoff delay). Returns the observation
    and the delay before the next attempt is due.
    """
    now = now or time.time()
    a = {
        "provider": "claude",
        "observed_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
        "status": "unknown",
        "windows": [],
    }
    delay = 300
    http_status = None
    retry_after = None
    try:
        cred = json.loads(credentials_text)["claudeAiOauth"]
        if isinstance(cred.get("expiresAt"), (int, float)) and cred["expiresAt"] / 1000 < now:
            raise PermissionError("token_expired")
        body = fetch(
            USAGE_URL,
            headers={
                "Authorization": "Bearer " + cred["accessToken"],
                "anthropic-beta": "oauth-2025-04-20",
            },
            timeout=15,
        )
        a["windows"] = [
            {
                "id": name,
                "used_percent": body[k]["utilization"],
                "resets_at": body[k].get("resets_at"),
            }
            for k, name in (
                ("five_hour", "five_hour"),
                ("seven_day", "weekly"),
                ("seven_day_opus", "weekly_opus"),
            )
            if isinstance(body.get(k), dict)
        ]
        a["status"] = "ok" if a["windows"] else "unknown"
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        retry_after = retry_seconds(exc.headers.get("Retry-After"), now)
        a["status"] = (
            "rate_limited"
            if exc.code == 429
            else "auth_required"
            if exc.code in (401, 403)
            else "unknown"
        )
        delay = min(3600, max(900, state.get("delay", 450) * 2)) if exc.code == 429 else 900
        if retry_after is not None:
            delay = max(delay, retry_after)
    except PermissionError:
        a["status"] = "auth_required"
        delay = 900
    except Exception:
        delay = 900
    if not a["windows"] and prior.get("windows"):
        a["windows"] = prior["windows"]
        a["observed_at"] = prior.get("observed_at") or a["observed_at"]
        a["stale"] = True
    info = {"http_status": http_status, "retry_after_seconds": retry_after}
    return a, delay, info


def read_credentials(config):
    """The keychain payload for the service named in the config (never logged)."""
    service = config.get("claude_keychain_service", KEYCHAIN_SERVICE)
    done = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return done.stdout


def poll(config, state_path=None):
    """The full cadence-aware poll: skip when not due, then persist state and overlay."""
    state_path = Path(
        state_path or config.get("claude_state_path", DEFAULT_DIR / "claude-poll-state.json")
    )
    try:
        previous = json.loads(state_path.read_text())
    except (OSError, ValueError):
        previous = {}
    due = time.time() >= previous.get("next_try", 0)
    overlay_path = Path(
        config.get("claude_overlay_path", "/private/tmp/grid-pwa-build/overlay.json")
    )
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prior = next(
            x for x in json.loads(overlay_path.read_text()) if x.get("provider") == "claude"
        )
    except (OSError, ValueError, StopIteration):
        prior = {}
    if not due:
        return None
    a, delay, info = observe(_default_fetch, read_credentials(config), prior, previous)
    state_path.write_text(
        json.dumps(
            {
                "status": a["status"],
                "next_try": time.time() + delay,
                "delay": delay,
                "http_status": info["http_status"],
                "retry_after_seconds": info["retry_after_seconds"],
            }
        )
    )
    os.chmod(state_path, 0o600)
    tmp = overlay_path.with_suffix(".tmp")
    tmp.write_text(json.dumps([a]))
    os.chmod(tmp, 0o600)
    tmp.replace(overlay_path)
    return a


def _default_fetch(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def run_collectors(config, run=None):
    """The collector chain, then the overlay build; a failing collector never stops the rest."""
    import sys

    run = run or _run
    python = config.get("python", sys.executable)
    here = Path(__file__).resolve().parent
    for name in ("collect_goat.py", "collect_cline.py", "collect_zai.py", "collect_codex.py"):
        run([python, str(here / name)], timeout=40)
    run([python, str(here / "overlay_build.py")], timeout=20)


def _run(argv, timeout):
    subprocess.run(argv, timeout=timeout)


def main(argv=None):
    import sys

    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    poll(config)
    run_collectors(config)


if __name__ == "__main__":
    main()
