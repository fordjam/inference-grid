"""The local capacity dashboard server, as a KeepAlive runtime.

Runs `inference_grid.capacity` on the configured port with the overlay the collectors
write, so it comes back after a reboot like the loop does. Config keys: `web_port`
(default 8040), `output_dir` (where overlay.json lives), `feed_port` (default 8020).
"""

import json
import sys
from pathlib import Path

from inference_grid import capacity

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError):
        config = {}
    overlay = Path(config.get("output_dir", DEFAULT_DIR)) / "overlay.json"
    # capacity.main parses sys.argv itself.
    sys.argv = [
        "inference-grid-capacity",
        "--port",
        str(config.get("web_port", 8040)),
        "--upstream",
        f"http://127.0.0.1:{config.get('feed_port', 8020)}/api/usage",
        "--overlay",
        str(overlay),
    ]
    capacity.main()


if __name__ == "__main__":
    main()
