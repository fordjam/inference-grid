"""The landing code node: verify, union-merge, gates on the merged tree, `landed`.

Every branch is authored through a scratch worktree so the project checkout — the
"operator checkout" of these tests — is never checked out to anything but its own
branch, and the assertions can hold it to that.
"""

import json
import shutil
import subprocess
import sys
import pytest
from sqlalchemy import select

from inference_grid.board import packet_task
from inference_grid.board.land import land_cli, land_packet
from inference_grid.ledger import Ledger, events

CONTRIBUTIONS = """## 2026-09-15 — brief 14

| Item | Evidence |
| --- | --- |
| A1 | alpha |
"""

OK_GATE = {"name": "ok", "argv": [sys.executable, "-c", "print('ok')"]}
FAIL_GATE = {"name": "no", "argv": [sys.executable, "-c", "raise SystemExit(3)"]}


def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert proc.returncode == 0 or not check, proc.stderr
    return proc.stdout.strip()


def make_task(tid, brief, state="passed", **spec_extra):
    spec = {
        "brief": brief,
        "packet_id": "A1",
        "gates": [OK_GATE],
        "base": "glm/work",
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
        state=state,
        blocked_reason=None,
        spec=spec,
    )


def write_task(board, task):
    validated = packet_task.validate_board_task(task)
    (board / (task["id"] + ".json")).write_text(json.dumps(validated, indent=1) + "\n")
    return validated


def commit_branch(project, name, start, edits, message):
    """Author `name` at `start` with the given {relpath: text} edits; returns the head.

    The work happens in a detached scratch worktree and the branch ref is force-moved,
    so this works for a brand-new packet branch and for advancing the base alike, and
    no checked-out worktree ever changes.
    """
    wt = project.parent / ("wt-" + name.replace("/", "-"))
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "-q", "--detach", str(wt), start],
        check=True,
        capture_output=True,
    )
    try:
        for rel, content in edits.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(wt, "add", "-A")
        git(
            wt,
            "-c",
            "user.email=p@t",
            "-c",
            "user.name=p",
            "commit",
            "-q",
            "-m",
            message,
        )
        head = git(wt, "rev-parse", "HEAD")
        git(project, "branch", "-f", name, head)
        return head
    finally:
        subprocess.run(
            ["git", "-C", str(project), "worktree", "remove", "--force", str(wt)],
            check=True,
            capture_output=True,
        )
        shutil.rmtree(wt, ignore_errors=True)


@pytest.fixture
def world(tmp_path):
    project = tmp_path / "project"
    board = project / "grid/board"
    board.mkdir(parents=True)
    (project / "docs").mkdir(parents=True)
    (project / "mod.py").write_text("VALUE = 1\n")
    (project / "docs/CONTRIBUTIONS.md").write_text(CONTRIBUTIONS)
    (project / "docs/LANES.md").write_text("# Lane reference\n")
    (project / "grid/briefs").mkdir(parents=True)
    (project / "grid/briefs/packet-t1.txt").write_text(
        "# Brief\n\n## 1. Hard rules\n\n- none\n\n---\n\n#### A1. Test packet\n"
    )
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "-c", "user.email=p@t", "-c", "user.name=p", "commit", "-q", "-m", "base")
    # The base branch is never checked out anywhere: landings go through the scratch
    # worktree land.py claims and removes.
    git(project, "branch", "glm/work")
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    write_task(board, make_task("t1", "grid/briefs/packet-t1.txt"))
    return dict(
        project=project,
        board=board,
        ledger=ledger,
        packets=tmp_path / "packets",
        base_head=git(project, "rev-parse", "glm/work"),
        root_head=git(project, "rev-parse", "HEAD"),
        status_before=git(project, "status", "--porcelain"),
    )


def land(world, task_id, dry_run=False, gates=None):
    task = json.loads((world["board"] / (task_id + ".json")).read_text())
    return land_packet(
        world["board"],
        world["project"],
        task_id,
        task["spec"]["base"],
        gates if gates is not None else task["spec"]["gates"],
        world["ledger"],
        world["packets"],
        dry_run=dry_run,
    )


def landed_events(ledger):
    with ledger.tx() as con:
        return [
            dict(r) for r in con.execute(select(events).where(events.c.kind == "landed")).mappings()
        ]


def test_clean_fast_forward_lands(world):
    head = commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"docs/reports/t1.md": "report\n"},
        "packet t1",
    )
    report = land(world, "t1")
    assert report["landed"] is True and report["how"] == "ff"
    assert report["merge_commit"] == head
    assert report["base_head"] == world["base_head"]
    task = json.loads((world["board"] / "t1.json").read_text())
    assert task["state"] == "landed" and task["landed"]["how"] == "ff"
    assert task["landed"]["merge_commit"] == head
    events_seen = landed_events(world["ledger"])
    assert [e["detail"]["task"] for e in events_seen] == ["t1"]
    # The base advanced to the branch; the operator checkout and the worktree list
    # are exactly as they were.
    assert git(world["project"], "rev-parse", "glm/work") == head
    assert git(world["project"], "rev-parse", "HEAD") == world["root_head"]
    assert git(world["project"], "status", "--porcelain") == world["status_before"]
    assert len(git(world["project"], "worktree", "list").splitlines()) == 1


def test_contributions_conflict_lands_by_union(world):
    # The base moves on after the branch was cut: both sides append their own row to
    # the same ledger file. Rows are independent, so the union keeps both.
    advanced = commit_branch(
        world["project"],
        "glm/work",
        world["base_head"],
        {"docs/CONTRIBUTIONS.md": CONTRIBUTIONS + "| Z9 | zulu |\n"},
        "base row",
    )
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {
            "docs/CONTRIBUTIONS.md": CONTRIBUTIONS + "| B2 | beta |\n",
            "docs/reports/t1.md": "report\n",
        },
        "packet t1",
    )
    write_task(world["board"], make_task("t1", "grid/briefs/packet-t1.txt"))
    report = land(world, "t1")
    assert report["landed"] is True and report["how"] == "union"
    assert git(world["project"], "rev-parse", "glm/work") == report["merge_commit"]
    assert report["base_head"] == advanced
    merged = subprocess.run(
        ["git", "-C", str(world["project"]), "show", "glm/work:docs/CONTRIBUTIONS.md"],
        capture_output=True,
        text=True,
    ).stdout
    assert "| Z9 | zulu |" in merged and "| B2 | beta |" in merged
    assert json.loads((world["board"] / "t1.json").read_text())["state"] == "landed"


def test_source_conflict_blocks_with_the_file_named(world):
    advanced = commit_branch(
        world["project"],
        "glm/work",
        world["base_head"],
        {"mod.py": "VALUE = 2\n"},
        "base change",
    )
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"mod.py": "VALUE = 3\n", "docs/reports/t1.md": "report\n"},
        "packet t1",
    )
    write_task(world["board"], make_task("t1", "grid/briefs/packet-t1.txt"))
    report = land(world, "t1")
    assert report["landed"] is False and report["blocked"] is True
    assert "mod.py" in report["reason"]
    task = json.loads((world["board"] / "t1.json").read_text())
    assert task["state"] == "blocked" and "mod.py" in task["blocked_reason"]
    # The base sits exactly where the test's own commit left it, its worktrees and the
    # operator checkout are untouched.
    assert git(world["project"], "rev-parse", "glm/work") == advanced
    assert len(git(world["project"], "worktree", "list").splitlines()) == 1


def test_gate_failure_on_the_merged_tree_blocks(world):
    # A union conflict makes verify_merge skip its gates (nothing mergeable to run them
    # on), so the landing run on the resolved tree is the proof that fails.
    advanced = commit_branch(
        world["project"],
        "glm/work",
        world["base_head"],
        {"docs/CONTRIBUTIONS.md": CONTRIBUTIONS + "| Z9 | zulu |\n"},
        "base row",
    )
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {
            "docs/CONTRIBUTIONS.md": CONTRIBUTIONS + "| B2 | beta |\n",
            "docs/reports/t1.md": "report\n",
        },
        "packet t1",
    )
    write_task(world["board"], make_task("t1", "grid/briefs/packet-t1.txt"))
    report = land(world, "t1", gates=[FAIL_GATE])
    assert report["landed"] is False and "gate no failed" in report["reason"]
    assert git(world["project"], "rev-parse", "glm/work") == advanced
    assert git(world["project"], "rev-parse", "HEAD") == world["root_head"]
    # The clean-merge shape fails the same way, one step earlier, at the verify.
    write_task(world["board"], make_task("t1", "grid/briefs/packet-t1.txt"))
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"docs/reports/t1.md": "report\n"},
        "packet t1",
    )
    report = land(world, "t1", gates=[FAIL_GATE])
    assert report["landed"] is False and "gate no failed" in report["reason"]
    assert git(world["project"], "rev-parse", "glm/work") == advanced


def test_the_lock_serializes_landings(world):
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"docs/reports/t1.md": "report\n"},
        "packet t1",
    )
    lock = world["packets"] / "land-glm-work.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("999")
    report = land(world, "t1")
    assert report["landed"] is False and "busy" in report["reason"]
    task = json.loads((world["board"] / "t1.json").read_text())
    assert task["state"] == "passed"  # a busy report is transient, not a block
    assert git(world["project"], "rev-parse", "glm/work") == world["base_head"]
    shutil.rmtree(lock)
    assert land(world, "t1")["landed"] is True


def test_dry_run_reports_what_would_land_without_touching_anything(world):
    head = commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"docs/reports/t1.md": "report\n"},
        "packet t1",
    )
    report = land(world, "t1", dry_run=True)
    assert report["dry_run"] is True and report["would_land"] is True
    assert report["how"] == "ff" and report["branch_head"] == head
    task = json.loads((world["board"] / "t1.json").read_text())
    assert task["state"] == "passed" and "landed" not in task
    assert git(world["project"], "rev-parse", "glm/work") == world["base_head"]
    assert landed_events(world["ledger"]) == []
    # And why not, when it would not.
    write_task(world["board"], make_task("t1", "grid/briefs/packet-t1.txt"))
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"mod.py": "VALUE = 3\n", "docs/reports/t1.md": "report\n"},
        "packet t1 conflict",
    )
    commit_branch(
        world["project"],
        "glm/work",
        world["base_head"],
        {"mod.py": "VALUE = 2\n"},
        "base change",
    )
    report = land(world, "t1", dry_run=True)
    assert report["would_land"] is False and "mod.py" in report["reason"]


def test_landed_packet_shape_round_trips_validation():
    task = make_task("t1", "grid/briefs/packet-t1.txt")
    task["state"] = "landed"
    task["landed"] = {
        "base_head": "a" * 40,
        "merge_commit": "b" * 40,
        "how": "union",
    }
    validated = packet_task.validate_board_task(task)
    assert validated["landed"]["how"] == "union"
    for broken in (
        {"base_head": "a" * 40, "merge_commit": "nope", "how": "ff"},
        {"base_head": "a" * 40, "merge_commit": "b" * 40, "how": "rebase"},
        {"base_head": "a" * 40, "merge_commit": "b" * 40},
    ):
        task["landed"] = broken
        with pytest.raises(ValueError):
            packet_task.validate_board_task(task)


def test_a_task_that_is_not_passed_is_not_landed(world):
    write_task(world["board"], make_task("t1", "grid/briefs/packet-t1.txt", state="ready"))
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"docs/reports/t1.md": "report\n"},
        "packet t1",
    )
    report = land(world, "t1")
    assert report["landed"] is False and "not passed" in report["reason"]
    assert json.loads((world["board"] / "t1.json").read_text())["state"] == "ready"


def test_tick_auto_land_lands_passed_packets(world, tmp_path):
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"docs/reports/t1.md": "report\n"},
        "packet t1",
    )
    write_task(world["board"], make_task("t1", "grid/briefs/packet-t1.txt"))
    lanes_path = tmp_path / "lanes.json"
    lanes_path.write_text(json.dumps({"lanes": {}}))
    from inference_grid.board.runner import tick

    plan = tick(
        world["board"],
        world["project"],
        world["ledger"],
        {},
        lanes_path,
        {},
        world["packets"],
        auto_land=True,
        dry_run=True,
    )
    row = next(r for r in plan["plan"] if r["task"] == "t1")
    assert row["reason"] == "auto_land: would land (ff)"
    assert row["land"]["would_land"] is True
    assert git(world["project"], "rev-parse", "glm/work") == world["base_head"]

    results = tick(
        world["board"],
        world["project"],
        world["ledger"],
        {},
        lanes_path,
        {},
        world["packets"],
        auto_land=True,
    )
    assert [r["result"] for r in results] == ["landed"]
    assert json.loads((world["board"] / "t1.json").read_text())["state"] == "landed"
    # A second tick has nothing left to land.
    assert (
        tick(
            world["board"],
            world["project"],
            world["ledger"],
            {},
            lanes_path,
            {},
            world["packets"],
            auto_land=True,
        )
        == []
    )


def test_the_cli_entry_lands(world, tmp_path):
    commit_branch(
        world["project"],
        "packet/t1",
        world["base_head"],
        {"docs/reports/t1.md": "report\n"},
        "packet t1",
    )
    report = land_cli(
        {
            "board_dir": str(world["board"]),
            "project_root": str(world["project"]),
            "task": "t1",
            "base": "glm/work",
            "gates": [OK_GATE],
            "packets_root": str(world["packets"]),
        },
        world["ledger"],
    )
    assert report["landed"] is True and report["how"] == "ff"
    assert json.loads((world["board"] / "t1.json").read_text())["state"] == "landed"
    assert [e["detail"]["task"] for e in landed_events(world["ledger"])] == ["t1"]
