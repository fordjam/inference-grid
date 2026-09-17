"""The board tick loop, rewritten in Python from tick-boards.sh.

Ticks each board named in the config's ``boards`` list until the config's ``deadline``
(epoch seconds). Each board loops on its own thread — prepare, tick, sleep — so a long
packet on one board never delays the reviews on another. The ledger is refreshed
(board-prepare) before every tick: a board with several serial reviews outlasts the
15-minute validity of the Go reading, and stale accounts refuse the rest of the tick. After
each tick a board logs its ready count and sleeps the idle interval when nothing is ready,
the busy one otherwise. Same ledger as the monarch loop; Kimi is serialised by it.

A ``calibration`` block in the config (``corpus_dir``, ``lanes``, ``every_days``) makes the
loop author a calibration run once the newest scored run is older than ``every_days``: the
ledger's own calibration outcomes are the clock, so a run in flight does not stop it. The
run is a board task the board-tick steps through; no lane runs the calibration itself.

A SIGTERM or SIGINT starts a drain: the board ticks in flight (the packet loop's rounds
included) finish, no board starts another tick and the loop exits; a
second signal within 30 s exits at once. While draining the loop writes ``draining`` to a
state file beside the log, so the operator can see why the restart is slow.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
IDLE_SECONDS = 1800
BUSY_SECONDS = 300
CALIBRATION_EVERY_DAYS = 7
DRAIN_GRACE_SECONDS = 30


def default_prepare(config):
    """One ledger refresh; output appended to the configured log."""
    python = config.get("python", sys.executable)
    here = Path(__file__).resolve().parent
    argv = [python, str(here / "board_prepare.py")]
    if config.get("_config_path"):
        argv.append(str(config["_config_path"]))
    with open(config.get("log_path", DEFAULT_DIR / "tick-boards.log"), "a") as log:
        subprocess.run(argv, stdout=log, stderr=log, timeout=300, check=True)


# 8 rounds of an hour plus gates: the longest packet the task validator admits.
TICK_TIMEOUT = 8 * 3600 + 1800


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
        argv = [config.get("inference_grid_bin", "inference-grid")]
        if config.get("database_url"):
            argv += ["--database", config["database_url"]]
        argv += ["board-tick", "--json", str(out)]
        # A packet may run max_rounds (up to 8) rounds of wall_seconds (up to 3600) each, and
        # a tick returns only when every attempt it started has settled. The old 3600 s cap
        # here killed every packet past its first hour and, being uncaught, took the runtime
        # with it — launchd respawned it and the orphaned attempts stayed "dispatching".
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=config.get("tick_timeout", TICK_TIMEOUT),
            )
        except subprocess.TimeoutExpired as exc:
            log.write(f"tick {board} exceeded {exc.timeout}s and was stopped\n")
            return count_ready(json.loads(out.read_text()).get("board_dir")) if out.exists() else 0
        log.write(done.stderr)
        # board-tick reads its configuration from the --json file and prints the pass's
        # results to stdout: one row per ready task, with the lane and outcome when dispatched.
        try:
            results = json.loads(done.stdout)
        except ValueError:
            log.write("tick output unreadable\n" + done.stdout[-500:] + "\n")
            results = []
        for r in results if isinstance(results, list) else []:
            if isinstance(r, dict) and r.get("lane"):
                why = ""
                if r.get("chosen_by"):
                    why = f" [{r['chosen_by']}"
                    if r.get("cost") is not None:
                        why += f" ${r['cost']:.4f}"
                    why += "]"
                line = f"  {r['task']} {r['lane']} {str(r.get('result', ''))[:80]}{why}\n"
                log.write(line)
                print(line, end="")
    try:
        board_dir = json.loads(out.read_text()).get("board_dir")
    except (OSError, ValueError):
        board_dir = None
    return count_ready(board_dir)


def count_ready(board_dir):
    if not board_dir:
        return 0
    import glob

    return sum(
        json.load(open(p))["state"] == "ready" for p in glob.glob(str(Path(board_dir) / "*.json"))
    )


def calibration_due(newest_at, every_days, now):
    """True when no calibration has been scored, or the newest one is older than every_days.

    `newest_at` is the newest recorded calibration outcome instant from the ledger (None
    when nothing has been scored); the decision never reads a file, so a run whose tasks
    exist but have not been scored does not stop the clock.
    """
    if newest_at is None:
        return True
    return now - newest_at >= every_days * 86400


def _calibration_board(config, calibration):
    """The board to author the run onto: the calibration block's, else the first named one."""
    if calibration.get("board_dir"):
        return calibration["board_dir"]
    if config.get("board_dir"):
        return config["board_dir"]
    for board in config.get("boards") or []:
        if isinstance(board, dict) and board.get("board_dir"):
            return board["board_dir"]
    return None


def default_calibrate(config, now=None):
    """Author a calibration_run task when the newest scored run is older than every_days.

    The config's ``calibration`` block names ``corpus_dir``, ``lanes`` and ``every_days``
    (and, when the tick loop does not name boards as objects, ``board_dir``). The ledger's
    own calibration outcomes are the only clock; the run itself is a board task the next
    board-tick steps through. Returns the authored (or existing) task id, or None.
    """
    calibration = config.get("calibration") or {}
    if not calibration.get("corpus_dir") or not calibration.get("lanes"):
        return None
    if not config.get("database_url"):
        return None
    board_dir = _calibration_board(config, calibration)
    if not board_dir:
        return None
    if config.get("package_src"):
        sys.path.insert(0, str(config["package_src"]))
    from inference_grid.board.calibration import calibration_task, newest_calibration_at
    from inference_grid.ledger import Ledger

    now = time.time() if now is None else now
    ledger = Ledger(config["database_url"])
    every_days = calibration.get("every_days", CALIBRATION_EVERY_DAYS)
    if not calibration_due(newest_calibration_at(ledger), every_days, now):
        return None
    run_id = "auto-" + time.strftime("%Y%m%d", time.gmtime(now))
    created = calibration_task(
        board_dir, calibration["corpus_dir"], list(calibration["lanes"]), run_id
    )
    return created["id"]


class Drain:
    """The first SIGTERM/SIGINT starts the drain; a second within the grace exits at once.

    The state file beside the log carries ``draining`` while the loop finishes the board
    tick in flight, so a slow restart is explained; ``clear`` drops it when the drain
    completes and when a restarting process finds a marker a forced exit left behind.
    """

    def __init__(self, state_path, clock=time.time, exit=os._exit):
        self.state_path = Path(state_path)
        self.draining = False
        self.since = None
        self._clock = clock
        self._exit = exit

    def on_signal(self, signum, frame):
        if not self.draining:
            self.draining = True
            self.since = self._clock()
            try:
                self.state_path.write_text("draining\n")
            except OSError:
                pass
        elif self._clock() - self.since <= DRAIN_GRACE_SECONDS:
            self._exit(128 + signum)

    def clear(self):
        try:
            self.state_path.unlink()
        except OSError:
            pass


def install_drain(drain):
    """SIGTERM and SIGINT both start the drain; the previous handlers are the caller's."""
    signal.signal(signal.SIGTERM, drain.on_signal)
    signal.signal(signal.SIGINT, drain.on_signal)
    return drain


def run(
    boards,
    deadline,
    prepare,
    tick,
    sleep,
    clock=time.time,
    idle=IDLE_SECONDS,
    busy=BUSY_SECONDS,
    calibrate=None,
    drain=None,
):
    """Each board loops on its own: prepare, tick, sleep, until the deadline. Returns the
    sum of the boards' last ready counts.

    Brief J6 put each board's tick on its own thread but joined the pass before sleeping,
    so a pass lasted as long as its longest board: on 2026-09-16 a vix-rs packet held every
    other board's reviews for hours. Now a board's thread runs its own loop — its ready
    count decides its own sleep — and the boards never wait on one another. `prepare` is
    serialised (one ledger refresh at a time) and still runs before every tick.

    ``calibrate`` (optional) runs on the loop's own thread before the boards start and again
    every idle interval while they run: the weekly calibration author is idempotent per run.
    ``drain`` (optional) ends the loop after the ticks in flight: no board starts another
    tick or sleeps once a drain is running. A board whose tick raises stops the others the
    same way, and the error is re-raised here once every tick in flight has finished.
    """
    stop = threading.Event()
    prepare_lock = threading.Lock()
    counts = {}
    failures = []

    def draining():
        return stop.is_set() or bool(drain and drain.draining)

    def loop(board):
        try:
            while clock() < deadline and not draining():
                with prepare_lock:
                    prepare()
                counts[board] = tick(board)
                print(
                    time.strftime("%FT%TZ", time.gmtime()),
                    f"ready tasks left on {board}:",
                    counts[board],
                )
                if draining():
                    break
                sleep(idle if counts[board] == 0 else busy)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the loop's own thread
            failures.append(exc)
            stop.set()

    if calibrate:
        calibrate()
    next_calibrate = clock() + idle
    threads = []
    for board in boards:
        if draining():
            break
        thread = threading.Thread(target=loop, args=(board,), daemon=True, name=board)
        threads.append(thread)
        thread.start()
    while any(t.is_alive() for t in threads):
        for thread in threads:
            thread.join(1.0)
        if calibrate and not draining() and clock() >= next_calibrate:
            calibrate()
            next_calibrate = clock() + idle
    if failures:
        raise failures[0]
    return sum(counts.values())


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    config["_config_path"] = str(config_path)
    # Boards may be names or {"name": ...} rows; a KeepAlive agent gives no deadline, so the
    # loop runs a day and lets launchd start it again.
    boards = [b["name"] if isinstance(b, dict) else b for b in config["boards"]]
    deadline = config.get("deadline") or time.time() + 86400
    log_path = Path(config.get("log_path", DEFAULT_DIR / "tick-boards.log"))
    drain = install_drain(Drain(log_path.with_suffix(".draining")))
    drain.clear()
    try:
        run(
            boards,
            deadline,
            lambda: default_prepare(config),
            lambda board: default_tick(board, config),
            time.sleep,
            calibrate=lambda: default_calibrate(config),
            drain=drain,
        )
    finally:
        drain.clear()


if __name__ == "__main__":
    main()
