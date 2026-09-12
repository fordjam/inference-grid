"""Opt-in real broker delivery check. Not a substitute for destructive chaos tests."""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from inference_grid.ledger import Ledger, digest


@pytest.mark.skipif(
    os.environ.get("GRID_RUN_BROKER_TEST") != "1",
    reason="real PostgreSQL/RabbitMQ services not requested",
)
def test_real_delivery_and_duplicate_suppression(tmp_path):
    from inference_grid.queue import publish, run_attempt

    ledger = Ledger(os.environ["GRID_DATABASE_URL"])
    ledger.initialize()
    name = uuid.uuid4().hex
    ledger.configure_account(name, 1, {"weekly": 10}, time.time() + 120, ["synthetic"])
    ledger.submit(
        name,
        "integration",
        dict(
            authorized=True,
            model="synthetic",
            family="example",
            argv=[
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "examples/synthetic_provider.py"),
            ],
            workspace=str(tmp_path / "work"),
            timeout=5,
            output_bytes=10000,
            inputs={},
            manifest_sha256=digest({}),
        ),
    )
    aid, gen = ledger.claim(name, name, {"weekly": 1})
    with (tmp_path / "worker.log").open("w") as log:
        worker = subprocess.Popen(
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
            ],
            stdout=log,
            stderr=log,
        )
        try:
            publish(ledger)
            run_attempt.apply_async(args=[aid, gen])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                row = next(r for r in ledger.status() if r["id"] == aid)
                if row["state"] == "completed":
                    break
                time.sleep(0.1)
            assert row["state"] == "completed", (tmp_path / "worker.log").read_text()
            assert len(list((tmp_path / "work").iterdir())) == 1
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
