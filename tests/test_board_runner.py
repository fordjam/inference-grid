import json
import os
import sys
import time
import uuid

import pytest

from inference_grid.board import runner
from inference_grid.ledger import Ledger

FAKE_ADAPTER = """
import hashlib, json, sys
from pathlib import Path
request = json.load(sys.stdin)
inputs = Path(request["input_directory"]); out = Path(request["output_directory"])
names = json.loads((inputs / "expected.json").read_text())
artifacts = []
for name in names:
    data = (inputs / "mod.py").read_bytes()
    if "wrong" in (inputs / "brief.txt").read_text():
        data = b"VALUE = 2\\n"
    (out / name).write_bytes(data)
    artifacts.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
print(json.dumps({"status": "completed", "finish_reason": "stop", "actual_model": request["model"], "manifest_sha256": request["manifest_sha256"], "artifacts": artifacts}))
"""


def make_task(tid, brief_name, state="ready"):
    return dict(
        id=tid,
        category="pure_function",
        brief=brief_name,
        inputs=[brief_name, "mod.py"],
        tests=["test_mod2.py"],
        artifacts=["mod2.py"],
        lanes=["go"],
        author_family=None,
        budget={"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None},
        state=state,
        blocked_reason=None,
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = tmp_path / "project"
    board = project / "grid/board"
    board.mkdir(parents=True)
    (project / "mod.py").write_text("VALUE = 1\n")
    (project / "brief.txt").write_text("copy mod.py to mod2.py\n")
    (project / "brief-wrong.txt").write_text("wrong\n")
    (project / "test_mod2.py").write_text(
        "import unittest\nimport mod2\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(mod2.VALUE, 1)\n"
    )
    for tid, brief in (("copy-ok", "brief.txt"), ("copy-wrong", "brief-wrong.txt")):
        (board / f"{tid}.json").write_text(json.dumps(make_task(tid, brief)))
    adapter = tmp_path / "fake_adapter.py"
    adapter.write_text(FAKE_ADAPTER)
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    url = os.environ.get("GRID_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger = Ledger(url)
    ledger.initialize()
    account = "go-" + uuid.uuid4().hex[:8]
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    lanes = {
        "go": {
            "provider": "opencode",
            "family": "glm",
            "model": "glm-5.3-flash",
            "kind": "go_http",
            "credential_path": None,
            "executable": None,
            "plan_units": {},
            "window": None,
            "max_concurrency": 1,
            "wall_seconds": 60,
            "categories": ["pure_function"],
        }
    }
    lanes_path = tmp_path / "lanes.json"
    lanes_path.write_text(json.dumps({"lanes": lanes}))
    return dict(
        project=project,
        board=board,
        ledger=ledger,
        lanes=lanes,
        lanes_path=lanes_path,
        account=account,
        packets=tmp_path / "packets",
    )


def ready_record(now):
    return dict(
        provider="go",
        auth="ok",
        quota_observed_at=now - 5,
        quota_freshness_seconds=900,
        used_percent_max=1.0,
        admission_limit_percent=80,
        cooldown_until=None,
        qualification="qualified",
        blocked_until=None,
        blocker=None,
    )


def test_tick_without_lane_record_dispatches_nothing(world):
    results = runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        world["lanes"],
        world["lanes_path"],
        {"go": world["account"]},
        world["packets"],
    )
    assert {r["task"]: r["result"] for r in results} == {
        "copy-ok": "no_ready_lane",
        "copy-wrong": "no_ready_lane",
    }
    assert world["ledger"].status() == []


def test_tick_dispatches_tests_and_records(world):
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    results = runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        world["lanes"],
        world["lanes_path"],
        {"go": world["account"]},
        world["packets"],
        now=now,
    )
    by_task = {r["task"]: r for r in results}
    assert by_task["copy-ok"]["result"] == "passed" and by_task["copy-ok"]["lane"] == "go"
    assert by_task["copy-wrong"]["result"] == "failed_tests"
    ok = json.loads((world["board"] / "copy-ok.json").read_text())
    wrong = json.loads((world["board"] / "copy-wrong.json").read_text())
    assert ok["state"] == "review_pending" and ok["blocked_reason"] is None
    assert wrong["state"] == "blocked" and "failed tests" in wrong["blocked_reason"]
    card = {
        (e["category"], e["accepted"], e["attempts"])
        for e in world["ledger"].scorecard(account=world["account"])
    }
    assert card == {("pure_function", 1, 2)}
    # The project tree itself was never modified.
    assert sorted(p.name for p in world["project"].iterdir()) == [
        "brief-wrong.txt",
        "brief.txt",
        "grid",
        "mod.py",
        "test_mod2.py",
    ]
    # A second tick finds nothing ready.
    assert (
        runner.tick(
            world["board"],
            world["project"],
            world["ledger"],
            world["lanes"],
            world["lanes_path"],
            {"go": world["account"]},
            world["packets"],
            now=now + 1,
        )
        == []
    )
