"""Run from the repository root after pip install -e ."""

import sys
import tempfile
import time
from pathlib import Path

from inference_grid.ledger import Ledger, digest
from inference_grid.scheduler import tick
from inference_grid.worker import execute

root = Path(tempfile.mkdtemp(prefix="inference-grid-demo-"))
ledger = Ledger("sqlite:///" + str(root / "ledger.sqlite"))
ledger.initialize()
ledger.configure_account("demo", 1, {"weekly_units": 10}, time.time() + 300, ["synthetic"])
spec = dict(
    authorized=True,
    model="synthetic",
    family="example",
    argv=[
        sys.executable,
        str(Path(__file__).with_name("synthetic_provider.py").resolve()),
    ],
    workspace=str(root / "work"),
    timeout=5,
    output_bytes=10000,
    inputs={},
    manifest_sha256=digest({}),
    candidates=[dict(account="demo", estimate={"weekly_units": 1})],
)
ledger.submit("example-task", "example-project", spec)
result = tick(ledger)[0]
print(execute(ledger, result["attempt"], result["generation"]))
print(execute(ledger, result["attempt"], result["generation"]))
print("Local artifacts:", root)
