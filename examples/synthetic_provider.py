"""Offline adapter example; does not call an inference service."""

import hashlib
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
body = b"Synthetic evaluation artifact. No inference was consumed.\n"
(Path(request["output_directory"]) / "result.txt").write_bytes(body)
print(
    json.dumps(
        dict(
            status="completed",
            finish_reason="stop",
            actual_model=request["model"],
            manifest_sha256=request["manifest_sha256"],
            artifacts=[dict(path="result.txt", sha256=hashlib.sha256(body).hexdigest())],
        )
    )
)
