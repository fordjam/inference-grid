"""The verify-merge code node: merge proof before review. All offline, temp repos only."""

import json
import subprocess
import sys

import pytest

from inference_grid.board import runner
from inference_grid.board.packet_task import validate_board_task
from inference_grid.board.verify_merge import verify_merge, validate_verify_merge_task
from inference_grid.ledger import Ledger

BOTH_FILES = (
    "import pathlib, sys\n"
    "sys.exit(0 if pathlib.Path('feature.txt').is_file() "
    "and pathlib.Path('main.txt').is_file() else 1)\n"
)


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _init(path):
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.test")
    _git(path, "config", "user.name", "test")


def _repo(tmp_path, name="repo"):
    """main and feature diverged additively: a merge brings both files in."""
    repo = tmp_path / name
    repo.mkdir()
    _init(repo)
    (repo / "main.txt").write_text("main\n")
    _commit_all(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    _commit_all(repo, "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "other.txt").write_text("other\n")
    _commit_all(repo, "other")
    return repo


def _conflicting_repo(tmp_path):
    """feature and main both edit main.txt: the merge cannot complete."""
    repo = _repo(tmp_path, "conflict")
    _git(repo, "checkout", "-q", "feature")
    (repo / "main.txt").write_text("feature's main\n")
    _commit_all(repo, "feature touches main.txt")
    _git(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main's main\n")
    _commit_all(repo, "main touches main.txt")
    return repo


def _snapshot(repo):
    return (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "write-tree"),
        _git(repo, "status", "--porcelain"),
    )


def test_a_clean_branch_merges_and_passes_its_gate(tmp_path):
    repo = _repo(tmp_path)
    report = verify_merge(
        repo,
        "feature",
        "main",
        [{"name": "both-files", "argv": [sys.executable, "-c", BOTH_FILES]}],
        work_dir=tmp_path / "scratch",
    )
    assert report["mergeable"] is True and report["conflicts"] == []
    assert report["gates"] == [
        {"name": "both-files", "ok": True, "returncode": 0, "tail": report["gates"][0]["tail"]}
    ]
    assert report["branch_head"] == _git(repo, "rev-parse", "feature").strip()
    assert report["target_head"] == _git(repo, "rev-parse", "main").strip()
    assert isinstance(report["elapsed_s"], float)


def test_a_conflicting_branch_names_the_paths_and_skips_the_gates(tmp_path):
    repo = _conflicting_repo(tmp_path)
    report = verify_merge(
        repo,
        "feature",
        "main",
        [{"name": "both-files", "argv": [sys.executable, "-c", BOTH_FILES]}],
        work_dir=tmp_path / "scratch",
    )
    assert report["mergeable"] is False
    assert report["conflicts"] == ["main.txt"]
    assert report["gates"] == []


def test_a_merge_that_breaks_a_gate_carries_the_tail(tmp_path):
    repo = _repo(tmp_path)
    report = verify_merge(
        repo,
        "feature",
        "main",
        [
            {
                "name": "failing",
                "argv": [sys.executable, "-c", "print('gate says no'); raise SystemExit(1)"],
            }
        ],
        work_dir=tmp_path / "scratch",
    )
    assert report["mergeable"] is True
    assert report["gates"][0]["ok"] is False and report["gates"][0]["returncode"] == 1
    assert "gate says no" in report["gates"][0]["tail"]


@pytest.mark.parametrize("case", ["clean", "conflict", "gate_fail"])
def test_the_worktree_is_removed_and_the_checkout_untouched_in_every_case(tmp_path, case):
    repo = _repo(tmp_path)
    if case == "conflict":
        repo = _conflicting_repo(tmp_path)
    gates = [{"name": "both-files", "argv": [sys.executable, "-c", BOTH_FILES]}]
    if case == "gate_fail":
        gates = [{"name": "failing", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}]
    scratch = tmp_path / "scratch"
    before = _snapshot(repo)
    verify_merge(repo, "feature", "main", gates, work_dir=scratch)
    # The operator checkout's HEAD, index and status are exactly what they were.
    assert _snapshot(repo) == before
    # No scratch worktree, no registration, nothing left in the scratch dir.
    assert list(scratch.iterdir()) == []
    assert _git(repo, "worktree", "list").strip().count("\n") == 0


def make_verify_task(**overrides):
    task = dict(
        id="verify-a",
        category="verify_merge",
        brief="brief.txt",
        inputs=[],
        tests=[],
        artifacts=[],
        lanes=[],
        author_family=None,
        budget={"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None},
        state="ready",
        blocked_reason=None,
        spec={
            "branch": "feature",
            "target": "main",
            "gates": [{"name": "both-files", "argv": [sys.executable, "-c", BOTH_FILES]}],
        },
    )
    task.update(overrides)
    return task


def test_validate_board_task_accepts_verify_merge_and_keeps_empty_lists():
    task = validate_board_task(make_verify_task())
    assert task["category"] == "verify_merge"
    assert task["spec"] == make_verify_task()["spec"]
    # A code node needs no lane and produces no artifacts: empties are legal.
    assert task["inputs"] == [] and task["artifacts"] == [] and task["lanes"] == []


def test_validate_verify_merge_task_refuses_bad_specs():
    good = make_verify_task()["spec"]
    with pytest.raises(ValueError):  # missing gates
        validate_verify_merge_task(make_verify_task(spec={"branch": "a", "target": "b"}))
    with pytest.raises(ValueError):  # unknown spec key
        validate_verify_merge_task(make_verify_task(spec=dict(good, extra=1)))
    with pytest.raises(ValueError):  # escaping ref
        validate_verify_merge_task(make_verify_task(spec=dict(good, branch="../escape")))
    with pytest.raises(ValueError):  # empty gates
        validate_verify_merge_task(make_verify_task(spec=dict(good, gates=[])))
    with pytest.raises(ValueError):  # a gate with an unknown key
        validate_verify_merge_task(
            make_verify_task(spec=dict(good, gates=[{"name": "g", "argv": ["true"], "env": {}}]))
        )
    with pytest.raises(ValueError):  # a gate without argv
        validate_verify_merge_task(make_verify_task(spec=dict(good, gates=[{"name": "g"}])))


@pytest.fixture
def board_world(tmp_path):
    """A git project with the diverged branches, a board inside it, and a fresh ledger."""
    project = _repo(tmp_path, "project")
    board = project / "grid" / "board"
    board.mkdir(parents=True)
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    return dict(project=project, board=board, ledger=ledger, packets=tmp_path / "packets")


def _tick(world):
    return runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        {},
        world["board"] / "lanes.json",
        {},
        world["packets"],
    )


def test_tick_settles_a_verify_merge_task_without_a_lane(board_world):
    world = board_world
    (world["board"] / "verify-a.json").write_text(json.dumps(make_verify_task()))
    before = _snapshot(world["project"])
    results = _tick(world)
    assert results == [{"task": "verify-a", "lane": None, "attempt": None, "result": "passed"}]
    task = json.loads((world["board"] / "verify-a.json").read_text())
    assert task["state"] == "passed" and task["blocked_reason"] is None
    # No lane ran, no attempt exists, the checkout's HEAD and index are untouched.
    assert world["ledger"].status() == []
    assert _snapshot(world["project"]) == before


def test_tick_blocks_a_verify_merge_task_on_a_conflict(board_world):
    world = board_world
    task = make_verify_task(
        spec={
            "branch": "feature",
            "target": "main",
            "gates": [{"name": "both-files", "argv": [sys.executable, "-c", BOTH_FILES]}],
        }
    )
    _git(world["project"], "checkout", "-q", "feature")
    (world["project"] / "main.txt").write_text("feature's main\n")
    _commit_all(world["project"], "feature touches main.txt")
    _git(world["project"], "checkout", "-q", "main")
    (world["project"] / "main.txt").write_text("main's main\n")
    _commit_all(world["project"], "main touches main.txt")
    (world["board"] / "verify-a.json").write_text(json.dumps(task))
    results = _tick(world)
    assert results[0]["result"] == "blocked"
    task = json.loads((world["board"] / "verify-a.json").read_text())
    assert task["state"] == "blocked"
    assert "merge conflicts: main.txt" in task["blocked_reason"]
    assert world["ledger"].status() == []


def test_tick_blocks_a_verify_merge_task_whose_merge_breaks_a_gate(board_world):
    world = board_world
    task = make_verify_task()
    task["spec"]["gates"] = [
        {
            "name": "failing",
            "argv": [sys.executable, "-c", "print('gate says no'); raise SystemExit(1)"],
        }
    ]
    (world["board"] / "verify-a.json").write_text(json.dumps(task))
    results = _tick(world)
    assert results[0]["result"] == "blocked"
    task = json.loads((world["board"] / "verify-a.json").read_text())
    assert task["state"] == "blocked"
    assert "gate failing failed (exit 1)" in task["blocked_reason"]
    assert "gate says no" in task["blocked_reason"]
    assert world["ledger"].status() == []


def test_tick_dry_run_plans_a_verify_merge_task_without_running_it(board_world):
    world = board_world
    (world["board"] / "verify-a.json").write_text(json.dumps(make_verify_task()))
    plan = runner.tick(
        world["board"],
        world["project"],
        world["ledger"],
        {},
        world["board"] / "lanes.json",
        {},
        world["packets"],
        dry_run=True,
    )
    assert [{k: r[k] for k in ("task", "lane", "reason")} for r in plan["plan"]] == [
        {"task": "verify-a", "lane": None, "reason": "verify_merge"}
    ]
    task = json.loads((world["board"] / "verify-a.json").read_text())
    assert task["state"] == "ready" and task["blocked_reason"] is None


def test_the_cli_writes_out_and_exits_non_zero_when_a_gate_fails(tmp_path, monkeypatch):
    from inference_grid import cli

    repo = _repo(tmp_path)
    for gate, code in (
        ({"name": "both-files", "argv": [sys.executable, "-c", BOTH_FILES]}, 0),
        ({"name": "failing", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}, 1),
    ):
        spec = {
            "repo": str(repo),
            "branch": "feature",
            "target": "main",
            "gates": [gate],
            "out": str(tmp_path / "out.json"),
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))
        monkeypatch.setattr(
            sys, "argv", ["inference-grid", "verify-merge", "--json", str(spec_path)]
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == code
        report = json.loads((tmp_path / "out.json").read_text())
        assert report["mergeable"] is True and report["gates"][0]["ok"] == (code == 0)
