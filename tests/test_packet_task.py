"""Packet board tasks: spec validation, the dispatched build→gate→re-enter loop, and
what the tick records from it.

The fake agent is a script that writes the report file, commits with the trailer and
prints a session id — enough for build_loop to run its rounds against fake gates. The
sandbox and adapter seams are monkeypatched; no test opens the real sandbox or network.
"""

import json
import re
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from inference_grid.board import packet_task, runner
from inference_grid.lanes import packet
from inference_grid.ledger import Ledger

TRAILER = "Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>"

BRIEF_DOC = """# Test brief

## 1. Hard rules

- never read secrets
- never push

---

## Phase A

#### A1. Test packet
Write `docs/reports/<task-id>.md`.
"""

# The fake agent: one more line in the report and one trailered commit per round, session
# id on stdout. A re-entry round changes the worktree, so a failing gate still exhausts the
# rounds rather than reading as the agent having stopped early.
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


class FakeCommitAdapter(packet.Adapter):
    """Stands in for the lane's CLI adapter; argv shape is the test's own business."""

    name = "fake_commit"

    def __init__(self, report_name):
        self.report_name = report_name

    def _argv(self, prompt):
        # The agent commits with whatever trailer the prompt told it to use, as a real
        # lane does: the gate and the prompt must name the same one.
        told = re.findall(r"`(Co-Authored-By: [^`]+)`", prompt)
        return [sys.executable, str(Path(FAKE_AGENT_SOURCE)), self.report_name, *told[:1]]

    def first(self, prompt):
        return self._argv(prompt)

    def resume(self, session_id, prompt):
        return self._argv(prompt)

    def session_id(self, native_jsonl):
        return packet._first_match(native_jsonl, r'"sessionId"\s*:\s*"([^"]+)"')


FAKE_AGENT_SOURCE = None  # set by the fixture to the written agent script path


def make_packet_task(tid="d1-packet", brief="grid/briefs/packet-d1.txt", **spec_extra):
    spec = {
        "brief": brief,
        "packet_id": "A1",
        "gates": [{"name": "ok", "argv": [sys.executable, "-c", "print('ok')"]}],
        "base": "main",
    }
    spec.update(spec_extra)
    return dict(
        id=tid,
        category="packet",
        brief=brief,
        inputs=[brief],
        tests=[],
        artifacts=["docs/reports/" + tid + ".md"],
        lanes=["packet-cli"],
        author_family=None,
        budget={"wall_seconds": 60, "output_bytes": 10000000, "thinking_tokens": None},
        state="ready",
        blocked_reason=None,
        spec=spec,
    )


def gate(name, argv):
    return {"name": name, "argv": argv}


def test_validate_board_task_routes_by_category():
    plain = dict(make_packet_task("plain"))
    plain.pop("spec")
    plain["category"] = "pure_function"
    assert packet_task.validate_board_task(plain)["category"] == "pure_function"
    validated = packet_task.validate_board_task(make_packet_task())
    assert validated["spec"]["max_rounds"] == 3  # the default, filled in
    assert validated["spec"]["packet_id"] == "A1"


def test_validation_rejects_bad_packet_specs():
    cases = [
        ("missing spec key", lambda t: t.pop("spec")),
        ("unknown spec key", lambda t: t["spec"].update(extra=1)),
        ("missing required key", lambda t: t["spec"].pop("base")),
        ("brief mismatch", lambda t: t["spec"].update(brief="grid/briefs/other.txt")),
        ("bad packet id", lambda t: t["spec"].update(packet_id="a1")),
        ("packet id not a heading", lambda t: t["spec"].update(packet_id="PHASE")),
        ("base escapes", lambda t: t["spec"].update(base="../elsewhere")),
        ("base is a flag", lambda t: t["spec"].update(base="--upload-pack=x")),
        ("base with a space", lambda t: t["spec"].update(base="main branch")),
        ("zero rounds", lambda t: t["spec"].update(max_rounds=0)),
        ("rounds as bool", lambda t: t["spec"].update(max_rounds=True)),
        ("empty gates", lambda t: t["spec"].update(gates=[])),
        ("gate without argv", lambda t: t["spec"].update(gates=[{"name": "x"}])),
        (
            "gate timeout unbounded",
            lambda t: t["spec"].update(
                gates=[gate("x", ["a"]), {"name": "y", "argv": ["b"], "timeout": 0}]
            ),
        ),
    ]
    for why, break_it in cases:
        task = make_packet_task()
        break_it(task)
        with pytest.raises(ValueError):
            packet_task.validate_board_task(task)


def test_cline_cli_runs_packets_as_fresh_sessions(tmp_path):
    from inference_grid.lanes.packet import ClineAdapter

    assert "cline_cli" in packet_task.PACKET_KINDS and not packet_task.UNSUPPORTED_ADAPTERS
    lane = {"model": "cline-pass/deepseek-v4.1-flash", "executable": "/x/cline"}
    adapter = packet_task.packet_adapter("cline_cli", lane, tmp_path, tmp_path, "s")
    assert isinstance(adapter, ClineAdapter)
    # no --id: a later round is a fresh session on the fix prompt
    assert adapter.resume("s1", "fix")[:2] == ["/x/cline", "fix"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    global FAKE_AGENT_SOURCE
    project = tmp_path / "project"
    board = project / "grid/board"
    briefs = project / "grid/briefs"
    briefs.mkdir(parents=True)
    board.mkdir(parents=True)
    (project / "mod.py").write_text("VALUE = 1\n")
    (briefs / "packet-d1.txt").write_text(BRIEF_DOC)
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
    (board / "d1-packet.json").write_text(
        json.dumps(packet_task.validate_board_task(make_packet_task()), indent=1) + "\n"
    )
    agent = tmp_path / "fake_agent.py"
    agent.write_text(FAKE_AGENT)
    FAKE_AGENT_SOURCE = str(agent)
    monkeypatch.setattr(
        packet_task,
        "packet_adapter",
        lambda kind, lane, work, attempt_dir, session_name: FakeCommitAdapter(
            "docs/reports/d1-packet.md"
        ),
    )
    monkeypatch.setattr(
        packet_task, "build_sandbox", lambda kind, work, attempt_dir: lambda argv: argv
    )
    url = os.environ.get("GRID_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger = Ledger(url)
    ledger.initialize()
    account = "goat-" + uuid.uuid4().hex[:8]
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    ledger.record_lane("packet-cli", ready_record(time.time()))
    lanes = {
        "packet-cli": {
            "provider": "opencode",
            "family": "glm",
            "model": "glm-5.3-flash",
            "kind": "goat_cli",
            "credential_path": None,
            "executable": "/usr/bin/true",
            "plan_units": {},
            "window": None,
            "max_concurrency": 1,
            "wall_seconds": 60,
            "categories": ["packet"],
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


def ready_record(now, provider="packet-cli"):
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


def git(project, *args):
    proc = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def tick(world, **kwargs):
    return runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        world["lanes"],
        world["lanes_path"],
        {"packet-cli": world["account"], "cline-lane": world["account"]},
        world["packets"],
        **kwargs,
    )


def test_tick_runs_the_packet_loop_and_fetches_the_branch(world):
    head_before = git(world["project"], "rev-parse", "HEAD")
    base_before = git(world["project"], "rev-parse", "main")
    status_before = git(world["project"], "status", "--porcelain")
    results = tick(world)
    assert [r["result"] for r in results] == ["passed"]
    task = json.loads((world["board"] / "d1-packet.json").read_text())
    assert task["state"] == "passed" and task["blocked_reason"] is None
    # The ledger attempt: opened before the loop (the loop's files live inside its
    # workspace lease), completed from the verdict.
    rows = [r for r in world["ledger"].status() if r["state"] == "completed"]
    assert len(rows) == 1
    attempt_dir = next(iter((world["packets"]).glob("*/*/attempts/" + rows[0]["id"])))
    assert (attempt_dir / "native-1.jsonl").is_file()
    receipt = rows[0]["receipt"]
    assert receipt["verified_in_lane"] is True and receipt["repairs"] == 0
    assert receipt["artifacts"][0]["path"] == "packet/d1-packet"
    assert len(receipt["artifacts"][0]["sha256"]) == 64
    # The branch is in the coordinator's repo; the base and the checkout are untouched.
    # The clone itself is gone — the receipt pins the head, and a landed clone is the
    # working tree that filled the disk.
    branch_head = git(world["project"], "rev-parse", "packet/d1-packet")
    assert branch_head == receipt["head"]
    assert not (attempt_dir / "work").exists()
    assert git(world["project"], "rev-parse", "main") == base_before
    assert git(world["project"], "rev-parse", "HEAD") == head_before
    # The fetch wrote refs only: the working tree and index are exactly as they were.
    assert git(world["project"], "status", "--porcelain") == status_before
    card = {
        (e["category"], e["accepted"], e["attempts"])
        for e in world["ledger"].scorecard(account=world["account"])
    }
    assert card == {("packet", 1, 1)}


def test_a_short_volume_holds_the_attempt_before_any_clone(world, monkeypatch):
    # The guard counts the checkout, not just the floor: 5 GiB free is enough for the
    # floor alone, but this repo's tree plus the floor is more than the volume holds.
    tree = packet_task.checkout_bytes(world["project"], "main")
    assert tree > 0
    floor = 5 * 1024**3
    original = packet_task.run_packet
    monkeypatch.setattr(
        packet_task,
        "run_packet",
        lambda ledger, admission: original(
            ledger, admission, min_free_bytes=floor, free_bytes=lambda path: floor + tree - 1
        ),
    )
    monkeypatch.setattr(runner, "run_packet", packet_task.run_packet)
    results = tick(world)
    assert [r["result"] for r in results] == ["held"]
    held = [r for r in world["ledger"].status() if r["state"] == "held"]
    assert len(held) == 1 and held[0]["reason"].startswith("packet attempt: disk_low:")
    attempt_dir = next(iter((world["packets"]).glob("*/*/attempts/" + held[0]["id"])))
    assert not (attempt_dir / "work").exists()
    assert (
        subprocess.run(
            ["git", "-C", str(world["project"]), "rev-parse", "--verify", "-q", "packet/d1-packet"],
            capture_output=True,
        ).returncode
        != 0
    )


def test_a_failing_gate_holds_the_attempt_and_leaves_the_branch(world):
    task_path = world["board"] / "d1-packet.json"
    raw = json.loads(task_path.read_text())
    raw["spec"]["gates"] = [gate("fail", [sys.executable, "-c", "import sys; sys.exit(3)"])]
    raw["spec"]["max_rounds"] = 2
    raw["budget"]["wall_seconds"] = 700  # room for the re-entry round
    task_path.write_text(json.dumps(raw, indent=1) + "\n")
    results = tick(world)
    assert [r["result"] for r in results] == ["held"]
    task = json.loads(task_path.read_text())
    assert task["state"] == "blocked"
    assert "packet loop rounds_exhausted" in task["blocked_reason"]
    held = [r for r in world["ledger"].status() if r["state"] == "held"]
    assert len(held) == 1 and held[0]["reason"] == "packet loop: rounds_exhausted"
    attempt_dir = next(iter((world["packets"]).glob("*/*/attempts/" + held[0]["id"])))
    # The branch survives inside the attempt's clone; the coordinator's repo got no ref.
    assert git(attempt_dir / "work", "rev-parse", "--abbrev-ref", "HEAD") == "packet/d1-packet"
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(world["project"]),
                "rev-parse",
                "--verify",
                "--quiet",
                "packet/d1-packet",
            ],
            capture_output=True,
        ).returncode
        != 0
    )


def test_an_unresolvable_base_refuses_before_any_attempt(world):
    task_path = world["board"] / "d1-packet.json"
    raw = json.loads(task_path.read_text())
    raw["spec"]["base"] = "no-such-branch"
    task_path.write_text(json.dumps(raw, indent=1) + "\n")
    results = tick(world)
    assert results[0]["result"].startswith("refused: packet base not found")
    task = json.loads(task_path.read_text())
    assert task["state"] == "ready"
    assert [r for r in world["ledger"].status() if r["account"] == world["account"]] == []


def test_an_unsupported_lane_kind_refuses_and_touches_nothing(world):
    # go_http is a one-request kind with no agent to re-enter; it never runs packets.
    world["lanes"]["cline-lane"] = dict(world["lanes"]["packet-cli"], kind="go_http")
    world["ledger"].record_lane("cline-lane", ready_record(time.time(), provider="cline-lane"))
    task_path = world["board"] / "d1-packet.json"
    raw = json.loads(task_path.read_text())
    raw["lanes"] = ["cline-lane"]
    task_path.write_text(json.dumps(raw, indent=1) + "\n")
    results = tick(world)
    assert "go_http" in results[0]["result"] and "does not run packet" in results[0]["result"]
    task = json.loads(task_path.read_text())
    assert task["state"] == "ready"
    assert [r for r in world["ledger"].status() if r["account"] == world["account"]] == []


def test_dry_run_plans_the_packet_task(world):
    plan = tick(world, dry_run=True)
    # route (brief 14 C1) adds candidates/dropped/score to every plan row; the packet task
    # is planned like any other task.
    assert len(plan["plan"]) == 1
    row = plan["plan"][0]
    assert {k: row[k] for k in ("task", "lane", "reason")} == {
        "task": "d1-packet",
        "lane": "packet-cli",
        "reason": "selected",
    }
    assert row["candidates"] == [{"lane": "packet-cli", "cap": 16000}] and row["dropped"] == []
    task = json.loads((world["board"] / "d1-packet.json").read_text())
    assert task["state"] == "ready"
    assert world["ledger"].status() == [] or all(
        r["account"] != world["account"] for r in world["ledger"].status()
    )


def test_the_commit_gate_and_the_prompt_carry_the_lanes_own_trailer(world):
    """A DeepSeek lane's packet is attributed to DeepSeek — the gate demanded the GLM
    trailer for every lane, so DeepSeek's landed work was signed GLM."""
    lane = world["lanes"]["packet-cli"]
    lane.update(family="deepseek", model="cline-pass/deepseek-v4.1-flash", kind="cline_cli")
    world["lanes_path"].write_text(json.dumps({"lanes": world["lanes"]}))
    world["ledger"].configure_account(
        world["account"],
        1,
        {"five_hour": 10, "weekly": 20},
        time.time() + 600,
        ["cline-pass/deepseek-v4.1-flash"],
    )
    results = tick(world)
    assert [r["result"] for r in results] == ["passed"]
    message = git(world["project"], "log", "-1", "--format=%B", "packet/d1-packet")
    assert "Co-Authored-By: Deepseek-V4.1-Flash <noreply@deepseek.com>" in message
    assert "GLM" not in message


def test_a_pending_clone_elsewhere_counts_against_the_guard(world, monkeypatch, tmp_path):
    # Two boards dispatch in the same second: the first has claimed its clone's bytes but
    # not written them yet, so the volume still reports them free. The second must count
    # that claim — on 2026-09-16 two vix-rs clones each passed against the same 15 GiB.
    tree = packet_task.checkout_bytes(world["project"], "main")
    floor = 5 * 1024**3
    # Enough for the floor and exactly one checkout — and one is already pending.
    reported_free = floor + tree
    packets_root = world["packets"]
    packet_task._reserve(packets_root, "other-attempt", tree)
    original = packet_task.run_packet
    monkeypatch.setattr(
        packet_task,
        "run_packet",
        lambda ledger, admission: original(
            ledger, admission, min_free_bytes=floor, free_bytes=lambda path: reported_free
        ),
    )
    monkeypatch.setattr(runner, "run_packet", packet_task.run_packet)
    results = tick(world)
    assert [r["result"] for r in results] == ["held"]
    held = [r for r in world["ledger"].status() if r["state"] == "held"]
    assert held[0]["reason"].startswith("packet attempt: disk_low:")
    assert "after other pending clones" in held[0]["reason"]
    # The held attempt left no reservation of its own; the other one is untouched.
    assert sorted(p.name for p in (packets_root / ".reservations").iterdir()) == ["other-attempt"]
    # A reservation older than the TTL is a dead clone and no longer counts.
    stale = packets_root / ".reservations" / "other-attempt"
    old = time.time() - packet_task.RESERVATION_TTL_SECONDS - 5
    os.utime(stale, (old, old))
    assert packet_task.reserved_bytes(packets_root) == 0


def test_a_completed_clone_leaves_no_reservation_behind(world):
    tick(world)
    reservations = world["packets"] / ".reservations"
    assert not reservations.exists() or list(reservations.iterdir()) == []
