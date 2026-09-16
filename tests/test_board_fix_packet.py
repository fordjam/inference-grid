"""A held packet drafts its own fix (brief 20, M1): one plan task per (task, reason).

The packet loop runs for real against a fake agent and an identity sandbox (the same seams
`tests/test_board_failover.py` uses); the plan lane's process seam is faked so a plan
attempt completes with a `packet.md` this test controls. Staging, admission, routing,
settlement and the board files run exactly as production runs them. No network, no real
CLI, no sandbox.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from inference_grid.board import packet_task, plan_task, runner
from inference_grid.board.fix_packet import (
    fix_plan_id,
    fix_reason,
    plan_lanes,
    same_gate_repeated,
)
from inference_grid.lanes import packet
from inference_grid.ledger import Ledger

BRIEF_DOC = """# Test brief

## 1. Hard rules

- never read secrets
- never push

---

## Phase A

#### A1. Test packet
Write the report and fix `src/pkg/worker.py`.
"""

# The plan lane's answer: a packet section plus the gates/tests block the plan node parses.
PACKET_MD = """#### K1. Retry budget on the worker

Location: `src/pkg/worker.py`.
Acceptance tests: `tests/test_worker_retry.py`.

```json
{"gates": [{"name": "fail", "argv": ["python", "-c", "pass"]}],
 "tests": ["tests/test_worker_retry.py"]}
```
"""

FAKE_AGENT = """
import json, subprocess, sys
from pathlib import Path
report = Path(sys.argv[1])
report.parent.mkdir(parents=True, exist_ok=True)
prior = report.read_text() if report.exists() else ""
report.write_text(prior + "report for the packet\\n")
subprocess.run(["git", "add", "-A"], check=True)
staged = subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0
if staged:
    trailer = sys.argv[2] if len(sys.argv) > 2 else "Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>"
    subprocess.run(
        ["git", "-c", "user.email=p@t", "-c", "user.name=p", "commit", "-q",
         "-m", "packet work\\n\\n" + trailer],
        check=True,
    )
print(json.dumps({"sessionId": "sess-packet-1"}))
"""

FAKE_AGENT_SOURCE = None

# A gate that fails every round with the same tail: the held attempt's evidence.
FAILING_GATES = [
    {
        "name": "fail",
        "argv": [sys.executable, "-c", "import sys; print('boom: test_worker_retry'); sys.exit(3)"],
    }
]


class FakeCommitAdapter(packet.Adapter):
    name = "fake_commit"

    def __init__(self, report_name):
        self.report_name = report_name

    def _argv(self, prompt):
        told = re.findall(r"`(Co-Authored-By: [^`]+)`", prompt)
        return [sys.executable, str(FAKE_AGENT_SOURCE), self.report_name, *told[:1]]

    def first(self, prompt):
        return self._argv(prompt)

    def resume(self, session_id, prompt):
        return self._argv(prompt)

    def session_id(self, native_jsonl):
        return packet._first_match(native_jsonl, r'"sessionId"\s*:\s*"([^"]+)"')


def make_packet_task(tid="gm1-packet", lanes=("build-lane",), gates=None):
    return dict(
        id=tid,
        category="packet",
        brief="grid/briefs/packet-gm1.txt",
        inputs=["grid/briefs/packet-gm1.txt"],
        tests=[],
        artifacts=["docs/reports/" + tid + ".md"],
        lanes=list(lanes),
        author_family=None,
        budget={"wall_seconds": 700, "output_bytes": 10000000, "thinking_tokens": None},
        state="ready",
        blocked_reason=None,
        # J4's different-family retry is exercised elsewhere; turning it off keeps this
        # world's rows about M1 alone.
        failover=False,
        spec={
            "brief": "grid/briefs/packet-gm1.txt",
            "packet_id": "A1",
            "gates": gates if gates is not None else FAILING_GATES,
            "base": "main",
            "max_rounds": 2,
        },
    )


def ready_record(now, provider):
    return dict(
        provider=provider,
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


def lane(family, model, kind, categories, tier=None):
    entry = {
        "provider": "opencode",
        "family": family,
        "model": model,
        "kind": kind,
        "credential_path": None,
        "executable": "/usr/bin/true",
        "plan_units": {},
        "window": None,
        "max_concurrency": 1,
        "wall_seconds": 700,
        "categories": list(categories),
    }
    if tier is not None:
        entry["tier"] = tier
    return entry


def serve_packet_md(monkeypatch, world, source_name):
    """The plan lane's process seam: the attempt completes with the chosen packet.md."""
    source = world["tmp"] / source_name

    def fake_execute(ledger, aid, generation):
        row = ledger.start(aid, generation)
        if row is None:
            return "duplicate_or_stale"
        spec = row["spec"]
        directory = Path(spec["workspace"]) / aid
        staged = Path(spec["input_root"])
        (directory / "inputs").mkdir(parents=True, exist_ok=True)
        (directory / "artifacts").mkdir(parents=True, exist_ok=True)
        for name in spec["inputs"]:
            (directory / "inputs" / name).write_bytes((staged / name).read_bytes())
        data = source.read_bytes()
        (directory / "artifacts" / "packet.md").write_bytes(data)
        ledger.finish(
            aid,
            generation,
            {
                "status": "completed",
                "finish_reason": "stop",
                "actual_model": spec["model"],
                "manifest_sha256": spec["manifest_sha256"],
                "artifacts": [{"path": "packet.md", "sha256": hashlib.sha256(data).hexdigest()}],
            },
        )
        return "completed"

    monkeypatch.setattr(runner, "execute", fake_execute)


@pytest.fixture
def world(tmp_path, monkeypatch):
    global FAKE_AGENT_SOURCE
    project = tmp_path / "project"
    board = project / "grid/board"
    briefs = project / "grid/briefs"
    (project / "src/pkg").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "docs/reports").mkdir(parents=True)
    (project / "src/pkg/worker.py").write_text('"""The worker."""\n\ndef run(): return 1\n')
    (project / "tests/test_worker_retry.py").write_text("from pkg.worker import run\n")
    briefs.mkdir(parents=True)
    board.mkdir(parents=True)
    (briefs / "packet-gm1.txt").write_text(BRIEF_DOC)
    subprocess.run(["git", "-C", str(project), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.email=p@t",
            "-c",
            "user.name=p",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )
    (board / "gm1-packet.json").write_text(
        json.dumps(packet_task.validate_board_task(make_packet_task()), indent=1) + "\n"
    )
    agent = tmp_path / "fake_agent.py"
    agent.write_text(FAKE_AGENT)
    FAKE_AGENT_SOURCE = agent
    (tmp_path / "packet.md").write_text(PACKET_MD)
    monkeypatch.setattr(
        packet_task,
        "packet_adapter",
        lambda kind, lane, work, attempt_dir, session_name: FakeCommitAdapter(
            "docs/reports/gm1-packet.md"
        ),
    )
    monkeypatch.setattr(
        packet_task, "build_sandbox", lambda kind, work, attempt_dir: lambda argv: argv
    )
    url = os.environ.get("GRID_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger = Ledger(url)
    ledger.initialize()
    build_account = "goat-" + uuid.uuid4().hex[:8]
    plan_account = "go-" + uuid.uuid4().hex[:8]
    ledger.configure_account(
        build_account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    ledger.configure_account(
        plan_account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    # The plan lane declares `plan` only, so route's defaulting never offers it a packet
    # (a go_http lane cannot run one) and J4 finds no different-family packet lane.
    lanes = {
        "build-lane": lane("glm", "glm-5.3-flash", "goat_cli", ["packet"]),
        "plan-lane": lane("glm", "glm-5.3-flash", "go_http", ["plan"], tier="plan"),
    }
    ledger.record_lane("build-lane", ready_record(time.time(), "build-lane"))
    ledger.record_lane("plan-lane", ready_record(time.time(), "plan-lane"))
    lanes_path = tmp_path / "lanes.json"
    lanes_path.write_text(json.dumps({"lanes": lanes}))
    world = dict(
        project=project,
        tmp=tmp_path,
        board=board,
        ledger=ledger,
        lanes=lanes,
        lanes_path=lanes_path,
        accounts={"build-lane": build_account, "plan-lane": plan_account},
        build_account=build_account,
        plan_account=plan_account,
        packets=tmp_path / "packets",
    )
    serve_packet_md(monkeypatch, world, "packet.md")
    return world


def tick(world, **kwargs):
    return runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        world["lanes"],
        world["lanes_path"],
        world["accounts"],
        world["packets"],
        **kwargs,
    )


def board_task(world, task_id):
    return json.loads((world["board"] / (task_id + ".json")).read_text())


def plan_tasks(world):
    return sorted(p.stem for p in world["board"].glob("plan-fix-*.json"))


def row_for(results, task_id):
    matches = [r for r in results if r["task"] == task_id]
    assert len(matches) == 1, results
    return matches[0]


def release_holds(world):
    """Resolve every held attempt so its account slot frees for the next dispatch."""
    for row in world["ledger"].status():
        if row["state"] == "held":
            world["ledger"].resolve(row["id"], "consumed", "test cleanup", "test")


# --- the eligibility rule ---


def test_fix_reason_reads_the_verdict_and_refuses_the_operators_work():
    # The loop's own bounds and the idle watchdog the operator owes nothing for.
    for reason in ("wall_deadline", "agent_idle", "rounds_exhausted", "agent_stopped_early"):
        assert fix_reason({"reason": reason}) == reason
    # A verdict-less hold (the adapter raised before the loop wrote one) reads the texts.
    assert fix_reason({}, "packet loop: rounds_exhausted", "attempt x held") == "rounds_exhausted"
    # The longer reason wins its own name.
    assert fix_reason({"reason": "wall_deadline_before_round"}) == "wall_deadline_before_round"
    # A gate that ended every round identically and never passed is its own label.
    repeated = {
        "reason": "no_session_to_resume",
        "rounds": [
            {"results": [{"name": "tests", "ok": False, "reason": "exited", "returncode": 1}]},
            {"results": [{"name": "tests", "ok": False, "reason": "exited", "returncode": 1}]},
        ],
    }
    assert same_gate_repeated(repeated)
    assert fix_reason(repeated) == "same_gate_repeated"
    # A transport refusal, and nothing else, is the last door.
    assert fix_reason({}, "packet attempt: TimeoutError: the read timed out") == "transport_refused"
    # A reason the operator does owe for drafts nothing.
    assert fix_reason({"reason": "gates_passed"}) is None
    assert fix_reason({}, "review rejected: 3 finding(s)") is None
    # One round is not "every round": a single-round verdict never proves repetition.
    assert not same_gate_repeated(
        {
            "rounds": [
                {"results": [{"name": "tests", "ok": False, "reason": "exited", "returncode": 1}]}
            ]
        }
    )


def test_plan_lane_selection_and_the_deterministic_id():
    lanes = {
        "a": lane("glm", "glm-5.3-flash", "go_http", ["plan"], tier="plan"),
        "b": lane("glm", "glm-5.3-flash", "go_http", ["build"], tier="build"),
        "c": lane("glm", "glm-5.3-flash", "go_http", ["packet"]),
        "d": lane("glm", "glm-5.3-flash", "go_http", ["plan"], tier="plan"),
    }
    assert plan_lanes(lanes) == ["a", "d"]
    assert plan_lanes({}) == []
    # The id is a pure function of the pair, and legal for a board task.
    first = fix_plan_id("gm1-packet", "rounds_exhausted")
    assert first == fix_plan_id("gm1-packet", "rounds_exhausted")
    assert first != fix_plan_id("gm1-packet", "wall_deadline")
    assert first.startswith("plan-fix-") and len(first) <= 60
    assert re.fullmatch(r"[a-z0-9-]+", first)


# --- the settlement drafts, the pass loop recovers ---


def test_a_held_packet_drafts_exactly_one_plan_with_the_verdict_in_its_ticket(world):
    results = tick(world)
    row = row_for(results, "gm1-packet")
    assert row["result"] == "held"
    plan_id = fix_plan_id("gm1-packet", "rounds_exhausted")
    assert row["fix"] == plan_id
    assert plan_tasks(world) == [plan_id]

    task = board_task(world, plan_id)
    assert task["category"] == "plan" and task["state"] == "ready"
    assert task["lanes"] == ["plan-lane"] and task["artifacts"] == ["packet.md"]
    assert task["spec"] == {"base": "main", "paths": ["src/pkg/worker.py"]}

    ticket = (world["project"] / task["brief"]).read_text()
    # The verdict, in the ticket: the reason, the attempt, the packet's own section,
    # the last round's gate tail, the transcript tail and the question.
    assert "the operator owes nothing for: rounds_exhausted" in ticket
    assert "held: packet loop rounds_exhausted" in ticket
    assert "ledger reason: packet loop: rounds_exhausted" in ticket
    assert "#### A1. Test packet" in ticket and "`src/pkg/worker.py`" in ticket
    assert "boom: test_worker_retry" in ticket and "### fail — failed" in ticket
    assert "sess-packet-1" in ticket
    assert "What change to the packet, the gates or the harness would let this land?" in ticket
    # The plan lane's brief carries the hard rules and the ask, so packet_node could run it.
    assert "## 1. Hard rules" in ticket and "Write exactly one file, `packet.md`" in ticket

    # A second tick over the same blocked task drafts nothing: the plan's id is its record.
    tick(world)
    assert plan_tasks(world) == [plan_id]
    assert board_task(world, plan_id)["state"] == "passed"


def test_a_second_identical_hold_drafts_nothing(world):
    tick(world)
    plan_id = fix_plan_id("gm1-packet", "rounds_exhausted")
    assert plan_tasks(world) == [plan_id]

    # The operator re-runs the packet; it holds again for the same reason.
    task = board_task(world, "gm1-packet")
    (world["board"] / "gm1-packet.json").write_text(
        json.dumps(dict(task, state="ready", blocked_reason=None), indent=1) + "\n"
    )
    release_holds(world)
    results = tick(world)
    assert row_for(results, "gm1-packet")["result"] == "held"
    assert "fix" not in row_for(results, "gm1-packet")
    assert plan_tasks(world) == [plan_id]
    assert board_task(world, "gm1-packet")["state"] == "blocked"
    assert "rounds_exhausted" in board_task(world, "gm1-packet")["blocked_reason"]


def test_a_draft_the_settlement_never_wrote_is_recovered_when_a_plan_lane_appears(world):
    # No plan lane at failure time: the hold owes a fix but nothing can draft it yet.
    del world["lanes"]["plan-lane"]
    world["lanes_path"].write_text(json.dumps({"lanes": world["lanes"]}))
    results = tick(world)
    assert row_for(results, "gm1-packet")["result"] == "held"
    assert plan_tasks(world) == []

    # The operator configures one; the dry run names the draft and writes nothing.
    world["lanes"]["plan-lane"] = lane("glm", "glm-5.3-flash", "go_http", ["plan"], tier="plan")
    world["lanes_path"].write_text(json.dumps({"lanes": world["lanes"]}))
    plan_id = fix_plan_id("gm1-packet", "rounds_exhausted")
    dry = tick(world, dry_run=True)
    assert [r["reason"] for r in dry["plan"] if r["task"] == "gm1-packet"] == [
        "fix pending: " + plan_id
    ]
    assert plan_tasks(world) == []

    results = tick(world)
    assert row_for(results, "gm1-packet")["result"] == "fix: " + plan_id
    assert plan_tasks(world) == [plan_id]


def test_auto_dispatch_gates_the_build_of_the_drafted_packet(world):
    tick(world)
    plan_id = fix_plan_id("gm1-packet", "rounds_exhausted")
    # The plan lane drafts packet-k1; the plan node turns the block into a real packet
    # task and holds it back.
    assert row_for(tick(world), plan_id)["result"] == "passed"
    drafted = board_task(world, "packet-k1")
    assert drafted["state"] == "ready" and drafted["lanes"] == ["build-lane"]
    assert plan_task.load_drafts(world["board"]) == ["packet-k1"]

    # Without the flag the board reports the draft and dispatches nothing.
    release_holds(world)
    results = tick(world)
    assert row_for(results, "packet-k1") == {
        "task": "packet-k1",
        "lane": None,
        "attempt": None,
        "result": "draft",
    }
    assert board_task(world, "packet-k1")["state"] == "ready"

    # With it, the draft is released to build and its own gates run (and fail, as the
    # packet does) — the gate opened.
    results = tick(world, auto_dispatch=True)
    assert row_for(results, "packet-k1")["result"] == "held"
    assert board_task(world, "packet-k1")["state"] == "blocked"


def test_a_packet_that_already_moved_on_drafts_nothing(world):
    # A superseded predecessor is closed work: the plan-node answer to it is the
    # successor's, and M1 must not draft a fix for a task the board already replaced.
    task = board_task(world, "gm1-packet")
    task["state"] = "blocked"
    task["blocked_reason"] = "superseded: gm1-packet-2 — failover glm -> deepseek"
    (world["board"] / "gm1-packet.json").write_text(json.dumps(task, indent=1) + "\n")
    results = tick(world)
    assert [r for r in results if str(r["result"]).startswith("fix")] == []
    assert plan_tasks(world) == []
