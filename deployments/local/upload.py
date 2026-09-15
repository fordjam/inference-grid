"""Upload only sanitized usage observations; never native provider credentials.

The config file (``client.json``) names the local feed, the remote URL and the upload
token path — the token itself is read from config at run time and never appears here.
Writes upload-status.json next to the config and exits nonzero when the upload failed.
"""

import json
import os
import sys
import urllib.request
import datetime
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/client.json"
ACCOUNT_KEYS = ["provider", "status", "observed_at", "windows", "monthly_remaining"]


def build_body(raw, now):
    """Keep only the sanitized keys of each account."""
    accounts = [{k: a.get(k) for k in ACCOUNT_KEYS} for a in raw.get("accounts", [])]
    return {"captured_at": now, "accounts": accounts}


def run(config, fetch, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc).isoformat()
    status = {"attempted_at": now, "status": "failed"}
    try:
        raw = fetch(config["local_feed"], headers={}, timeout=12)
        body = build_body(raw, now)
        response = fetch(
            config["remote_url"] + "/api/snapshot",
            headers={
                "Authorization": "Bearer " + config["upload_token"],
                "Content-Type": "application/json",
            },
            timeout=20,
            data=json.dumps(body, allow_nan=False).encode(),
            want_status=True,
        )
        if response != 200:
            raise RuntimeError("upload refused")
        status.update(status="ok", uploaded_at=now, providers=len(body["accounts"]))
    except Exception as exc:
        status.update(error=type(exc).__name__, http_status=getattr(exc, "code", None))
    return status


def default_fetch(url, headers, timeout, data=None, want_status=False):
    req = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status if want_status else json.load(response)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    status = run(config, default_fetch)
    out = config_path.with_name("upload-status.json")
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(status))
    os.chmod(tmp, 0o600)
    tmp.replace(out)
    print(json.dumps(status))
    return 0 if status["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
