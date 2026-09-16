"""Loopback quota feed on 8020: the overlay's accounts and attempts, nothing else.

The dashboard server (`inference_grid.capacity`) merges an upstream feed with the overlay,
and the phone reads this feed directly, so the overlay is the answer: re-read from
``<output_dir>/overlay.json`` on every request, so a rebuild by ``overlay_build.py``
appears without a restart, and a missing or unreadable overlay answers an empty snapshot.
Kept alive by launchd like the other runtimes.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"


def snapshot(overlay_path):
    """The feed body: the overlay's accounts and attempts; empty when it cannot be read."""
    try:
        overlay = json.loads(Path(overlay_path).read_text())
    except (OSError, ValueError):
        return {"accounts": [], "attempts": []}
    if isinstance(overlay, list):
        return {"accounts": overlay, "attempts": []}
    if not isinstance(overlay, dict):
        return {"accounts": [], "attempts": []}
    accounts = overlay.get("accounts")
    attempts = overlay.get("attempts")
    return {
        "accounts": accounts if isinstance(accounts, list) else [],
        "attempts": attempts if isinstance(attempts, list) else [],
    }


class Handler(BaseHTTPRequestHandler):
    # The file this feed serves; main() points it at the operator's overlay. Read fresh
    # on every request, never cached at startup.
    overlay_path = DEFAULT_DIR / "overlay.json"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps(snapshot(self.overlay_path)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError):
        config = {}
    Handler.overlay_path = Path(config.get("output_dir", DEFAULT_DIR)) / "overlay.json"
    port = int(config.get("feed_port", 8020))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
