"""Failover on failure, not race (brief J4): the one different-family retry.

A packet that exhausts its rounds on one family is authored exactly one successor task
whose lanes name only other families; the predecessor is superseded and a `failover`
ledger event names the swap. The fake agent and adapter seams are the packet task tests';
no test opens the real sandbox or network.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid

import pytest

from inference_grid.board import packet_task, runner
from inference_grid.lanes import packet
from inference_grid.ledger import Ledger
from sqlalchemy import select

from inference_grid.ledger import events as ledger_events

BRIEF_DOC = """# Test brief

## 1. Hard rules

- never read secrets
- never push

---

## Phase A

#### A1. Test packet
Write `docs/reports/<task-id>.md`.
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


FAKE_AGENT_SOURCE = None


FAILING_GATES = [{"name": "fail", "argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}]
PASSING_GATES = [{"name": "ok", "argv": [sys.executable, "-c", "print('ok')"]}]


def make_packet_task(tid="j4-packet", lanes=("glm-lane",), gates=None, **task_extra):
    spec = {
        "brief": "grid/briefs/packet-j4.txt",
        "packet_id": "A1",
        "gates": gates if gates is not None else FAILING_GATES,
        "base": "main",
        "max_rounds": 2,
    }
    return dict(
        id=tid,
        category="packet",
        brief="grid/briefs/packet-j4.txt",
        inputs=["grid/briefs/packet-j4.txt"],
        tests=[],
        artifacts=["docs/reports/" + tid + ".md"],
        lanes=list(lanes),
        author_family=None,
        budget={"wall_seconds": 700, "output_bytes": 10000000, "thinking_tokens": None},
        state="ready",
        blocked_reason=None,
        spec=spec,
        **task_extra,
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


def lane(family, model, kind):
    return {
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
        "categories": ["packet"],
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    global FAKE_AGENT_SOURCE
    project = tmp_path / "project"
    board = project / "grid/board"
    briefs = project / "grid/briefs"
    briefs.mkdir(parents=True)
    board.mkdir(parents=True)
    (briefs / "packet-j4.txt").write_text(BRIEF_DOC)
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
    (board / "j4-packet.json").write_text(
        json.dumps(packet_task.validate_board_task(make_packet_task()), indent=1) + "\n"
    )
    agent = tmp_path / "fake_agent.py"
    agent.write_text(FAKE_AGENT)
    FAKE_AGENT_SOURCE = agent
    monkeypatch.setattr(
        packet_task,
        "packet_adapter",
        lambda kind, lane, work, attempt_dir, session_name: FakeCommitAdapter(
            "docs/reports/j4-packet.md"
        ),
    )
    monkeypatch.setattr(
        packet_task, "build_sandbox", lambda kind, work, attempt_dir: lambda argv: argv
    )
    url = os.environ.get("GRID_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger = Ledger(url)
    ledger.initialize()
    glm_account = "goat-" + uuid.uuid4().hex[:8]
    ds_account = "zcode-" + uuid.uuid4().hex[:8]
    ledger.configure_account(
        glm_account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    ledger.configure_account(
        ds_account,
        1,
        {"five_hour": 10, "weekly": 20},
        time.time() + 600,
        ["deepseek-v4.1-flash"],
    )
    lanes = {
        "glm-lane": lane("glm", "glm-5.3-flash", "goat_cli"),
        "ds-lane": lane("deepseek", "deepseek-v4.1-flash", "zcode_cli"),
    }
    ledger.record_lane("glm-lane", ready_record(time.time(), "glm-lane"))
    ledger.record_lane("ds-lane", ready_record(time.time(), "ds-lane"))
    lanes_path = tmp_path / "lanes.json"
    lanes_path.write_text(json.dumps({"lanes": lanes}))
    return dict(
        project=project,
        board=board,
        ledger=ledger,
        lanes=lanes,
        lanes_path=lanes_path,
        accounts={"glm-lane": glm_account, "ds-lane": ds_account},
        glm_account=glm_account,
        ds_account=ds_account,
        packets=tmp_path / "packets",
    )


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


def failover_events(world):
    with world["ledger"].engine.connect() as con:
        return [
            dict(r)
            for r in con.execute(
                select(ledger_events).where(ledger_events.c.kind == "failover")
            ).mappings()
        ]


def write_task(world, task_id, task):
    (world["board"] / (task_id + ".json")).write_text(
        json.dumps(packet_task.validate_board_task(task), indent=1) + "\n"
    )


def test_a_held_glm_packet_reappears_once_on_deepseek(world):
    results = tick(world)
    assert results[0]["result"] == "held" and results[0]["failover"] == "j4-packet-2"
    predecessor = board_task(world, "j4-packet")
    assert predecessor["state"] == "blocked"
    assert predecessor["blocked_reason"].startswith(
        "superseded: j4-packet-2 — failover glm -> deepseek"
    )
    successor = board_task(world, "j4-packet-2")
    assert successor["state"] == "ready"
    assert successor["lanes"] == ["ds-lane"]
    assert successor["failover"] is True and successor["failover_from"] == "j4-packet"
    assert successor["spec"]["gates"] == FAILING_GATES
    assert (world["project"] / "grid/briefs/j4-packet-2.txt").read_text() == BRIEF_DOC
    (event,) = failover_events(world)
    assert event["detail"] == {
        "from_task": "j4-packet",
        "to_task": "j4-packet-2",
        "from_family": "glm",
        "to_families": ["deepseek"],
    }
    # The failover attempt itself: gates fixed on the retry (the operator's recorded
    # change), it runs on the deepseek account and passes.
    successor["spec"]["gates"] = PASSING_GATES
    write_task(world, "j4-packet-2", successor)
    results = tick(world)
    assert [r["result"] for r in results] == ["passed"]
    assert board_task(world, "j4-packet-2")["state"] == "passed"
    completed = [r for r in world["ledger"].status() if r["state"] == "completed"]
    assert len(completed) == 1 and completed[0]["account"] == world["ds_account"]


def test_the_reverse_held_deepseek_packet_reappears_on_glm(world):
    write_task(world, "j4-packet", make_packet_task(lanes=("ds-lane",)))
    results = tick(world)
    assert results[0]["failover"] == "j4-packet-2"
    successor = board_task(world, "j4-packet-2")
    assert successor["lanes"] == ["glm-lane"]
    predecessor = board_task(world, "j4-packet")
    assert predecessor["blocked_reason"].startswith(
        "superseded: j4-packet-2 — failover deepseek -> glm"
    )
    assert failover_events(world)[0]["detail"]["from_family"] == "deepseek"
    assert failover_events(world)[0]["detail"]["to_families"] == ["glm"]


def test_a_failover_task_never_fails_over_again(world):
    tick(world)
    results = tick(world)
    assert results[0]["result"] == "held" and "failover" not in results[0]
    assert board_task(world, "j4-packet-2")["state"] == "blocked"
    assert "rounds_exhausted" in board_task(world, "j4-packet-2")["blocked_reason"]
    assert not (world["board"] / "j4-packet-3.json").exists()
    assert len(failover_events(world)) == 1
    # And no second attempt was ever authored on the failed family by this path.
    assert board_task(world, "j4-packet-2")["lanes"] == ["ds-lane"]


def test_failover_false_stays_put(world):
    write_task(world, "j4-packet", make_packet_task(failover=False))
    results = tick(world)
    assert results[0]["result"] == "held" and "failover" not in results[0]
    assert board_task(world, "j4-packet")["state"] == "blocked"
    assert "rounds_exhausted" in board_task(world, "j4-packet")["blocked_reason"]
    assert not (world["board"] / "j4-packet-2.json").exists()
    assert failover_events(world) == []


def test_the_dry_run_reports_the_pending_failover(world):
    # No different-family lane configured when the packet fails: it stays blocked and
    # eligible, and the dry run names the swap the next real tick would author.
    del world["lanes"]["ds-lane"]
    results = tick(world)
    assert results[0]["result"] == "held" and "failover" not in results[0]
    assert board_task(world, "j4-packet")["state"] == "blocked"
    world["lanes"]["ds-lane"] = lane("deepseek", "deepseek-v4.1-flash", "zcode_cli")
    world["lanes_path"].write_text(json.dumps({"lanes": world["lanes"]}))
    plan = tick(world, dry_run=True)
    assert [r["reason"] for r in plan["plan"]] == ["failover pending: glm -> deepseek"]
    assert plan["plan"][0]["task"] == "j4-packet" and plan["plan"][0]["lane"] is None
    assert not (world["board"] / "j4-packet-2.json").exists()
    # The real tick authors it: the pending failover resolves.
    results = tick(world)
    assert results[0]["result"] == "failover: j4-packet-2"
    assert board_task(world, "j4-packet-2")["lanes"] == ["ds-lane"]


def test_packet_failover_keys_validate():
    out = packet_task.validate_board_task(make_packet_task())
    assert out["failover"] is True and "failover_from" not in out
    off = packet_task.validate_board_task(make_packet_task(failover=False))
    assert off["failover"] is False
    linked = packet_task.validate_board_task(make_packet_task(failover_from="j4-packet"))
    assert linked["failover_from"] == "j4-packet"
    for bad in ({"failover": "yes"}, {"failover": 1}, {"failover_from": ""}, {"failover_from": 7}):
        with pytest.raises(ValueError):
            packet_task.validate_board_task(make_packet_task(**bad))
