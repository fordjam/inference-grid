"""Generic trusted adapter entrypoint: `inference-grid-lane <lane-id> --config lanes.json`.

The Grid worker starts this process with the attempt request on stdin and the attempt directory
as the working directory. The lane module does the native work and returns a receipt; this
module validates configuration, records the verdict beside the attempt, and prints exactly one
receipt on success. Any refusal exits non-zero with a short reason on stderr, which the ledger
records as a held attempt.
"""

import argparse
import json
import sys
from pathlib import Path

from .config import validate_lane_config

# Coordinator edit (brief-14 D2): "opencode_cli" added where the accepted kind set is
# consumed; lanes/config.py is provider-authored, so the operator's lanes.json cannot
# name the kind until its own set grows by one entry — patch provided in the D2 report.
KINDS = {
    "zcode_cli": "zcode",
    "claude_headless": "zai",
    "go_http": "go",
    "goat_cli": "goat",
    "cline_cli": "cline",
    "codex_cli": "codex",
    "opencode_cli": "opencode",
}


def load_lanes(path):
    return validate_lane_config(json.loads(Path(path).read_text()))["lanes"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one Grid attempt on a configured lane")
    parser.add_argument("lane")
    parser.add_argument("--config", required=True, help="private lanes.json; never committed")
    args = parser.parse_args(argv)
    lanes = load_lanes(args.config)
    if args.lane not in lanes:
        print("unknown lane: " + args.lane, file=sys.stderr)
        return 2
    lane = lanes[args.lane]
    module_name = KINDS.get(lane["kind"])
    if module_name is None:
        print("lane kind not packaged yet: " + lane["kind"], file=sys.stderr)
        return 2
    request = json.load(sys.stdin)
    if request.get("model") != lane["model"]:
        print("attempt model does not match the lane", file=sys.stderr)
        return 1
    module = __import__("inference_grid.lanes." + module_name, fromlist=["run"])
    attempt_dir = Path.cwd()
    # The lane's executable is configured, never discovered on PATH inside the worker.
    options = {"claude": lane["executable"]} if module_name == "zai" else {}
    receipt, verdict = module.run(request, lane, attempt_dir, **options)
    (attempt_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))
    if receipt is None:
        print("lane refused: " + str(verdict.get("refusal")), file=sys.stderr)
        return 1
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
