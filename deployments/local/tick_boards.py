"""The board tick loop, rewritten in Python from tick-boards.sh.

Ticks each board named in the config's ``boards`` list, every pass, until the config's
``deadline`` (epoch seconds). Each board's tick runs in its own thread and the pass joins
them before it sleeps, so a long packet on one board never delays the reviews on another;
the ready count is summed after the joins. The ledger is refreshed (board-prepare) before
every board: a board with several serial reviews outlasts the 15-minute validity of the Go
reading, and stale accounts refuse the rest of the pass. After each pass the loop logs the
ready count across boards and sleeps the idle interval when nothing is ready, the busy one
otherwise. Same ledger as the monarch loop; Kimi is serialised by it.

A ``calibration`` block in the config (``corpus_dir``, ``lanes``, ``every_days``) makes the
loop author a calibration run once the newest scored run is older than ``every_days``: the
ledger's own calibration outcomes are the clock, so a run in flight does not stop it. The
run is a board task the board-tick steps through; no lane runs the calibration itself.

A SIGTERM or SIGINT starts a drain: the board ticks in flight (the packet loop's rounds
included) finish, the remaining boards of the pass are not started and the loop exits; a
second signal within 30 s exits at once. While draining the loop writes ``draining`` to a
state file beside the log, so the operator can see why the restart is slow.

The loop also writes a heartbeat file (``heartbeat_path`` in the config, default
``~/.local/share/inference-grid/heartbeat/tick-boards.json``) from a daemon thread every
``HEARTBEAT_INTERVAL`` seconds for as long as the process lives — fresh through multi-hour
board ticks, stale only when the loop is gone — and the per-pass ready count is printed
unbuffered so the launchd log shows it the moment it happens.
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

# The loop's heartbeat: written by a daemon thread for as long as the process lives, so
# the file stays fresh even while the main thread sits inside a multi-hour board-tick
# subprocess. A dead-man watch on this file detects a silently dead loop, not a busy one.
DEFAULT_HEARTBEAT = Path.home() / ".local/share/inference-grid/heartbeat/tick-boards.json"
HEARTBEAT_INTERVAL = 300


def write_heartbeat(path, boards=None, now=None):
    """One atomic heartbeat write: tmp file, then rename. Never raises past OSError."""
    path = Path(path)
    payload = {
        "written_at": time.strftime("%FT%TZ", time.gmtime(now if now is not None else time.time())),
        "pid": os.getpid(),
    }
    if boards is not None:
        payload["boards"] = list(boards)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except OSError:
        pass  # observability must never kill the loop


def heartbeat_path(config):
    """The heartbeat file: the config's ``heartbeat_path`` or the default beside the state."""
    raw = config.get("heartbeat_path", DEFAULT_HEARTBEAT)
    return Path(raw).expanduser() if isinstance(raw, str) else Path(raw)


def start_heartbeat(config, interval=HEARTBEAT_INTERVAL):
    """Beat for as long as this process lives; returns the stop event for a clean exit."""
    path = heartbeat_path(config)
    boards = [b["name"] if isinstance(b, dict) else b for b in config.get("boards") or []]
    stop = threading.Event()

    def beat():
        while not stop.wait(interval):
            write_heartbeat(path, boards)

    threading.Thread(target=beat, daemon=True, name="tick-boards-heartbeat").start()
    return stop


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
                print(line, end="", flush=True)
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
    """Each board's thread prepares, then ticks; the ready count decides the sleep.

    Each board's tick runs in its own thread and the pass joins them at its end (brief J6),
    so a board whose packet runs for an hour no longer delays the reviews on the next one:
    the loop moves on as soon as a board's tick has been started, not when it has settled.
    The prepare runs inside that same thread: a failing board-prepare blocks only its own
    board for the pass, with the reason printed, and the loop continues (the next pass
    retries it). The ready count is summed after the joins, and the pass never starts a
    board once a drain is running — the ticks already in flight still finish.

    ``calibrate`` (optional) runs once per pass before the boards: the weekly calibration
    author is idempotent per run, so a pass that finds a run in flight authors nothing.
    ``drain`` (optional) ends the loop after the board ticks in flight: the pass's remaining
    boards are skipped and no further pass or sleep is started.
    """
    ready = 0
    while clock() < deadline and not (drain and drain.draining):
        if calibrate:
            calibrate()
        counts = []
        failures = []
        blocked = []

        def one(board):
            # board-prepare runs inside the board's own thread: a failing refresh (stale
            # accounts, a refused ledger) blocks that board for this pass, with the reason
            # printed unbuffered, and the pass's other boards still tick. The next pass
            # retries the prepare. Before, prepare() ran on the loop's own thread with
            # check=True, so one bad refresh killed the whole runtime.
            try:
                prepare()
            except Exception as exc:  # noqa: BLE001 - a blocked board is not a dead loop
                blocked.append(board)
                print(
                    f"{board}: board-prepare failed; board blocked this pass: {exc!r}",
                    flush=True,
                )
                return
            try:
                counts.append(tick(board))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the loop's own thread
                failures.append(exc)

        threads = []
        for board in boards:
            if drain and drain.draining:
                break
            thread = threading.Thread(target=one, args=(board,), daemon=True)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        if failures:
            raise failures[0]
        ready = sum(counts)
        if drain and drain.draining:
            break
        print(
            time.strftime("%FT%TZ", time.gmtime()),
            "ready tasks left across boards:",
            ready,
            flush=True,
        )
        sleep(idle if ready == 0 else busy)
    return ready


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
    # One synchronous beat proves the loop started; the thread keeps it fresh through the
    # long board-tick subprocesses, so a stale file means the process itself is gone.
    write_heartbeat(heartbeat_path(config), boards)
    stop_heartbeat = start_heartbeat(config)
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
        stop_heartbeat.set()
        drain.clear()


if __name__ == "__main__":
    main()
