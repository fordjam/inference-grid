"""The board tick loop, rewritten in Python from tick-boards.sh.

Ticks each board named in the config's ``boards`` list, in turn, until the config's
``deadline`` (epoch seconds). The ledger is refreshed (board-prepare) before every board:
a board with several serial reviews outlasts the 15-minute validity of the Go reading, and
stale accounts refuse the rest of the pass. After each pass the loop logs the ready count
across boards and sleeps the idle interval when nothing is ready, the busy one otherwise.
Same ledger as the monarch loop; Kimi is serialised by it.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
IDLE_SECONDS = 1800
BUSY_SECONDS = 300


def default_prepare(config):
    """One ledger refresh; output appended to the configured log."""
    python = config.get("python", sys.executable)
    here = Path(__file__).resolve().parent
    argv = [python, str(here / "board_prepare.py")]
    if config.get("_config_path"):
        argv.append(str(config["_config_path"]))
    with open(config.get("log_path", DEFAULT_DIR / "tick-boards.log"), "a") as log:
        subprocess.run(argv, stdout=log, stderr=log, timeout=300, check=True)


def default_tick(board, config):
    """One ``inference-grid board-tick`` for the board; returns its ready-task count.

    The tick's JSON reply names the board directory; a task file there counts as ready when
    its state says so. Each dispatched result is logged with its lane and outcome.
    """
    out = Path(config.get("tick_dir", "/tmp")) / f"tick-{board}.json"
    log_path = Path(config.get("log_path", DEFAULT_DIR / "tick-boards.log"))
    with log_path.open("a") as log:
        stamp = time.strftime("%FT%TZ", time.gmtime())
        log.write(f"=== {stamp} tick {board}\n")
        done = subprocess.run(
            [config.get("inference_grid_bin", "inference-grid"), "board-tick", "--json", str(out)],
            capture_output=True,
            text=True,
            timeout=3600,
        )
        log.write(done.stdout)
        log.write(done.stderr)
    results = json.loads(out.read_text())
    for r in results:
        if r.get("lane"):
            print(" ", r["task"], r["lane"], r["result"][:80])
    return count_ready(results.get("board_dir") if isinstance(results, dict) else None)


def count_ready(board_dir):
    if not board_dir:
        return 0
    import glob

    return sum(
        json.load(open(p))["state"] == "ready" for p in glob.glob(str(Path(board_dir) / "*.json"))
    )


def run(
    boards, deadline, prepare, tick, sleep, clock=time.time, idle=IDLE_SECONDS, busy=BUSY_SECONDS
):
    """Prepare before every board; the ready count decides the sleep. Returns the last count."""
    ready = 0
    while clock() < deadline:
        ready = 0
        for board in boards:
            prepare()
            ready += tick(board)
        print(time.strftime("%FT%TZ", time.gmtime()), "ready tasks left across boards:", ready)
        sleep(idle if ready == 0 else busy)
    return ready


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    config["_config_path"] = str(config_path)
    run(
        config["boards"],
        config["deadline"],
        lambda: default_prepare(config),
        lambda board: default_tick(board, config),
        time.sleep,
    )


if __name__ == "__main__":
    main()
