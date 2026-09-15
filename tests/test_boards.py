"""The local Boards data node: one read-only row per task, legible without the id.

Everything here is offline: a temp board, a temp ledger and a fake attempt directory.
No lane runs, nothing is dispatched and no quota is spent — the planning call is the
board runner's dry run.
"""

import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path

from inference_grid.boards import boards, brief_identity, plan_reason
from inference_grid.capacity import clean_boards, project
from inference_grid.digest import digest
from inference_grid.ledger import Ledger, digest as ledger_digest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deployments" / "capacity"

# lanes.json's validator wants exactly this key set; the record's tier is absent by design
# (lanes/config.py is provider-authored and cannot carry it), so every lane is a workhorse.
LANE = {
    "provider": "opencode",
    "family": "glm",
    "model": "glm-5.3-flash",
    "kind": "go_http",
    "credential_path": None,
    "executable": None,
    "plan_units": {"five_hour": 1, "weekly": 2},
    "window": None,
    "max_concurrency": 1,
    "wall_seconds": 600,
    "categories": ["packet"],
}


def packet_task(task_id, state, packet_id="X1", **overrides):
    task = {
        "id": task_id,
        "category": "packet",
        "brief": f"grid/briefs/{task_id}.txt",
        "inputs": [f"grid/briefs/{task_id}.txt"],
        "tests": [],
        "artifacts": [f"docs/reports/{task_id}.md"],
        "lanes": ["go"],
        "author_family": None,
        "budget": {"wall_seconds": 600, "output_bytes": 1000, "thinking_tokens": None},
        "state": state,
        "blocked_reason": None,
        "spec": {
            "brief": f"grid/briefs/{task_id}.txt",
            "packet_id": packet_id,
            "gates": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
            "base": "main",
            "max_rounds": 3,
        },
    }
    task.update(overrides)
    return task


def write_brief(project, task_id, text):
    brief = project / "grid/briefs" / f"{task_id}.txt"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(text)
    return brief


def write_task(board, task):
    (board / (task["id"] + ".json")).write_text(json.dumps(task))


def make_ledger(tmp_path):
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    ledger.configure_account(
        "go", 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    ledger.record_lane(
        "go",
        {
            "provider": "go",
            "auth": "ok",
            "quota_observed_at": time.time(),
            "quota_freshness_seconds": 900,
            "used_percent_max": 10.0,
            "admission_limit_percent": 80,
            "cooldown_until": None,
            "qualification": "qualified",
            "blocked_until": None,
            "blocker": None,
        },
    )
    return ledger


def live_attempt(ledger, packets_root, task_id):
    """A fake live attempt for a dispatched packet task: an ACTIVE ledger row, a gates dir."""
    workspace = packets_root / task_id / "20260915T000000" / "attempts"
    spec = {
        "authorized": True,
        "model": "glm-5.3-flash",
        "family": "glm",
        "argv": ["/usr/bin/true"],
        "workspace": str(workspace),
        "timeout": 600,
        "output_bytes": 1000,
        "inputs": {},
        "manifest_sha256": ledger_digest({}),
    }
    ledger.submit(task_id + "-attempt", "proj", spec)
    aid, generation = ledger.claim(task_id + "-attempt", "go", {"five_hour": 0.01, "weekly": 0.01})
    ledger.start(aid, generation)
    attempt = workspace / aid
    gates = attempt / "gates-1"
    gates.mkdir(parents=True)
    (gates / "gate-tests.log").write_text("1 passed in 0.01s\n")
    return attempt


def fixture(tmp_path):
    project = tmp_path / "myproject"
    board = project / "grid/board"
    board.mkdir(parents=True)
    packets = tmp_path / "packets"

    ready = packet_task("packet-x1", "ready", packet_id="X1")
    write_brief(
        project,
        "packet-x1",
        "## 1. Hard rules\n\nrules here\n\n---\n\n"
        "#### X1. Some title\nThe first sentence states the work. A second sentence follows.\n",
    )
    write_task(board, ready)
    (project / "docs/reports").mkdir(parents=True)
    (project / "docs/reports/packet-x1.md").write_text("# report\n")

    # A ready task whose brief carries no packet heading: the id is the title.
    plain = {
        "id": "plain-task",
        "category": "pure_function",
        "brief": "grid/briefs/plain.txt",
        "inputs": ["grid/briefs/plain.txt"],
        "tests": [],
        "artifacts": ["out.py"],
        "lanes": ["go"],
        "author_family": None,
        "budget": {"wall_seconds": 60, "output_bytes": 1000, "thinking_tokens": None},
        "state": "ready",
        "blocked_reason": None,
    }
    write_brief(project, "plain", "Just a brief without a heading. Second sentence here.\n")
    write_task(board, plain)

    dispatched = packet_task("packet-live", "dispatched", packet_id="X2")
    write_brief(project, "packet-live", "#### X2. A live packet\nIt is running now.\n")
    write_task(board, dispatched)

    blocked = {
        "id": "blocked-task",
        "category": "pure_function",
        "brief": "grid/briefs/plain.txt",
        "inputs": ["grid/briefs/plain.txt"],
        "tests": [],
        "artifacts": ["out.py"],
        "lanes": ["go"],
        "author_family": None,
        "budget": {"wall_seconds": 60, "output_bytes": 1000, "thinking_tokens": None},
        "state": "blocked",
        "blocked_reason": "resolve with evidence: the operator must decide the boundary",
    }
    write_task(board, blocked)

    landed = packet_task(
        "packet-done",
        "landed",
        packet_id="X3",
        landed={"base_head": "a" * 40, "merge_commit": "b" * 40, "how": "ff"},
    )
    write_brief(project, "packet-done", "#### X3. A landed packet\nIt is merged.\n")
    write_task(board, landed)

    lanes = tmp_path / "lanes.json"
    lanes.write_text(json.dumps({"lanes": {"go": LANE}}))
    config_dir = tmp_path / "boards"
    config_dir.mkdir()
    (config_dir / "myproject.json").write_text(
        json.dumps(
            {
                "board_dir": str(board),
                "project_root": str(project),
                "lanes_path": str(lanes),
            }
        )
    )
    ledger = make_ledger(tmp_path)
    attempt = live_attempt(ledger, packets, "packet-live")
    return {
        "project": project,
        "board": board,
        "packets": packets,
        "config_dir": config_dir,
        "ledger": ledger,
        "attempt": attempt,
    }


def test_snapshot_lists_one_task_in_each_state(tmp_path):
    world = fixture(tmp_path)
    result = boards(
        world["ledger"],
        boards_dir=world["config_dir"],
        packets_root=world["packets"],
        now=time.time(),
    )
    assert len(result) == 1
    board = result[0]
    assert board["name"] == "myproject"

    planned = {row["id"]: row for row in board["planned"]}
    assert set(planned) == {"packet-x1", "plain-task"}
    x1 = planned["packet-x1"]
    assert x1["project"] == "myproject"
    assert x1["lane"] == "go" and x1["reason"] is None
    assert x1["title"] == "Some title"
    assert x1["focus"] == "The first sentence states the work."
    assert x1["links"]["brief"].endswith("grid/briefs/packet-x1.txt")
    assert x1["links"]["report"].endswith("docs/reports/packet-x1.md")
    assert x1["age"] is not None
    # The headless task reports its id as the title.
    assert planned["plain-task"]["title"] == "plain-task"
    assert planned["plain-task"]["reason"] in ("no_ready_lane", "quota stale", "account busy")

    assert len(board["active"]) == 1
    active = board["active"][0]
    assert active["id"] == "packet-live"
    assert active["lane"] == "go" and active["model"] == "glm-5.3-flash"
    assert (active["round"], active["max_rounds"]) == (1, 3)
    assert active["gate"]["name"] == "tests" and "1 passed" in active["gate"]["tail"]
    assert active["minutes"] is not None
    assert active["links"]["attempt"] == str(world["attempt"])

    assert len(board["blocked"]) == 1
    blocked = board["blocked"][0]
    assert blocked["id"] == "blocked-task"
    assert blocked["operator_owed"] is True
    assert blocked["reason"].startswith("resolve with evidence")

    assert [row["id"] for row in board["landed_today"]] == ["packet-done"]
    assert board["landed_today"][0]["state"] == "landed"


def test_snapshot_is_read_only(tmp_path):
    world = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in world["board"].glob("*.json")}
    boards(world["ledger"], boards_dir=world["config_dir"], packets_root=world["packets"])
    assert {p.name: p.read_bytes() for p in world["board"].glob("*.json")} == before


def test_brief_identity_reports_the_heading_and_its_first_sentence(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("#### X1. Some title\nFirst sentence here. Second one after.\n\nmore text\n")
    title, focus, section = brief_identity(brief, "X1", "packet-x1")
    assert title == "Some title"
    assert focus == "First sentence here."
    assert section.startswith("#### X1. Some title")
    # No packet heading: the id is the title, and the brief's first sentence the focus.
    plain = tmp_path / "plain.txt"
    plain.write_text("A brief with no heading. Its second sentence.\n")
    assert brief_identity(plain, None, "plain-task")[:2] == (
        "plain-task",
        "A brief with no heading.",
    )
    # An unreadable brief is the id, with nothing invented.
    assert brief_identity(tmp_path / "absent.txt", None, "gone") == ("gone", None, None)


def test_plan_reason_names_the_three_refusals():
    assert plan_reason({"lane": "go", "reason": "selected"}, {}) is None
    assert plan_reason(
        {"lane": None, "reason": "lane_busy", "candidates": [{"lane": "go"}]}, {}
    ) == ("account busy")
    assert (
        plan_reason(
            {"lane": None, "reason": "no_ready_lane", "candidates": [{"lane": "go"}]},
            {"go": {"state": "stale"}},
        )
        == "quota stale"
    )
    assert (
        plan_reason({"lane": None, "reason": "budget_unfit", "candidates": [], "dropped": []}, {})
        == "no_lane_for_category"
    )
    # A reason the dashboard does not name is carried through as route reported it.
    assert (
        plan_reason(
            {"lane": None, "reason": "budget_unfit", "candidates": [], "dropped": [{"lane": "go"}]},
            {},
        )
        == "budget_unfit"
    )


def test_digest_lists_a_board_once(tmp_path):
    world = fixture(tmp_path)
    # A second config file for the same board (one project, two configs): the digest must
    # report the project once.
    (world["config_dir"] / "duplicate.json").write_text(
        json.dumps(
            {
                "board_dir": str(world["board"]),
                "project_root": str(world["project"]),
                "priority": 5,
            }
        )
    )
    text = digest(world["ledger"], world["config_dir"])
    assert text.count("## Board myproject") == 1


def test_project_carries_and_sanitizes_boards():
    rows = [
        {
            "name": "myproject",
            "planned": [
                {
                    "project": "myproject",
                    "id": "packet-x1",
                    "title": "Some title",
                    "focus": "First sentence.",
                    "section": "#### X1. Some title",
                    "lane": "go",
                    "reason": None,
                    "age": 12.0,
                    "secret": "DO_NOT_SHARE",
                }
            ],
            "active": [],
            "blocked": [
                {
                    "project": "myproject",
                    "id": "t",
                    "reason": "blocked",
                    "operator_owed": True,
                    "age": 1.0,
                }
            ],
            "landed_today": [],
        }
    ]
    out = project({}, {"boards": rows})
    board = out["boards"][0]
    assert board["name"] == "myproject"
    planned = board["planned"][0]
    assert planned["title"] == "Some title" and planned["age"] == 12.0
    assert "secret" not in planned
    assert board["blocked"][0]["operator_owed"] is True
    # Malformed sections invent nothing.
    assert clean_boards("junk") == []
    assert clean_boards([{"planned": []}]) == []
    assert project({}, {})["boards"] == []


def test_the_dashboard_renders_the_boards_section():
    root = REPO / "src" / "inference_grid" / "capacity_web"
    js = (root / "app.js").read_text()
    html = (root / "index.html").read_text()
    assert "/api/boards" in js
    assert "title" in js
    assert "All work" in js
    for marker in ('id="boards"', 'id="boardcards"', 'id="allwork"', 'id="boarddrawer"'):
        assert marker in html, marker


def _load_cloud_server():
    sys.path.insert(0, str(DEPLOY))
    spec = importlib.util.spec_from_file_location(
        "grid_cloud_server_" + uuid.uuid4().hex[:6], DEPLOY / "cloud_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clean_snapshot_refuses_boards():
    cloud = _load_cloud_server()
    from datetime import datetime, timezone

    sample = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "accounts": [],
        "boards": [{"name": "myproject", "planned": [{"id": "packet-x1"}]}],
    }
    try:
        cloud.clean_snapshot(sample)
    except ValueError as exc:
        assert "boards" in str(exc)
    else:  # pragma: no cover - the refusal is the point
        raise AssertionError("clean_snapshot accepted a local-only boards section")
