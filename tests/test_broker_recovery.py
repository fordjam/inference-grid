"""Opt-in destructive tests against a dedicated disposable broker/database only."""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from inference_grid.ledger import Ledger, digest, events
from inference_grid.queue import hold_abandoned, run_attempt

pytestmark = pytest.mark.skipif(
    os.environ.get("GRID_RUN_BROKER_TEST") != "1",
    reason="dedicated real services not requested",
)


def until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("condition not reached within bounded deadline")


@contextmanager
def worker(tmp_path, queue):
    env = dict(
        os.environ,
        GRID_QUEUE=queue,
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
    )
    with (tmp_path / (uuid.uuid4().hex + ".log")).open("w") as log:
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "inference_grid.queue:app",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--loglevel=WARNING",
                "-Q",
                queue,
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            yield p
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


def job(tmp_path, slow=False):
    ledger = Ledger(os.environ["GRID_DATABASE_URL"])
    ledger.initialize()
    name = uuid.uuid4().hex
    ledger.configure_account(name, 1, {"weekly": 10}, time.time() + 120, ["synthetic"])
    if slow:
        code = "import os,json,sys,time,pathlib;r=json.load(sys.stdin);(pathlib.Path(r['output_directory'])/'invoked').write_text(str(os.getpid()));time.sleep(60)"
        argv = [sys.executable, "-c", code]
    else:
        argv = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "examples/synthetic_provider.py"),
        ]
    ledger.submit(
        name,
        "broker-recovery",
        dict(
            authorized=True,
            model="synthetic",
            family="example",
            argv=argv,
            workspace=str(tmp_path / "work"),
            timeout=90,
            output_bytes=10000,
            inputs={},
            manifest_sha256=digest({}),
        ),
    )
    aid, gen = ledger.claim(name, name, {"weekly": 1})
    return ledger, aid, gen


def test_worker_killed_after_adapter_start_is_not_redispatched(tmp_path):
    ledger, aid, gen = job(tmp_path, slow=True)
    queue = "grid-test-" + uuid.uuid4().hex
    marker = tmp_path / "work" / aid / "artifacts" / "invoked"
    adapter_pid = None
    try:
        with worker(tmp_path, queue) as p:
            run_attempt.apply_async(args=[aid, gen], queue=queue)
            until(marker.exists)
            adapter_pid = int(marker.read_text())
            p.kill()
            p.wait(timeout=5)
        with worker(tmp_path, queue):
            # Explicit duplicate plus broker's unacked redelivery; both must be inert.
            run_attempt.apply_async(args=[aid, gen], queue=queue)
            time.sleep(3)
            with ledger.engine.connect() as con:
                starts = list(
                    con.execute(
                        select(events.c.id).where(
                            events.c.attempt == aid, events.c.kind == "dispatch_intent"
                        )
                    ).scalars()
                )
            assert len(starts) == 1
            assert next(r for r in ledger.status() if r["id"] == aid)["state"] == "dispatching"
            assert marker.read_text() == str(adapter_pid)
            hold_abandoned(ledger, time.time() + 1)
            assert next(r for r in ledger.status() if r["id"] == aid)["state"] == "held"
    finally:
        if adapter_pid:
            try:
                os.kill(adapter_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_published_job_survives_broker_restart(tmp_path):
    command = os.environ.get("GRID_TEST_BROKER_RESTART")
    if not command:
        pytest.skip("explicit disposable broker restart command not supplied")
    argv = json.loads(command)
    assert isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv)
    ledger, aid, gen = job(tmp_path)
    queue = "grid-test-" + uuid.uuid4().hex
    run_attempt.apply_async(args=[aid, gen], queue=queue)
    subprocess.run(argv, check=True, timeout=45)
    with worker(tmp_path, queue):
        until(
            lambda: next(r for r in ledger.status() if r["id"] == aid)["state"] == "completed",
            30,
        )
