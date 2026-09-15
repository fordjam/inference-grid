"""The plan node: board-new drafts a packet from a ticket, the runner settles it.

All offline: the lane process seam (`worker.execute`) is faked so an attempt completes
with a `packet.md` the test controls, while staging, admission and settlement run exactly
as production runs them. No test opens the real sandbox or the network.
"""

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from inference_grid import cli
from inference_grid.board import plan_task, runner
from inference_grid.board.plan_task import guess_paths, validate_plan_task
from inference_grid.lanes.brief import hard_rules, packet_text
from inference_grid.ledger import Ledger

TICKET = {
    "title": "Retry budget on the worker",
    "body": "The worker gives up on the first transport error; it should retry twice.",
    "paths": ["src/pkg/worker.py"],
}

PACKET_VALID = """#### K1. Retry budget on the worker

Location: `src/pkg/worker.py`.
Acceptance tests: `tests/test_worker_retry.py`.
Size: small. Operator step: merge the packet branch.

```json
{"gates": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
 "tests": ["tests/test_worker_retry.py"]}
```
"""

# A syntactically broken fenced block: nothing can be read from it.
PACKET_UNPARSEABLE = """#### K1. Retry budget on the worker

```json
{"gates": [{"name": "tests", "argv": ["python"]}], "tests": ["tests/x.py",}
```
"""

# A readable block the packet validator refuses (a gate without a name).
PACKET_BAD_GATE = """#### K1. Retry budget on the worker

```json
{"gates": [{"argv": ["python"]}], "tests": []}
```
"""


def ready_record(now, provider="plan-lane"):
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


def serve_packet_md(monkeypatch, world, source_name):
    """Fake the OS-process seam: the attempt completes with the chosen packet.md.

    `worker.execute` is the one place a lane process runs; the sandbox this suite is
    developed in refuses the process-group cleanup the real worker does after every
    attempt (killpg on an exited group, then `/bin/ps`), which fails every dispatching
    test in the base for the same reason. The seam is faked here so staging, admission
    and settlement run exactly as production runs them, with the lane's answer supplied.
    """
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
    project = tmp_path / "project"
    (project / "src/pkg").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "docs/reports").mkdir(parents=True)
    (project / "src/pkg/worker.py").write_text('"""The worker."""\n\ndef run(): return 1\n')
    (project / "tests/test_worker_retry.py").write_text("from pkg.worker import run\n")
    (project / "docs/reports/glm-2026-09-14.md").write_text("old report\n")
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
            "worker retry budget",
        ],
        check=True,
    )
    for name, text in (
        ("packet-valid.md", PACKET_VALID),
        ("packet-unparseable.md", PACKET_UNPARSEABLE),
        ("packet-bad-gate.md", PACKET_BAD_GATE),
    ):
        (tmp_path / name).write_text(text)
    board = project / "grid/board"
    created = plan_task.plan_task(board, dict(TICKET, repo=str(project)), "plan-lane")

    url = os.environ.get("GRID_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger = Ledger(url)
    ledger.initialize()
    account = "plan-" + uuid.uuid4().hex[:8]
    now = time.time()
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, now + 600, ["glm-5.3-flash"]
    )
    ledger.record_lane("plan-lane", ready_record(now))
    lanes = {
        "plan-lane": {
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
            "categories": ["plan", "packet"],
            "tier": "plan",
        }
    }
    lanes_path = tmp_path / "lanes.json"
    lanes_path.write_text(json.dumps({"lanes": lanes}))
    world = dict(
        project=project,
        tmp=tmp_path,
        board=board,
        ledger=ledger,
        lanes=lanes,
        lanes_path=lanes_path,
        account=account,
        packets=tmp_path / "packets",
        plan_id=created["id"],
    )
    serve_packet_md(monkeypatch, world, "packet-valid.md")
    return world


def tick(world, **kwargs):
    return runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        world["lanes"],
        world["lanes_path"],
        {"plan-lane": world["account"]},
        world["packets"],
        **kwargs,
    )


def plan_file(world):
    return json.loads((world["board"] / (world["plan_id"] + ".json")).read_text())


def attempts(world):
    return [r for r in world["ledger"].status() if r["account"] == world["account"]]


def test_the_scout_section_reaches_the_plan_prompt(world):
    results = tick(world)
    assert [r["result"] for r in results] == ["passed"]
    staged = list(world["packets"].glob("*/*/attempts/*/inputs/brief.txt"))
    assert len(staged) == 1
    prompt = staged[0].read_text()
    # The scout's facts: the named file, its commit and its pinning test.
    assert "## Orientation (generated by the scout at " in prompt
    assert "`src/pkg/worker.py` — exists" in prompt
    assert "worker retry budget" in prompt
    assert "`tests/test_worker_retry.py`" in prompt
    # ... beside the ticket and the ask.
    assert TICKET["title"] in prompt and TICKET["body"] in prompt
    assert "Write exactly one file, `packet.md`" in prompt


def test_a_fake_lanes_packet_md_becomes_a_valid_packet_task(world):
    results = tick(world)
    assert results[0]["result"] == "passed" and results[0]["drafted"] == "packet-k1"
    assert plan_file(world)["state"] == "passed"
    packet = json.loads((world["board"] / "packet-k1.json").read_text())
    assert packet["category"] == "packet" and packet["state"] == "ready"
    assert packet["id"] == "packet-k1" and packet["blocked_reason"] is None
    assert packet["brief"] == "grid/briefs/packet-k1.txt"
    assert packet["inputs"] == [packet["brief"]]
    assert packet["tests"] == ["tests/test_worker_retry.py"]
    # lanes come from route's defaulting for the packet's category, not the plan lane
    assert packet["lanes"] == ["plan-lane"]
    assert packet["spec"] == {
        "brief": "grid/briefs/packet-k1.txt",
        "packet_id": "K1",
        "gates": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
        "base": "main",
        "max_rounds": 3,
    }
    # The board loads it through the packet validator.
    assert runner.load_board(world["board"])["packet-k1"][1]["state"] == "ready"
    brief = (world["project"] / "grid/briefs/packet-k1.txt").read_text()
    assert hard_rules(brief).startswith("## 1. Hard rules")
    assert packet_text(brief, "K1").startswith("#### K1. Retry budget on the worker")
    # The draft list holds it back from dispatch.
    assert plan_task.load_drafts(world["board"]) == ["packet-k1"]
    # The plan run is recorded under its own category.
    card = {
        (e["category"], e["accepted"], e["attempts"])
        for e in world["ledger"].scorecard(account=world["account"])
    }
    assert card == {("plan", 1, 1)}


def test_a_malformed_block_holds_the_plan_task_with_the_error(world, tmp_path, monkeypatch):
    serve_packet_md(monkeypatch, world, "packet-unparseable.md")
    results = tick(world)
    assert results[0]["result"] == "blocked"
    task = plan_file(world)
    assert task["state"] == "blocked"
    assert task["blocked_reason"].startswith("plan: packet.md JSON block unreadable")
    assert not (world["board"] / "packet-k1.json").exists()
    assert plan_task.load_drafts(world["board"]) == []
    assert attempts(world)[0]["state"] == "completed"


def test_a_block_the_packet_validator_refuses_holds_the_plan_task(world, tmp_path, monkeypatch):
    serve_packet_md(monkeypatch, world, "packet-bad-gate.md")
    tick(world)
    task = plan_file(world)
    assert task["state"] == "blocked"
    # The validation error reaches the operator unmangled.
    assert "each gate needs a name and argv" in task["blocked_reason"]
    assert not (world["board"] / "packet-k1.json").exists()


def test_nothing_builds_without_the_flag(world):
    tick(world)  # the plan pass drafts packet-k1
    assert len(attempts(world)) == 1
    assert (world["board"] / "packet-k1.json").exists()

    # A later pass leaves the draft alone: the default is to draft, not to build.
    results = tick(world)
    assert results == [{"task": "packet-k1", "lane": None, "attempt": None, "result": "draft"}]
    assert len(attempts(world)) == 1
    dry = tick(world, dry_run=True)
    assert [row["reason"] for row in dry["plan"]] == ["draft"]

    # With auto_dispatch the draft is released to build.
    results = tick(world, auto_dispatch=True)
    assert "does not run packet tasks" in results[0]["result"]
    assert len(attempts(world)) == 1


def test_planning_is_refused_on_a_lane_that_is_not_tier_plan(world):
    world["lanes"]["plan-lane"]["tier"] = "build"
    results = tick(world)
    assert results[0]["result"] == "plan_requires_tier_plan: plan-lane"
    assert attempts(world) == []
    assert plan_file(world)["state"] == "ready"
    # An absent tier is build, the same refusal.
    del world["lanes"]["plan-lane"]["tier"]
    assert tick(world)[0]["result"] == "plan_requires_tier_plan: plan-lane"


def test_guess_paths_takes_a_named_path_or_greps_the_body(world):
    project = world["project"]
    assert guess_paths(project, "fix `src/pkg/worker.py` now") == ["src/pkg/worker.py"]
    # No path token: the identifiers are grepped out of the tree, never invented.
    guessed = guess_paths(project, "the worker gives up too early")
    assert {"src/pkg/worker.py", "tests/test_worker_retry.py"} <= set(guessed)


def test_validate_plan_task_refuses_a_foreign_shape(world):
    raw = json.loads((world["board"] / (world["plan_id"] + ".json")).read_text())
    for broken in (
        dict(raw, category="pure_function"),
        {k: v for k, v in raw.items() if k != "spec"},
        dict(raw, spec={"base": "../elsewhere"}),
        dict(raw, spec={"base": "main", "extra": 1}),
        dict(raw, spec={}),
    ):
        with pytest.raises(ValueError):
            validate_plan_task(broken)


def test_plan_task_refuses_existing_files(world):
    with pytest.raises(FileExistsError, match="task file already exists"):
        plan_task.plan_task(world["board"], dict(TICKET, repo=str(world["project"])), "plan-lane")


def test_board_new_from_a_ticket_authors_the_plan_task(world):
    ticket = dict(TICKET, title="A second ticket", repo=str(world["project"]))
    created = cli.board_new(world["board"], ticket=ticket, lane="plan-lane")
    assert created["id"] == "plan-a-second-ticket"
    task = json.loads((world["board"] / "plan-a-second-ticket.json").read_text())
    assert (task["category"], task["state"], task["lanes"]) == ("plan", "ready", ["plan-lane"])
    assert task["artifacts"] == ["packet.md"]
    with pytest.raises(ValueError, match="lane"):
        cli.board_new(world["board"], ticket=ticket)
