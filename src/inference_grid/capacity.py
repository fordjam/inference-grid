"""Loopback-only PWA for a sanitized, read-only quota feed."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

PROVIDERS = {"codex", "claude", "clinepass", "command-code", "opencode", "zai"}


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0


def project(raw, overlays=()):
    # An overlay is a list of account observations or {"accounts": [...], "attempts": [...]};
    # overlay attempts replace the upstream activity list when present.
    overlay_accounts = overlays.get("accounts", []) if isinstance(overlays, dict) else overlays
    overlay_attempts = overlays.get("attempts") if isinstance(overlays, dict) else None
    accounts = {}
    for a in [*raw.get("accounts", []), *overlay_accounts]:
        if not isinstance(a, dict) or a.get("provider") not in PROVIDERS:
            continue
        key = a["provider"]
        if key in accounts and timestamp(a.get("observed_at")) <= timestamp(
            accounts[key].get("observed_at")
        ):
            continue
        windows = []
        for w in a.get("windows", []):
            if not isinstance(w, dict):
                continue
            value = w.get("used_percent")
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                value = None
            windows.append(
                dict(
                    id=str(w.get("id", "unknown"))[:80],
                    used_percent=value,
                    resets_at=w.get("resets_at"),
                    reset_label=w.get("reset_label"),
                )
            )
        remaining = a.get("monthly_remaining")
        if type(remaining) not in (int, float) or not math.isfinite(remaining):
            remaining = None
        accounts[key] = dict(
            provider=key,
            observed_at=a.get("observed_at"),
            status=a.get("status", "unknown"),
            windows=windows,
            monthly_remaining=remaining,
        )
    return dict(
        accounts=list(accounts.values()),
        attempts=[
            {k: a.get(k) for k in ("task", "provider", "model", "status", "at")}
            for a in (
                overlay_attempts if isinstance(overlay_attempts, list) else raw.get("attempts", [])
            )[:30]
            if isinstance(a, dict)
        ],
        served_at=datetime.now(timezone.utc).isoformat(),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    parser.add_argument("--upstream", default="http://127.0.0.1:8020/api/usage")
    parser.add_argument("--overlay", type=Path)
    args = parser.parse_args()
    parsed = urlsplit(args.upstream)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username
        or parsed.password
    ):
        parser.error("upstream must be a credential-free loopback HTTP feed")
    root = Path(__file__).with_name("capacity_web")
    allowed = {"/": "index.html", **{("/" + p.name): p.name for p in root.iterdir() if p.is_file()}}
    types = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css",
        ".js": "application/javascript",
        ".png": "image/png",
        ".webmanifest": "application/manifest+json",
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header(
                "Cache-Control", "no-store" if mime.startswith("application/json") else "no-cache"
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") not in {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}:
                return self.reply(403, b"Forbidden", "text/plain")
            path = urlsplit(self.path).path
            if path == "/api/usage":
                try:
                    with urlopen(args.upstream, timeout=8) as response:
                        raw = json.load(response)
                    overlays = (
                        json.loads(args.overlay.read_text())
                        if args.overlay and args.overlay.exists()
                        else []
                    )
                    body = json.dumps(project(raw, overlays), allow_nan=False).encode()
                    return self.reply(200, body, "application/json")
                except Exception:
                    return self.reply(
                        503, b'{"error":"Local quota feed unavailable"}', "application/json"
                    )
            if path not in allowed:
                return self.reply(404, b"Not found", "text/plain")
            p = root / allowed[path]
            return self.reply(200, p.read_bytes(), types.get(p.suffix, "application/octet-stream"))

    print(f"Inference capacity PWA: http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
