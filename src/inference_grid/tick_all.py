"""`inference-grid tick-all`: one scheduler pass over every board config.

Reads each ``*.json`` in ``boards_dir`` (a board-tick config, optionally with a
``priority``), orders them by that priority, runs the optional prepare command once
(capacity refresh), then ticks every board in order on one shared ledger — so busy
counts, campaign windows (the lane records carry them), Cline-after-reset (the seeded
placeholder is stale until configured) and GOAT's category restrictions are all just
selection, already enforced by the tick. Emits one JSON line per board to ``log_path``.
"""

import json
import subprocess
import time
from pathlib import Path


def ordered_configs(boards_dir):
    """The board configs in boards_dir, ordered by declared priority then name."""
    configs = []
    for path in sorted(Path(boards_dir).glob("*.json")):
        config = json.loads(path.read_text())
        configs.append((config.get("priority", 100), path.name, config))
    return [config for _, _, config in sorted(configs, key=lambda entry: (entry[0], entry[1]))]


def tick_all(ledger, boards_dir, log_path=None, prepare=None, dry_run=False):
    """Tick every board config in order; one JSON line per board, to the log and stdout."""
    from .cli import board_tick

    if prepare and not dry_run:
        # The capacity refresh has side effects; a dry run plans without it.
        if callable(prepare):
            prepare()
        else:
            subprocess.run(prepare, capture_output=True, timeout=600)
    lines = []
    for config in ordered_configs(boards_dir):
        started = time.time()
        # `priority` is tick-all's ordering knob, not a board-tick key.
        results = board_tick(
            ledger, dry_run=dry_run, **{k: v for k, v in config.items() if k != "priority"}
        )
        line = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "board": config.get("board_dir"),
            "seconds": round(time.time() - started, 1),
            "results": results,
        }
        lines.append(line)
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with Path(log_path).open("a") as handle:
                handle.write(json.dumps(line) + "\n")
    return lines
