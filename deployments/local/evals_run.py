"""The nightly eval loop: `inference-grid evals` every hour, kept alive by launchd.

launchd parks ``StartInterval`` spawns for a GUI-session agent while the display is off
(the note in ``install.py``), so the evals agent is kept alive and loops itself: one
``evals`` call per ``EVAL_SECONDS`` against the operator's corpus, then a sleep. ``evals``
authors only what is stale, so a pass with nothing due is a cheap no-op — the loop logs the
authored count every time, so the log still says the agent is alive.

The config is the capacity layer's own (``~/.local/share/inference-grid-capacity/
config.json``): ``evals_corpus`` is the corpus path, ``lanes_path`` the lanes the freshness
is measured against, ``board_dir`` the board the runs are authored onto and
``database_url`` the ledger. A missing corpus is a skip, logged, never a crash.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
EVAL_SECONDS = 3600
SPAWN_TIMEOUT = 1800
RUN_SECONDS = 86400


def eval_payload(config):
    """The `inference-grid evals --json` document the config names, or None when incomplete.

    Every path comes from the config; nothing is discovered. A config without a corpus or
    without the three board keys asks for nothing rather than guessing.
    """
    required = ("evals_corpus", "lanes_path", "board_dir", "evals_project_root")
    if any(not config.get(key) for key in required):
        return None
    return {
        "corpus_dir": str(config["evals_corpus"]),
        "lanes": str(config["lanes_path"]),
        "board_dir": str(config["board_dir"]),
        "project_root": str(config["evals_project_root"]),
        "every_days": config.get("evals_every_days"),
    }


def default_evals(config):
    """One `inference-grid evals` call; returns its stdout decoded, or a skip note."""
    payload = eval_payload(config)
    log_path = Path(config.get("log_path", DEFAULT_DIR / "evals.log"))
    with log_path.open("a") as log:
        if payload is None:
            log.write("[evals] config incomplete; nothing to author\n")
            return None
        arg_dir = Path(config.get("evals_dir", config.get("tick_dir", "/tmp")))
        arg_dir.mkdir(parents=True, exist_ok=True)
        arg_path = arg_dir / "evals.json"
        arg_path.write_text(json.dumps(payload))
        argv = [config.get("inference_grid_bin", "inference-grid")]
        if config.get("database_url"):
            argv += ["--database", config["database_url"]]
        argv += ["evals", "--json", str(arg_path)]
        stamp = time.strftime("%FT%TZ", time.gmtime())
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=config.get("evals_timeout", SPAWN_TIMEOUT),
            )
        except subprocess.TimeoutExpired:
            log.write(f"[evals] {stamp} exceeded its timeout and was stopped\n")
            return None
        if done.returncode != 0:
            log.write(f"[evals] {stamp} exit {done.returncode}: {done.stderr.strip()[:400]}\n")
            return done.stderr
        try:
            authored = len(json.loads(done.stdout).get("authored") or [])
        except ValueError:
            log.write(f"[evals] {stamp} output unreadable\n")
            return done.stdout
        log.write(f"[evals] {stamp} authored {authored}\n")
        return done.stdout


class Stop:
    """SIGTERM/SIGINT end the loop after the call in flight; a second ends it at once."""

    def __init__(self, exit=os._exit):
        self.stopping = False
        self._exit = exit

    def on_signal(self, signum, frame):
        if self.stopping:
            self._exit(128 + signum)
        self.stopping = True


def install_stop(stop):
    signal.signal(signal.SIGTERM, stop.on_signal)
    signal.signal(signal.SIGINT, stop.on_signal)
    return stop


def run(config, spawn, sleep, clock=time.time, deadline=None, seconds=EVAL_SECONDS, stop=None):
    """Run `spawn` once per interval until the deadline or a stop; returns the call count.

    The call is made before the sleep, so the first eval happens at startup and a restart
    (launchd's KeepAlive) does not delay it by an hour.
    """
    deadline = (clock() + RUN_SECONDS) if deadline is None else deadline
    calls = 0
    while clock() < deadline and not (stop and stop.stopping):
        spawn(config)
        calls += 1
        if clock() >= deadline or (stop and stop.stopping):
            break
        sleep(seconds)
    return calls


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    config["_config_path"] = str(config_path)
    stop = install_stop(Stop())
    run(
        config,
        default_evals,
        time.sleep,
        deadline=time.time() + RUN_SECONDS,
        stop=stop,
    )


if __name__ == "__main__":
    main()
