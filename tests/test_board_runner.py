import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

from inference_grid.board import runner
from inference_grid.ledger import Ledger, Refused

FAKE_ADAPTER = """
import hashlib, json, sys
from pathlib import Path
request = json.load(sys.stdin)
inputs = Path(request["input_directory"]); out = Path(request["output_directory"])
names = json.loads((inputs / "expected.json").read_text())
artifacts = []
for name in names:
    src = inputs / "mod.py" if (inputs / "mod.py").exists() else inputs / "pkg" / "mod.py"
    data = src.read_bytes()
    if "wrong" in (inputs / "brief.txt").read_text():
        data = b"VALUE = 2\\n"
    (out / name).parent.mkdir(parents=True, exist_ok=True)
    (out / name).write_bytes(data)
    artifacts.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
print(json.dumps({"status": "completed", "finish_reason": "stop", "actual_model": request["model"], "manifest_sha256": request["manifest_sha256"], "artifacts": artifacts}))
"""

REVIEW_ADAPTER = """
import hashlib, json, sys
from pathlib import Path
request = json.load(sys.stdin)
inputs = Path(request["input_directory"]); out = Path(request["output_directory"])
names = json.loads((inputs / "expected.json").read_text())
reject = "reject" in (inputs / "brief.txt").read_text()
review = {"verdict": "rejected" if reject else "approved", "checked": ["schema", "verdict", "findings"]}
if reject:
    review["findings"] = [{"location": "mod2.py", "input": "VALUE check", "expected": "VALUE = 1", "observed": "VALUE = 2"}]
else:
    review["findings"] = []
data = (json.dumps(review) + "\\n").encode()
artifacts = []
for name in names:
    (out / name).parent.mkdir(parents=True, exist_ok=True)
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


def make_review_task(tid, brief_name, author_family="claude"):
    return dict(
        id=tid,
        category="independent_review",
        brief=brief_name,
        inputs=[brief_name, "grid/tests/test_review_schema.py"],
        tests=["grid/tests/test_review_schema.py"],
        artifacts=["reply.txt"],
        lanes=["go"],
        author_family=author_family,
        budget={"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None},
        state="ready",
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
    (project / "brief-reject.txt").write_text("reject this review\n")
    (project / "brief-approve.txt").write_text("approve this review\n")
    (project / "test_mod2.py").write_text(
        "import unittest\nimport mod2\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(mod2.VALUE, 1)\n"
    )
    schema_dst = project / "grid/tests"
    schema_dst.mkdir(parents=True)
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "grid/tests/test_review_schema.py",
        schema_dst / "test_review_schema.py",
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
            "categories": ["pure_function", "independent_review"],
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
    assert [r for r in world["ledger"].status() if r["account"] == world["account"]] == []


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
    # The passing task got a review task: staged artifact, generated brief, cross-family marker.
    review = json.loads((world["board"] / "review-copy-ok.json").read_text())
    assert review["state"] == "ready" and review["category"] == "independent_review"
    assert review["author_family"] == "glm" and review["artifacts"] == ["reply.txt"]
    assert (world["project"] / "grid/board/review/copy-ok/mod2.py").read_text() == "VALUE = 1\n"
    assert "review-copy-ok" in (world["project"] / "grid/briefs/review-copy-ok.txt").read_text()
    assert wrong.get("blocked_reason") and not (world["board"] / "review-copy-wrong.json").exists()
    card = {
        (e["category"], e["accepted"], e["attempts"])
        for e in world["ledger"].scorecard(account=world["account"])
    }
    assert card == {("pure_function", 1, 2)}
    # The project tree's top level was never modified.
    assert sorted(p.name for p in world["project"].iterdir()) == [
        "brief-approve.txt",
        "brief-reject.txt",
        "brief-wrong.txt",
        "brief.txt",
        "grid",
        "mod.py",
        "test_mod2.py",
    ]
    # A second tick finds the created review task ready but no independent-family lane for it.
    assert runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        world["lanes"],
        world["lanes_path"],
        {"go": world["account"]},
        world["packets"],
        now=now + 1,
    ) == [
        {"task": "review-copy-ok", "lane": None, "attempt": None, "result": "no_independent_family"}
    ]


def test_held_attempt_with_complete_files_gets_a_verify_followup(world, monkeypatch, tmp_path):
    # An adapter that writes the artifact but exits without a receipt (deadline-like), then a
    # second run that succeeds: the runner must resolve the first and dispatch the follow-up.
    flaky = tmp_path / "flaky_adapter.py"
    flaky.write_text(
        FAKE_ADAPTER.replace(
            'print(json.dumps({"status": "completed"',
            'import os\nif not os.path.exists(str(Path(request["input_directory"]) / "brief.txt")) or "already exist" not in (Path(request["input_directory"]) / "brief.txt").read_text():\n    (Path(request["output_directory"]).parent / "work").mkdir(exist_ok=True)\n    for name in names:\n        (Path(request["output_directory"]).parent / "work" / name).write_bytes((inputs / "mod.py").read_bytes())\n    sys.exit(1)\nprint(json.dumps({"status": "completed"',
        )
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(flaky)])
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    (world["board"] / "copy-wrong.json").unlink()
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
    assert results[0]["result"] == "passed"
    states = sorted(
        (r["task"].split("-2026")[0], r["state"])
        for r in world["ledger"].status()
        if r["account"] == world["account"]
    )
    assert states == [("copy-ok", "completed"), ("copy-ok", "failed")]
    assert json.loads((world["board"] / "copy-ok.json").read_text())["state"] == "review_pending"


def test_rejected_review_blocks_the_task(world, monkeypatch):
    adapter = world["lanes_path"].parent / "review_adapter.py"
    adapter.write_text(REVIEW_ADAPTER)
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
    (world["board"] / "rev.json").write_text(
        json.dumps(make_review_task("rev", "brief-reject.txt"))
    )
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
    assert results[0]["result"] == "review_rejected"
    task = json.loads((world["board"] / "rev.json").read_text())
    assert task["state"] == "blocked"
    assert "review rejected with 1 finding" in task["blocked_reason"]
    # The rejection is recorded as a failed outcome, never an acceptance.
    card = world["ledger"].scorecard(account=world["account"])
    assert [(e["category"], e["accepted"], e["attempts"]) for e in card] == [
        ("independent_review", 0, 1)
    ]


def test_approved_review_passes_and_creates_no_further_review(world, monkeypatch):
    adapter = world["lanes_path"].parent / "review_adapter.py"
    adapter.write_text(REVIEW_ADAPTER)
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
    (world["board"] / "rev.json").write_text(
        json.dumps(make_review_task("rev", "brief-approve.txt"))
    )
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
    assert results[0]["result"] == "passed"
    assert json.loads((world["board"] / "rev.json").read_text())["state"] == "passed"
    # A review of a review is never generated.
    assert not (world["board"] / "review-rev.json").exists()


def test_dispatched_state_is_saved_before_the_attempt(world, monkeypatch):
    seen = []
    real_dispatch = runner.dispatch

    def probing_dispatch(*args, **kwargs):
        task_id = args[4]["id"]
        seen.append(json.loads((world["board"] / f"{task_id}.json").read_text())["state"])
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(runner, "dispatch", probing_dispatch)
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    (world["board"] / "copy-wrong.json").unlink()
    runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        world["lanes"],
        world["lanes_path"],
        {"go": world["account"]},
        world["packets"],
        now=now,
    )
    assert seen == ["dispatched"]
    # Terminal states replace dispatched.
    assert json.loads((world["board"] / "copy-ok.json").read_text())["state"] in (
        "review_pending",
        "passed",
    )


def test_name_collision_blocks_before_dispatch(world):
    clash = make_task("clash", "brief.txt")
    clash["artifacts"] = ["test_mod2.py"]  # an artifact named like the coordinator test
    (world["board"] / "clash.json").write_text(json.dumps(clash))
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
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
    assert results[0]["result"] == "blocked: name collision"
    task = json.loads((world["board"] / "clash.json").read_text())
    assert task["state"] == "blocked" and "test_mod2.py" in task["blocked_reason"]
    assert [r for r in world["ledger"].status() if r["account"] == world["account"]] == []


def test_a_test_sharing_an_input_basename_blocks_too(world):
    clash = make_task("clash", "brief.txt")
    clash["tests"] = ["sub/mod.py"]  # different file, same basename as a staged input
    (world["board"] / "clash.json").write_text(json.dumps(clash))
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
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
    assert results[0]["result"] == "blocked: name collision"
    assert [r for r in world["ledger"].status() if r["account"] == world["account"]] == []


def test_stage_packet_refuses_colliding_input_names(world):
    task = make_task("collide", "brief.txt")
    task["inputs"] = ["brief.txt", "a/config.json", "b/config.json"]
    for name in task["inputs"]:
        source = world["project"] / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("{}\n")
    with pytest.raises(Refused, match="must not collide"):
        runner.stage_packet(world["project"], task, world["packets"] / "collide")
    task2 = make_task("collide2", "brief.txt")
    task2["inputs"] = ["brief.txt", "expected.json"]
    with pytest.raises(Refused, match="expected.json"):
        runner.stage_packet(world["project"], task2, world["packets"] / "collide2")


def test_run_tests_harness_failure_blocks_and_the_tick_continues(world, monkeypatch):
    real = runner.run_tests
    calls = []

    def flaky(project_root, task, artifact_dir, scratch):
        calls.append(task["id"])
        if task["id"] == "copy-ok":
            raise FileNotFoundError("mod2.py missing from artifacts")
        return real(project_root, task, artifact_dir, scratch)

    monkeypatch.setattr(runner, "run_tests", flaky)
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
    assert by_task["copy-ok"]["result"] == "test_error"
    assert by_task["copy-wrong"]["result"] == "failed_tests"  # later tasks still dispatched
    ok = json.loads((world["board"] / "copy-ok.json").read_text())
    assert ok["state"] == "blocked" and "test harness error" in ok["blocked_reason"]
    card = world["ledger"].scorecard(account=world["account"])
    outcomes = {e["category"]: (e["attempts"], e["accepted"]) for e in card}
    assert outcomes["pure_function"][0] == 2  # both attempts got an outcome recorded


def test_refusal_before_any_attempt_leaves_the_task_ready(world, monkeypatch):
    real = runner.run_tests
    monkeypatch.setattr(runner, "run_tests", real)
    # Break staging by removing a declared input after the board loads: simplest is to point
    # the task at a missing file and reload, so select still succeeds and dispatch refuses.
    task = make_task("ghost", "brief.txt")
    task["inputs"] = ["brief.txt", "ghost.py"]
    (world["board"] / "ghost.json").write_text(json.dumps(task))
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
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
    assert results[0]["result"].startswith("refused:")
    assert json.loads((world["board"] / "ghost.json").read_text())["state"] == "ready"
    assert [r for r in world["ledger"].status() if r["account"] == world["account"]] == []


def test_parse_review_accepts_fences_and_refuses_non_verdicts(tmp_path):
    good = tmp_path / "reply.txt"
    good.write_text('{"verdict": "approved", "findings": [], "checked": ["a"]}\n')
    assert runner.parse_review(good)["verdict"] == "approved"
    fenced = tmp_path / "fenced.txt"
    fenced.write_text('```json\n{"verdict": "rejected", "findings": [], "checked": ["a"]}\n```\n')
    assert runner.parse_review(fenced)["verdict"] == "rejected"
    bad = tmp_path / "bad.txt"
    bad.write_text('{"answer": 42}\n')
    with pytest.raises(ValueError):
        runner.parse_review(bad)
    with pytest.raises(OSError):
        runner.parse_review(tmp_path / "absent.txt")


def test_tree_task_keeps_paths_and_discovers_tests(world):
    project = world["project"]
    (project / "pkg").mkdir()
    (project / "pkg/__init__.py").write_text("")
    (project / "pkg/mod.py").write_text("VALUE = 1\n")
    (project / "tests").mkdir()
    (project / "tests/__init__.py").write_text("")
    (project / "tests/test_pkg.py").write_text(
        "import unittest\nfrom pkg import mod2\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(mod2.VALUE, 1)\n"
    )
    for f in world["board"].glob("*.json"):
        f.unlink()
    task = dict(
        make_task("tree-ok", "brief.txt"),
        inputs=["brief.txt", "pkg/__init__.py", "pkg/mod.py", "tests/__init__.py"],
        tests=["tests/test_pkg.py"],
        artifacts=["pkg/mod2.py"],
    )
    (world["board"] / "tree-ok.json").write_text(json.dumps(task))
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
    assert results == [
        {"task": "tree-ok", "lane": "go", "attempt": results[0]["attempt"], "result": "passed"}
    ]
    packet = next((world["packets"] / "tree-ok").iterdir())
    assert (packet / "input/pkg/mod.py").is_file() and json.loads(
        (packet / "input/expected.json").read_text()
    ) == ["pkg/mod2.py"]
    assert (packet / "scratch/pkg/mod2.py").read_text() == "VALUE = 1\n"
