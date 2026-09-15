"""Go (OpenCode) collector chain: the node collector, the widget bridge, then the overlay build.

The node collector and widget bridge live next to this module in the deployed directory
(``collect-go.cjs``, ``widget_bridge.py``); paths and interpreters come from the config.
The widget bridge's failure stops the chain; the collector's and the build's are reported
and the loop continues.
"""

import json
import subprocess
import sys
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
NODE = "/opt/homebrew/bin/node"


def steps(config):
    here = Path(__file__).resolve().parent
    python = config.get("python", sys.executable)
    return [
        (
            [
                config.get("node", NODE),
                str(config.get("collect_go_script", here / "collect-go.cjs")),
            ],
            55,
            False,
        ),
        ([python, str(config.get("widget_bridge", here / "widget_bridge.py"))], 10, True),
        ([python, str(here / "overlay_build.py")], 20, False),
    ]


def refresh(config, run=None):
    """Node collector, widget bridge (its failure aborts the chain), then the overlay build."""
    run = run or _run
    for argv, timeout, checked in steps(config):
        try:
            run(argv, timeout, checked)
        except Exception as exc:
            print("refresh-go:", Path(argv[1]).name, type(exc).__name__)
            return False
    return True


def _run(argv, timeout, checked):
    subprocess.run(argv, timeout=timeout, check=checked)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    raise SystemExit(0 if refresh(config) else 1)


if __name__ == "__main__":
    main()
