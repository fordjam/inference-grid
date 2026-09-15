"""Minimal loopback upstream for the capacity dashboard: every reading lives in the overlay.

The dashboard server (`inference_grid.capacity`) merges an upstream feed with the overlay;
with local collectors there is no upstream, so this answers an empty snapshot on 8020 and
the overlay carries everything. Kept alive by launchd like the other runtimes.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"accounts": [], "attempts": []}).encode()
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
    port = int(config.get("feed_port", 8020))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
