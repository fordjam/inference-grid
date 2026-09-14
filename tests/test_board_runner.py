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
    # A deadline-shaped hold (lane verdict says wall_deadline) that wrote the artifact but
    # exits without a receipt, then a second run that succeeds: the runner must resolve the
    # first and dispatch the follow-up.
    flaky = tmp_path / "flaky_adapter.py"
    flaky.write_text(
        FAKE_ADAPTER.replace(
            'print(json.dumps({"status": "completed"',
            'import os\nif not os.path.exists(str(Path(request["input_directory"]) / "brief.txt")) or "already exist" not in (Path(request["input_directory"]) / "brief.txt").read_text():\n    attempt = Path(request["output_directory"]).parent\n    (attempt / "work").mkdir(exist_ok=True)\n    for name in names:\n        (attempt / "work" / name).write_bytes((inputs / "mod.py").read_bytes())\n    (attempt / "verdict.json").write_text(json.dumps({"supervisor": {"reason": "wall_deadline"}}))\n    sys.exit(1)\nprint(json.dumps({"status": "completed"',
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


def test_receipt_refusal_hold_gets_no_followup(world, monkeypatch, tmp_path):
    # The same complete files, but the adapter failed its own receipt check (no deadline
    # evidence anywhere): the follow-up must not fire and the hold waits for the operator.
    crashing = tmp_path / "crashing_adapter.py"
    crashing.write_text(
        FAKE_ADAPTER.replace(
            'print(json.dumps({"status": "completed"',
            '(Path(request["output_directory"]).parent / "work").mkdir(exist_ok=True)\n'
            "for name in names:\n"
            '    (Path(request["output_directory"]).parent / "work" / name).write_bytes((inputs / "mod.py").read_bytes())\n'
            "sys.exit(1)\n"
            'print(json.dumps({"status": "completed"',
        )
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(crashing)])
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
    assert results[0]["result"] == "held"
    task = json.loads((world["board"] / "copy-ok.json").read_text())
    assert task["state"] == "blocked"
    assert "resolve with evidence" in task["blocked_reason"]
    attempts = [r for r in world["ledger"].status() if r["account"] == world["account"]]
    assert len(attempts) == 1 and attempts[0]["state"] == "held"


def test_rejected_review_blocks_the_task(world, monkeypatch):
    adapter = world["lanes_path"].parent / "review_adapter.py"
    adapter.write_text(REVIEW_ADAPTER)
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
    # A source task waiting for this review; its findings must reach it, not just the review.
    (world["board"] / "calc.json").write_text(
        json.dumps(make_task("calc", "brief.txt", state="review_pending"))
    )
    (world["board"] / "review-calc.json").write_text(
        json.dumps(make_review_task("review-calc", "brief-reject.txt"))
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
    task = json.loads((world["board"] / "review-calc.json").read_text())
    assert task["state"] == "blocked"
    assert "review rejected with 1 finding" in task["blocked_reason"]
    # A behavioral finding is not tagged: the size-hint tag exists for size-only rejections.
    assert "advisory-only" not in task["blocked_reason"]
    source = json.loads((world["board"] / "calc.json").read_text())
    assert source["state"] == "blocked"
    assert "review rejected" in source["blocked_reason"]
    assert "mod2.py" in source["blocked_reason"] and "VALUE = 2" in source["blocked_reason"]
    # The rejection is recorded as a failed outcome, never an acceptance.
    card = world["ledger"].scorecard(account=world["account"])
    assert [(e["category"], e["accepted"], e["attempts"]) for e in card] == [
        ("independent_review", 0, 1)
    ]


def test_review_brief_says_size_hints_are_advisory():
    # A literal reviewer enforces the original brief's 45-line hint (live round 2 rejected
    # a clean artifact on exactly that); the generated brief must state the policy.
    text = runner.review_brief_text({"id": "x", "artifacts": ["reply.txt"], "brief": "b.txt"})
    assert "advisory" in text and "not a finding" in text
    # The output-format rule stays the final sentence; the advisory note comes before it.
    assert text.index("advisory") < text.index("OUTPUT FORMAT")


def test_advisory_size_finding_matches_only_size_citations():
    assert runner.advisory_size_finding({"expected": "~45 lines", "observed": "51 lines"})
    assert runner.advisory_size_finding(
        {"expected": "size under 2 KB", "observed": "length exceeds the budget"}
    )
    assert not runner.advisory_size_finding(
        {"expected": "VALUE = 1", "observed": "VALUE = 2"}
    )
    assert not runner.advisory_size_finding(
        {"expected": "45 lines", "observed": "crashes on empty input"}
    )
    assert not runner.advisory_size_finding({})


def test_a_size_only_rejection_is_tagged_advisory_only(world, monkeypatch):
    # A rejection whose single finding cites only the brief's size hint is blocked with
    # advisory-only in the reason; it is never auto-approved — the retry path answers it.
    adapter = world["lanes_path"].parent / "size_adapter.py"
    adapter.write_text(
        REVIEW_ADAPTER.replace(
            '"expected": "VALUE = 1"',
            '"expected": "about 45 lines per the hint"',
        ).replace(
            '"observed": "VALUE = 2"',
            '"observed": "the artifact is 51 lines"',
        )
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
    (world["board"] / "x.json").write_text(
        json.dumps(make_task("x", "brief.txt", state="review_pending"))
    )
    (world["board"] / "review-x.json").write_text(
        json.dumps(make_review_task("review-x", "brief-reject.txt"))
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
    task = json.loads((world["board"] / "review-x.json").read_text())
    assert task["state"] == "blocked"
    assert "advisory-only" in task["blocked_reason"]
    source = json.loads((world["board"] / "x.json").read_text())
    assert source["state"] == "blocked"  # nothing auto-approved
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


def test_duplicate_test_basenames_block_before_dispatch(world):
    # review-board-runner-4's case: two declared tests sharing a basename both copied to
    # scratch/test_mod2.py, the lenient one overwriting the strict one, and an artifact
    # that failed a declared coordinator test was accepted. Blocks now.
    clash = make_task("clash", "brief.txt")
    clash["tests"] = ["strict/test_mod2.py", "lenient/test_mod2.py"]
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
    assert task["state"] == "blocked"
    assert "strict/test_mod2.py" in task["blocked_reason"]
    assert "lenient/test_mod2.py" in task["blocked_reason"]
    assert world["ledger"].status() == []  # blocked before any attempt existed


def test_duplicate_basenames_within_one_declared_list_block():
    # The same rule holds within inputs in flat tasks (a slash in an artifact would make
    # the task tree, so flat artifact duplicates cannot be declared).
    flat = make_task("x", "brief.txt")
    flat["inputs"] = ["brief.txt", "mod.py", "dup/mod.py"]
    assert "mod.py" in runner.shadowing_names(flat)
    flat = make_task("x", "brief.txt")
    flat["tests"] = ["strict/test_mod2.py", "lenient/test_mod2.py"]
    assert "strict/test_mod2.py" in runner.shadowing_names(flat)
    # Tree tasks flag duplicate test basenames...
    tree = dict(
        make_task("t", "brief.txt"),
        inputs=["brief.txt"],
        tests=["tests/a/test_pkg.py", "tests/b/test_pkg.py"],
        artifacts=["pkg/mod2.py"],
    )
    problem = runner.shadowing_names(tree)
    assert "tests/a/test_pkg.py" in problem and "tests/b/test_pkg.py" in problem
    # ...while distinct basenames — and same-basename tree inputs like two __init__.py —
    # are distinct staged files, not collisions.
    tree["tests"] = ["tests/test_pkg.py"]
    tree["inputs"] = ["brief.txt", "pkg/__init__.py", "tests/__init__.py"]
    assert runner.shadowing_names(tree) == ""


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
    one_line = tmp_path / "one-line.txt"
    one_line.write_text('```json{"verdict": "approved", "findings": [], "checked": ["a"]}```')
    assert runner.parse_review(one_line)["verdict"] == "approved"
    bare_fence = tmp_path / "bare-fence.txt"
    bare_fence.write_text('```{"verdict": "rejected", "findings": [], "checked": ["a"]}```')
    assert runner.parse_review(bare_fence)["verdict"] == "rejected"
    fence_no_object = tmp_path / "empty-fence.txt"
    fence_no_object.write_text("```json```")
    with pytest.raises(ValueError):
        runner.parse_review(fence_no_object)
    prose = tmp_path / "prose.txt"
    prose.write_text("the review looked fine to me\n")
    with pytest.raises(ValueError):
        runner.parse_review(prose)
    bad = tmp_path / "bad.txt"
    bad.write_text('{"answer": 42}\n')
    with pytest.raises(ValueError):
        runner.parse_review(bad)
    with pytest.raises(OSError):
        runner.parse_review(tmp_path / "absent.txt")


def test_unreadable_fenced_reply_blocks_the_review_without_crashing_the_tick(world, monkeypatch):
    # A one-line fenced reply used to raise IndexError inside the tick, stranding every
    # later task; it must block this review as unreadable and leave the tick alive.
    fenced_adapter = world["lanes_path"].parent / "fenced_adapter.py"
    fenced_adapter.write_text(
        REVIEW_ADAPTER.replace(
            'data = (json.dumps(review) + "\\n").encode()',
            'data = b"i read the code, it looked fine"',
        )
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(fenced_adapter)])
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
    review = make_review_task("rev-fence", "brief-approve.txt")
    review["tests"] = []  # no schema test: the reply reaches parse_review unparsed
    (world["board"] / "rev-fence.json").write_text(json.dumps(review))
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
    assert results[0]["result"] == "review_unreadable"
    task = json.loads((world["board"] / "rev-fence.json").read_text())
    assert task["state"] == "blocked"
    assert "review artifact unusable" in task["blocked_reason"]


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


def test_approved_review_accepts_source_and_lands_on_inbox(world, monkeypatch, tmp_path):
    import subprocess

    project = world["project"]
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    (project / "grid/tests").mkdir(parents=True, exist_ok=True)
    (project / "grid/tests/test_review_schema.py").write_text(
        "import json, unittest\nfrom pathlib import Path\n\nclass T(unittest.TestCase):\n    def test_verdict(self):\n        self.assertIn(json.loads(Path('reply.txt').read_text())['verdict'], ('approved', 'rejected'))\n"
    )
    (world["board"] / "copy-wrong.json").unlink()
    lanes = dict(world["lanes"])
    lanes["kimi"] = dict(
        lanes["go"], family="kimi", model="kimi-k3", categories=["independent_review"]
    )
    world["lanes_path"].write_text(json.dumps({"lanes": lanes}))
    world["ledger"].configure_account(
        "kimi-acct",
        1,
        {"five_hour": 10, "weekly": 20},
        time.time() + 600,
        ["kimi-k3"],
        ["kimi-alias"],
    )
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    world["ledger"].record_lane("kimi", dict(ready_record(now), provider="kimi"))
    accounts = {"go": world["account"], "kimi": "kimi-alias"}
    first = runner.tick(
        world["board"],
        project,
        world["ledger"],
        lanes,
        world["lanes_path"],
        accounts,
        world["packets"],
        now=now,
    )
    assert first[0]["result"] == "passed"
    assert (world["board"] / "review/copy-ok/source.json").is_file()
    review_task = json.loads((world["board"] / "review-copy-ok.json").read_text())
    review_task["lanes"] = ["go", "kimi"]
    (world["board"] / "review-copy-ok.json").write_text(json.dumps(review_task))
    adapter = tmp_path / "review_adapter.py"
    # The generated review brief mentions the word "rejected" in its schema; approve unless the
    # original brief itself asked for a rejection.
    adapter.write_text(REVIEW_ADAPTER.replace('reject = "reject" in', 'reject = "brief-reject" in'))
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    second = runner.tick(
        world["board"],
        project,
        world["ledger"],
        lanes,
        world["lanes_path"],
        accounts,
        world["packets"],
        now=now + 1,
    )
    assert second[0]["lane"] == "kimi", second
    assert second[0]["result"].startswith("passed; accepted; landed on grid/inbox"), second
    assert json.loads((world["board"] / "copy-ok.json").read_text())["state"] == "accepted"
    states = {r["state"] for r in world["ledger"].status() if r["account"] == world["account"]}
    assert "accepted" in states
    log = subprocess.run(
        ["git", "-C", str(project), "log", "--oneline", "grid/inbox"],
        capture_output=True,
        text=True,
    ).stdout
    assert "grid: accept copy-ok" in log
    tree = subprocess.run(
        ["git", "-C", str(project), "ls-tree", "-r", "--name-only", "grid/inbox"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "grid/inbox/copy-ok/mod2.py" in tree and "grid/inbox/copy-ok.json" in tree
    assert not (project / "grid/inbox").exists()  # the operator's checkout is untouched


def test_an_artifact_sharing_an_input_basename_blocks_too(world):
    clash = make_task("clash", "brief.txt")
    clash["artifacts"] = ["mod.py"]  # the artifact would silently replace the staged mod.py
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
    assert task["state"] == "blocked" and "staged input mod.py" in task["blocked_reason"]
    assert world["ledger"].status() == []


def seed_active_attempt(world, task_id, alias=None):
    """Submit and claim one attempt on the account, leaving it queued (active)."""
    from inference_grid.ledger import digest

    spec = {
        "authorized": True,
        "model": "glm-5.3-flash",
        "family": "glm",
        "argv": ["/usr/bin/true"],
        "workspace": str(world["packets"] / (task_id + "-ws")),
        "timeout": 60,
        "output_bytes": 1000,
        "inputs": {},
        "manifest_sha256": digest({}),
    }
    world["ledger"].submit(task_id, "project", spec)
    return world["ledger"].claim(
        task_id, alias or world["account"], {"five_hour": 0.01, "weekly": 0.01}
    )


def test_busy_lane_reports_lane_busy_and_skips_dispatch(world):
    seed_active_attempt(world, "seed-1")
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    view = runner.readiness_view(world["ledger"], world["lanes"], now, {"go": world["account"]})
    assert view["go"]["state"] == "busy"  # one active attempt, max_concurrency defaults to 1
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
    assert {r["task"]: r["result"] for r in results} == {
        "copy-ok": "lane_busy",
        "copy-wrong": "lane_busy",
    }
    # Neither task was dispatched and neither left the ready state.
    assert [r["attempt"] for r in results] == [None, None]
    for tid in ("copy-ok", "copy-wrong"):
        assert json.loads((world["board"] / f"{tid}.json").read_text())["state"] == "ready"
    assert len(world["ledger"].status()) == 1  # only the seeded attempt exists


def test_lane_within_concurrency_still_dispatches(world):
    # A wider account and lane cap: one active attempt leaves room for the tick to work.
    account = "go-wide-" + uuid.uuid4().hex[:8]
    world["ledger"].configure_account(
        account, 2, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    seed_active_attempt(world, "seed-1", alias=account)
    world["lanes"]["go"]["max_concurrency"] = 2
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    view = runner.readiness_view(world["ledger"], world["lanes"], now, {"go": account})
    assert view["go"]["state"] == "ready"
    results = runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        world["lanes"],
        world["lanes_path"],
        {"go": account},
        world["packets"],
        now=now,
    )
    assert {r["task"]: r["result"] for r in results} == {
        "copy-ok": "passed",
        "copy-wrong": "failed_tests",
    }


def test_dry_run_plans_without_touching_anything(world):
    # The plan a tick would follow, without the dispatch: one task selects the free lane,
    # one is skipped on the busy account, one on family exclusion — and nothing on disk
    # or in the ledger moves.
    (world["board"] / "copy-ok.json").write_text(
        json.dumps(dict(make_task("copy-ok", "brief.txt"), lanes=["go", "zai"]))
    )
    (world["board"] / "review-fam.json").write_text(
        json.dumps(
            dict(make_review_task("review-fam", "brief-approve.txt"), lanes=["zai"])
        )
    )
    lanes = dict(world["lanes"])
    lanes["zai"] = dict(
        world["lanes"]["go"],
        family="claude",
        model="kimi-k3",
        categories=["pure_function", "independent_review"],
    )
    world["ledger"].configure_account(
        "zai-acct",
        1,
        {"five_hour": 10, "weekly": 20},
        time.time() + 600,
        ["kimi-k3"],
        ["zai-alias"],
    )
    seed_active_attempt(world, "seed-busy")  # the go account is at capacity
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    world["ledger"].record_lane("zai", dict(ready_record(now), provider="zai"))
    before = {p.name: p.read_bytes() for p in world["board"].glob("*.json")}
    plan = runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        lanes,
        world["lanes_path"],
        {"go": world["account"], "zai": "zai-alias"},
        world["packets"],
        now=now,
        dry_run=True,
    )
    assert plan["readiness"] == {"go": {"state": "busy"}, "zai": {"state": "ready"}}
    assert plan["plan"] == [
        {"task": "copy-ok", "lane": "zai", "reason": "selected"},
        {"task": "copy-wrong", "lane": None, "reason": "lane_busy"},
        {"task": "review-fam", "lane": None, "reason": "no_independent_family"},
    ]
    # Nothing was dispatched, written or staged.
    assert {p.name: p.read_bytes() for p in world["board"].glob("*.json")} == before
    assert len(world["ledger"].status()) == 1  # only the seeded attempt exists
    assert not world["packets"].exists()


def test_two_boards_tick_in_order_and_share_the_busy_count(world, tmp_path, monkeypatch):
    # board-tick-all looped configs; the packaged board-tick took one. With boards: [...]
    # a call ticks each in order and readiness carries across: board A's held attempt
    # makes board B's task lane_busy instead of colliding with the account.
    from inference_grid.cli import board_tick

    second = tmp_path / "grid2/board"
    second.mkdir(parents=True)
    (second / "task-b.json").write_text(json.dumps(make_task("task-b", "brief.txt")))
    crashing = world["lanes_path"].parent / "crashing_adapter.py"
    crashing.write_text("import sys\nsys.exit(1)\n")
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(crashing)])
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    shared = {
        "project_root": str(world["project"]),
        "lanes_path": str(world["lanes_path"]),
        "accounts_by_lane": {"go": world["account"]},
    }
    report = board_tick(
        world["ledger"],
        boards=[
            dict(shared, board_dir=str(world["board"]), packets_root=str(world["packets"] / "a")),
            dict(shared, board_dir=str(second), packets_root=str(world["packets"] / "b")),
        ],
    )
    assert [entry["board"] for entry in report] == [str(world["board"]), str(second)]
    assert {r["task"]: r["result"] for r in report[0]["results"]} == {
        "copy-ok": "held",
        "copy-wrong": "lane_busy",  # board A's own second task already sees the hold
    }
    assert {r["task"]: r["result"] for r in report[1]["results"]} == {"task-b": "lane_busy"}
    assert json.loads((second / "task-b.json").read_text())["state"] == "ready"
    attempts = [r for r in world["ledger"].status() if r["account"] == world["account"]]
    assert len(attempts) == 1 and attempts[0]["state"] == "held"


def test_a_hold_mid_tick_makes_the_next_task_lane_busy(world, monkeypatch):
    # Busy used to be evaluated once per tick: the first task's attempt went held
    # mid-tick and the second task on the same account was still dispatched and refused
    # 'account busy'. The tick must re-evaluate after every dispatch.
    crashing = world["lanes_path"].parent / "crashing_adapter.py"
    crashing.write_text("import sys\nsys.exit(1)\n")
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(crashing)])
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
    assert by_task["copy-ok"]["result"] == "held"
    assert by_task["copy-wrong"]["result"] == "lane_busy"
    assert by_task["copy-wrong"]["attempt"] is None
    assert json.loads((world["board"] / "copy-wrong.json").read_text())["state"] == "ready"
    # The second task never reached the lane: one attempt exists and nothing was refused.
    attempts = [r for r in world["ledger"].status() if r["account"] == world["account"]]
    assert len(attempts) == 1 and attempts[0]["state"] == "held"


def test_a_held_attempt_makes_the_lane_busy(world):
    # A held attempt still occupies the account slot in the ledger, so after a hold the
    # lane must be busy, not three refused dispatches waiting to happen.
    aid, _ = seed_active_attempt(world, "seed-hold")
    world["ledger"].hold(aid, "Refused: timeout: provider acceptance may be ambiguous")
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    view = runner.readiness_view(world["ledger"], world["lanes"], now, {"go": world["account"]})
    assert view["go"]["state"] == "busy"
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
    assert {r["task"]: r["result"] for r in results} == {
        "copy-ok": "lane_busy",
        "copy-wrong": "lane_busy",
    }
    assert len(world["ledger"].status()) == 1  # nothing new dispatched


def write_link(board, source_id, **fields):
    """A review source link; fields default to a first-round link for source_id."""
    link = dict(
        task=source_id,
        attempt="a" * 8 + "-0000",
        lane="go",
        family="glm",
        receipt_digest="d" * 64,
        artifacts=["mod2.py"],
        review_task="review-" + source_id,
    )
    link.update(fields)
    stage = board / "review" / source_id
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "source.json").write_text(json.dumps(link))
    return link


def test_a_retry_review_resolves_its_source_by_link(world, monkeypatch):
    # review-x-2 cannot be resolved by stripping the prefix (that yields x-2): the source
    # link names the retry explicitly, so its approval accepts x.
    (world["board"] / "copy-wrong.json").unlink()
    lanes = dict(world["lanes"])
    lanes["kimi"] = dict(
        lanes["go"], family="kimi", model="kimi-k3", categories=["independent_review"]
    )
    world["ledger"].configure_account(
        "kimi-acct",
        1,
        {"five_hour": 10, "weekly": 20},
        time.time() + 600,
        ["kimi-k3"],
        ["kimi-alias"],
    )
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    world["ledger"].record_lane("kimi", dict(ready_record(now), provider="kimi"))
    accounts = {"go": world["account"], "kimi": "kimi-alias"}
    first = runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        lanes,
        world["lanes_path"],
        accounts,
        world["packets"],
        now=now,
    )
    assert first[0]["task"] == "copy-ok" and first[0]["result"] == "passed"
    # copy-ok is now review_pending with a source link naming review-copy-ok; the operator
    # re-dispatches the review as a new task and records the change in the link.
    link = json.loads((world["board"] / "review/copy-ok/source.json").read_text())
    assert link["review_task"] == "review-copy-ok"
    link["review_task"] = "review-copy-ok-2"
    (world["board"] / "review/copy-ok/source.json").write_text(json.dumps(link))
    retry = make_review_task("review-copy-ok-2", "brief-approve.txt")
    retry["lanes"] = ["kimi"]
    (world["board"] / "review-copy-ok-2.json").write_text(json.dumps(retry))
    adapter = world["lanes_path"].parent / "review_adapter.py"
    # The generated review brief quotes the word "rejected" in its schema; reject only
    # when the original brief itself asks for it.
    adapter.write_text(REVIEW_ADAPTER.replace('reject = "reject" in', 'reject = "brief-reject" in'))
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    second = runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        lanes,
        world["lanes_path"],
        accounts,
        world["packets"],
        now=now + 1,
    )
    result = second[0]["result"]
    assert "accepted" in result, result
    assert json.loads((world["board"] / "copy-ok.json").read_text())["state"] == "accepted"


def test_prefix_fallback_still_resolves_legacy_links(world):
    # A link without the review_task field (first-round files) resolves by the prefix rule.
    board = world["board"]
    link = write_link(board, "y")
    del link["review_task"]
    (board / "review/y/source.json").write_text(json.dumps(link))
    found, path = runner.find_source_link(board, "review-y")
    assert found["task"] == "y"
    missing, _ = runner.find_source_link(board, "review-nowhere")
    assert missing is None


def test_a_retry_rejection_blocks_its_source(world, monkeypatch):
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
    adapter = world["lanes_path"].parent / "review_adapter.py"
    adapter.write_text(REVIEW_ADAPTER)
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    source = make_task("x", "brief.txt", state="review_pending")
    (world["board"] / "x.json").write_text(json.dumps(source))
    write_link(world["board"], "x", review_task="review-x-2")
    retry = make_review_task("review-x-2", "brief-reject.txt")
    (world["board"] / "review-x-2.json").write_text(json.dumps(retry))
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
    assert json.loads((world["board"] / "x.json").read_text())["state"] == "blocked"
    assert (
        "review rejected" in json.loads((world["board"] / "x.json").read_text())["blocked_reason"]
    )


def test_superseded_sources_are_never_touched(world, monkeypatch):
    (world["board"] / "copy-wrong.json").unlink()
    adapter = world["lanes_path"].parent / "review_adapter.py"
    adapter.write_text(REVIEW_ADAPTER.replace('reject = "reject" in', 'reject = "brief-reject" in'))
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    # The source is already closed as superseded by a recorded retry.
    source = dict(
        make_task("x", "brief.txt", state="blocked"),
        blocked_reason="superseded: replaced by x-2 (review round 2)",
    )
    (world["board"] / "x.json").write_text(json.dumps(source))
    write_link(world["board"], "x")
    # An approving review of a superseded source accepts nothing.
    approve = make_review_task("review-x", "brief-approve.txt")
    (world["board"] / "review-x.json").write_text(json.dumps(approve))
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
    review_row = next(r for r in results if r["task"] == "review-x")
    assert "superseded" in review_row["result"], review_row
    task = json.loads((world["board"] / "x.json").read_text())
    assert task["state"] == "blocked" and task["blocked_reason"].startswith("superseded:")
    # A rejecting review of a superseded source leaves it alone too.
    superseded_task = runner.superseded
    assert superseded_task(task)
    rejection = make_review_task("review-x", "brief-reject.txt")
    rejection["state"] = "blocked"  # never dispatched; call propagate directly
    board = runner.load_board(world["board"])
    runner.propagate_rejection(
        board, world["board"], "review-x", [{"location": "mod2.py", "observed": "VALUE = 2"}]
    )
    task = json.loads((world["board"] / "x.json").read_text())
    assert task["blocked_reason"].startswith("superseded:")


def test_a_403_verdict_excludes_the_model_until_expiry(world, monkeypatch):
    # An adapter whose verdict records an endpoint 403 for the model: the hold also
    # records unsupported_until for the lane's model, and readiness excludes the lane.
    (world["board"] / "copy-wrong.json").unlink()
    refusing = world["lanes_path"].parent / "refusing_adapter.py"
    refusing.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "attempt = Path(request['output_directory']).parent\n"
        "verdict = {'refusal': 'endpoint returned HTTP 403'}\n"
        "(attempt / 'verdict.json').write_text(json.dumps(verdict))\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(refusing)])
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
    assert results[0]["result"] == "held"
    view = runner.readiness_view(world["ledger"], world["lanes"], now + 1, {"go": world["account"]})
    assert view["go"]["state"] == "unqualified"
    with world["ledger"].engine.connect() as con:
        from inference_grid.ledger import lanes as lane_records, select

        record = (
            con.execute(select(lane_records).where(lane_records.c.provider == "go"))
            .mappings()
            .one()["record"]
        )
    until = record["unsupported_until"]["glm-5.3-flash"]
    assert now + 1 < until <= now + runner.MODEL_REFUSAL_TTL + 5


def test_unsupported_until_expires_and_ignores_other_models(world):
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    accounts = {"go": world["account"]}
    # A refusal for another model does not touch this lane's model...
    world["ledger"].record_lane(
        "go", dict(ready_record(now), unsupported_until={"kimi-k3": now + 60})
    )
    view = runner.readiness_view(world["ledger"], world["lanes"], now + 1, accounts)
    assert view["go"]["state"] == "ready"
    # ...a refusal for this model does, until it expires...
    world["ledger"].record_lane(
        "go", dict(ready_record(now), unsupported_until={"glm-5.3-flash": now + 60})
    )
    view = runner.readiness_view(world["ledger"], world["lanes"], now + 1, accounts)
    assert view["go"]["state"] == "unqualified"
    # ...and an expired entry restores the lane.
    world["ledger"].record_lane(
        "go", dict(ready_record(now), unsupported_until={"glm-5.3-flash": now - 1})
    )
    view = runner.readiness_view(world["ledger"], world["lanes"], now + 1, accounts)
    assert view["go"]["state"] == "ready"


def test_a_region_optin_refusal_excludes_the_model_until_expiry(world, monkeypatch):
    # DeepSeek's 403 says the hosting opt-in is off: its own region_optin_required
    # refusal, recorded per model in the lane record like any other 403.
    (world["board"] / "copy-wrong.json").unlink()
    lanes = dict(world["lanes"])
    lanes["go"] = dict(lanes["go"], model="deepseek-v4-flash")
    world["ledger"].configure_account(
        "ds-acct",
        1,
        {"five_hour": 10, "weekly": 20},
        time.time() + 600,
        ["deepseek-v4-flash"],
        ["ds-alias"],
    )
    refusing = world["lanes_path"].parent / "region_refusing_adapter.py"
    refusing.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "attempt = Path(request['output_directory']).parent\n"
        "verdict = {'refusal': 'region_optin_required: enable China hosting"
        " in the Go console'}\n"
        "(attempt / 'verdict.json').write_text(json.dumps(verdict))\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(refusing)])
    now = time.time()
    world["ledger"].record_lane("go", ready_record(now))
    results = runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        lanes,
        world["lanes_path"],
        {"go": "ds-alias"},
        world["packets"],
        now=now,
    )
    assert results[0]["result"] == "held"
    view = runner.readiness_view(world["ledger"], lanes, now + 1, {"go": "ds-alias"})
    assert view["go"]["state"] == "unqualified"
    with world["ledger"].engine.connect() as con:
        from inference_grid.ledger import lanes as lane_records, select

        record = (
            con.execute(select(lane_records).where(lane_records.c.provider == "go"))
            .mappings()
            .one()["record"]
        )
    until = record["unsupported_until"]["deepseek-v4-flash"]
    assert now + 1 < until <= now + runner.MODEL_REFUSAL_TTL + 5


def test_lane_records_carry_metadata_without_confusing_the_classifier(world):
    # record_lane stores the routing metadata while lane_readiness keeps classifying the
    # strict key set, so doctor's lane view stays valid too.
    now = time.time()
    world["ledger"].record_lane(
        "go", dict(ready_record(now), unsupported_until={"glm-5.3-flash": now + 60})
    )
    from inference_grid.lane_readiness import lane_readiness
    from inference_grid.ledger import classifier_view

    with world["ledger"].engine.connect() as con:
        from inference_grid.ledger import lanes as lane_records, select

        record = (
            con.execute(select(lane_records).where(lane_records.c.provider == "go"))
            .mappings()
            .one()["record"]
        )
    assert "unsupported_until" in record
    assert lane_readiness(classifier_view(record), now + 1)["state"] == "ready"


def test_transport_timeout_hold_names_the_bounds(world, monkeypatch):
    # A hold whose verdict records a transport timeout blocks the task with the refusal
    # and the three bounds, so the operator needs no packet dig to know what clipped.
    (world["board"] / "copy-wrong.json").unlink()
    slow = world["lanes_path"].parent / "slow_adapter.py"
    slow.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "attempt = Path(request['output_directory']).parent\n"
        "verdict = {\n"
        "    'refusal': 'transport_error: TimeoutError',\n"
        "    'transport_timeout': 400,\n"
        "    'task_wall_seconds': 600,\n"
        "    'lane_wall_seconds': 400,\n"
        "}\n"
        "(attempt / 'verdict.json').write_text(json.dumps(verdict))\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(slow)])
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
    assert results[0]["result"] == "held"
    task = json.loads((world["board"] / "copy-ok.json").read_text())
    assert task["blocked_reason"] == (
        "attempt "
        + results[0]["attempt"]
        + " held: transport_error: TimeoutError"
        + " (transport 400 s, task 600 s, lane 400 s); resolve with evidence"
    )


def test_reasoning_overrun_hold_names_the_counts(world, monkeypatch):
    # A kimi-style overrun (length with no content) blocks the task with the refusal and
    # both counts, so neither the hold reason nor board-status says "did not stop
    # normally".
    (world["board"] / "copy-wrong.json").unlink()
    counts = "reasoning_tokens 16000 of max_tokens 10000"
    overrun = world["lanes_path"].parent / "overrun_adapter.py"
    overrun.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "attempt = Path(request['output_directory']).parent\n"
        "verdict = {\n"
        "    'refusal': 'reasoning_overrun: finish_reason length with no content"
        " (" + counts + ")',\n"
        "    'finish_reason': 'length',\n"
        "    'max_tokens': 10000,\n"
        "}\n"
        "(attempt / 'verdict.json').write_text(json.dumps(verdict))\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(overrun)])
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
    assert results[0]["result"] == "held"
    task = json.loads((world["board"] / "copy-ok.json").read_text())
    assert task["blocked_reason"] == (
        "attempt "
        + results[0]["attempt"]
        + " held: reasoning_overrun: finish_reason length with no content"
        + " (" + counts + "); resolve with evidence"
    )


def test_a_hold_without_a_transport_verdict_keeps_the_plain_reason(world, monkeypatch):
    (world["board"] / "copy-wrong.json").unlink()
    crashing = world["lanes_path"].parent / "crashing_adapter.py"
    crashing.write_text("import sys\nsys.exit(1)\n")
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(crashing)])
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
    assert results[0]["result"] == "held"
    task = json.loads((world["board"] / "copy-ok.json").read_text())
    assert task["blocked_reason"] == (
        "attempt " + results[0]["attempt"] + " held; resolve with evidence"
    )


def test_review_briefs_forbid_tool_calls():
    # review-ff-glm-r6-c8e5c2c emitted a tool-calling transcript; every generated brief
    # now tells the model it has no tools, just before the output-format rule.
    task = {"id": "review-x", "artifacts": ["reply.txt"], "brief": "b.txt"}
    text = runner.review_brief_text(task)
    sentence = "You have no tools; every file you need is in this message. Do not emit tool calls."
    assert sentence in text
    assert text.index("Do not emit tool calls.") < text.index("OUTPUT FORMAT")
    # Branch-review briefs build on the same function and carry the sentence too.
    branch_text = runner.review_brief_text(task, context="The work under review is a range.")
    assert sentence in branch_text


def test_a_tool_markup_hold_names_the_transcript(world, monkeypatch):
    (world["board"] / "copy-wrong.json").unlink()
    transcript = '<|open|>tools<|sep|><|open|>call tool="bash" ls -la'
    overrun = world["lanes_path"].parent / "markup_adapter.py"
    overrun.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "attempt = Path(request['output_directory']).parent\n"
        "verdict = {'refusal': 'tool_markup: the reply is a tool-calling transcript, "
        "not a verdict: " + transcript + "'}\n"
        "(attempt / 'verdict.json').write_text(json.dumps(verdict))\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(overrun)])
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
    assert results[0]["result"] == "held"
    task = json.loads((world["board"] / "copy-ok.json").read_text())
    assert task["blocked_reason"] == (
        "attempt " + results[0]["attempt"] + " held: tool_markup: the reply is a "
        "tool-calling transcript, not a verdict: " + transcript + "; resolve with evidence"
    )


def test_a_canary_dispatches_on_an_unverified_lane_and_nothing_else_does(world, monkeypatch):
    # A fresh lane has no qualification evidence yet: only its canary may run on it.
    from inference_grid.board.new import lane_init

    lane_init(world["board"], world["project"], "go")
    # The lane record qualifies nothing and auth is unknown: the classifier says unverified.
    world["ledger"].record_lane(
        "go",
        dict(
            ready_record(time.time()),
            qualification="unqualified",
            auth="unknown",
        ),
    )
    adapter = world["lanes_path"].parent / "ok_adapter.py"
    adapter.write_text(
        "import hashlib, json, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "out = Path(request['output_directory'])\n"
        "(out / 'reply.txt').write_text('OK\\n')\n"
        "artifacts = [{'path': 'reply.txt', 'sha256': hashlib.sha256(b'OK\\n').hexdigest()}]\n"
        "print(json.dumps({'status': 'completed', 'finish_reason': 'stop',"
        " 'actual_model': request['model'], 'manifest_sha256': request['manifest_sha256'],"
        " 'artifacts': artifacts}))\n"
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])
    now = time.time()
    world["ledger"].record_lane("go", dict(ready_record(now), auth="unknown", qualification="unqualified"))
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
    assert by_task["canary-go"]["result"] == "passed"
    # A canary is lane evidence, not deliverable work: no review task spawns for it.
    assert not (world["board"] / "review-canary-go.json").exists()
    assert json.loads((world["board"] / "canary-go.json").read_text())["state"] == "passed"
    # The regular work tasks on the same lane are refused: the lane is not for them yet.
    assert by_task["copy-ok"]["result"] == "no_ready_lane"
    assert by_task["copy-wrong"]["result"] == "no_ready_lane"
    # The canary's outcome is recorded under the canary category — accepted, which is
    # the row the qualification gates consume.
    card = world["ledger"].scorecard(account=world["account"])
    assert [(e["category"], e["accepted"], e["attempts"]) for e in card] == [("canary", 1, 1)]


def test_a_recently_refused_model_blocks_even_the_canary(world, monkeypatch):
    from inference_grid.board.new import lane_init

    lane_init(world["board"], world["project"], "go")
    (world["board"] / "copy-ok.json").unlink()
    (world["board"] / "copy-wrong.json").unlink()
    now = time.time()
    world["ledger"].record_lane(
        "go",
        dict(
            ready_record(now),
            auth="unknown",
            qualification="unqualified",
            unsupported_until={"glm-5.3-flash": now + 3600},
        ),
    )
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
    assert results[0]["result"] == "no_ready_lane"
    assert world["ledger"].status() == []


def test_lane_view_marks_first_party_families_explicit_only(world):
    # The runner's selection inputs are built from lane_view: first-party families are
    # marked explicit_only so nothing auto-selects them for tasks that do not name them.
    lanes = dict(world["lanes"])
    lanes["claude"] = dict(lanes["go"], family="claude", model="claude-opus-4")
    lanes["openai"] = dict(lanes["go"], family="openai", model="codex-5")
    now = time.time()
    view = runner.lane_view(lanes, now)
    assert view["go"]["explicit_only"] is False
    assert view["claude"]["explicit_only"] is True
    assert view["openai"]["explicit_only"] is True
    # Selection is structurally restricted to the task's named lanes: a task naming only
    # go never considers the explicit-only lane, whatever its readiness.
    world["ledger"].record_lane("claude", dict(ready_record(now), provider="claude"))
    allowed = {k: view[k] for k in ("go",)}
    assert "claude" in view and "claude" not in allowed
