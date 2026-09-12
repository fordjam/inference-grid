import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from inference_grid.service import supervise


def command(code):
    return [sys.executable, "-c", code]


def wait_for(path, condition, seconds=4):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists():
            state = json.loads(path.read_text())
            if condition(state):
                return state
        time.sleep(0.01)
    raise AssertionError("status condition not reached")


def test_success_and_atomic_stopped_status(tmp_path):
    state = supervise(tmp_path, max_cycles=1, command=command("pass"))
    assert state["phase"] == "stopped"
    assert state["last_success_at"] is not None
    assert json.loads((tmp_path / "service.json").read_text()) == state
    assert not (tmp_path / "service.partial").exists()
    assert (tmp_path / "service.json").stat().st_mode & 0o777 == 0o600


def test_timeout_and_secret_safe_failure(tmp_path):
    state = supervise(
        tmp_path,
        cycle_timeout=0.15,
        max_cycles=1,
        command=command("import time;print('SECRET_URL');time.sleep(10)"),
    )
    assert state["last_error"] == "cycle_timeout"
    assert "SECRET_URL" not in (tmp_path / "service.json").read_text()
    assert state["last_success_at"] is None


def test_nonzero_failure_and_missing_executable(tmp_path):
    state = supervise(tmp_path, max_cycles=1, command=command('raise RuntimeError("SECRET")'))
    assert state["last_error"] == "cycle_failed"
    state = supervise(tmp_path, max_cycles=1, command=["/does/not/exist"])
    assert state["last_error"] == "cycle_failed"


def test_long_interval_heartbeat_and_interruptible_stop(tmp_path):
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(supervise, tmp_path, interval=3600, stop=stop, command=command("pass"))
        try:
            first = wait_for(tmp_path / "service.json", lambda s: s["phase"] == "idle")
            assert first["next_cycle_at"] - first["heartbeat_at"] > 3500
            second = wait_for(
                tmp_path / "service.json",
                lambda s: s["phase"] == "idle" and s["heartbeat_at"] > first["heartbeat_at"] + 0.5,
            )
            assert second["cycles"] == 1
        finally:
            stop.set()
        assert future.result(timeout=2)["phase"] == "stopped"


def test_single_supervisor_lock_and_stop_active_cycle(tmp_path):
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            supervise, tmp_path, stop=stop, command=command("import time;time.sleep(10)")
        )
        try:
            wait_for(tmp_path / "service.json", lambda s: s["phase"] == "running_cycle")
            with pytest.raises(RuntimeError, match="already running"):
                supervise(tmp_path, max_cycles=1, command=command("pass"))
        finally:
            stop.set()
        assert future.result(timeout=2)["last_error"] == "stopped"


@pytest.mark.parametrize("interval", [0, -1, True, float("inf"), float("nan"), 3601])
def test_invalid_interval(interval, tmp_path):
    with pytest.raises(ValueError):
        supervise(tmp_path, interval=interval)
