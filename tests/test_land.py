"""board/land.py: a passed packet branch lands on its base as a code node. Offline."""

import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import select

from inference_grid.board import runner
from inference_grid.board.land import land
from inference_grid.board.packet_task import validate_board_task
from inference_grid.ledger import Ledger, events

BOTH_FILES = (
    "import pathlib, sys\n"
    "sys.exit(0 if pathlib.Path('feature.txt').is_file() "
    "and pathlib.Path('main.txt').is_file() else 1)\n"
)

BRANCH_ROW_ONLY = (
    "import pathlib, sys\n"
    "text = pathlib.Path('docs/CONTRIBUTIONS.md').read_text()\n"
    "sys.exit(0 if 'feature row' in text and 'base row' not in text else 1)\n"
)


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


def _commit_all(repo, message, *paths):
    # Scoped adds: the untracked board task file must never join a commit.
    _git(repo, "add", "-A", "--", *(paths or ["."]))
    _git(repo, "commit", "-q", "-m", message)


def _init(path):
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.test")
    _git(path, "config", "user.name", "test")


def _packet_branch(tmp_path, name="project"):
    """main (main.txt) with a packet/t1 branch one commit ahead; HEAD detached off main."""
    repo = tmp_path / name
    repo.mkdir()
    _init(repo)
    (repo / "main.txt").write_text("main\n")
    _commit_all(repo, "base")
    _git(repo, "checkout", "-q", "-b", "packet/t1")
    (repo / "feature.txt").write_text("feature\n")
    _commit_all(repo, "feature")
    _git(repo, "checkout", "-q", "--detach")
    return repo


def make_packet_task(tid="t1", state="passed", gates=None, base="main"):
    return dict(
        id=tid,
        category="packet",
        brief="brief.txt",
        inputs=["brief.txt"],
        tests=[],
        artifacts=["out.txt"],
        lanes=["go"],
        author_family=None,
        budget={"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None},
        state=state,
        blocked_reason=None,
        spec={
            "brief": "brief.txt",
            "packet_id": "I1",
            "gates": gates or [{"name": "both-files", "argv": [sys.executable, "-c", BOTH_FILES]}],
            "base": base,
            "max_rounds": 3,
        },
    )


def _write_task(board, task):
    (board / (task["id"] + ".json")).write_text(json.dumps(task, indent=1) + "\n")


@pytest.fixture
def world(tmp_path):
    """Project with the packet branch, a board inside it, a packets root and a ledger."""
    project = _packet_branch(tmp_path)
    board = project / "grid" / "board"
    board.mkdir(parents=True)
    _write_task(board, make_packet_task())
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    return dict(
        project=project,
        board=board,
        ledger=ledger,
        packets=tmp_path / "packets",
        task=make_packet_task(),
    )


def _land(world, task_id="t1", **kw):
    return land(
        world["board"],
        world["project"],
        task_id,
        packets_root=world["packets"],
        ledger=world["ledger"],
        **kw,
    )


def _task_file(world, task_id="t1"):
    return json.loads((world["board"] / (task_id + ".json")).read_text())


def _base_head(world):
    return _git(world["project"], "rev-parse", "main").strip()


def _ledger_events(world):
    with world["ledger"].engine.connect() as con:
        return [dict(r) for r in con.execute(select(events)).mappings()]


def test_validate_board_task_accepts_a_landed_packet_task():
    task = make_packet_task(state="landed")
    task["landed"] = {
        "base_head": "0" * 40,
        "merge_commit": "1" * 40,
        "how": "merge",
    }
    out = validate_board_task(task)
    assert out["state"] == "landed" and out["landed"]["how"] == "merge"
    # The record and the state come together — or not at all.
    with pytest.raises(ValueError):
        validate_board_task(make_packet_task(state="landed"))
    with pytest.raises(ValueError):
        validate_board_task(dict(make_packet_task(), landed={"base_head": "0" * 40}))
    bad = dict(make_packet_task(state="landed"))
    bad["landed"] = {"base_head": "nothex", "merge_commit": "1" * 40, "how": "merge"}
    with pytest.raises(ValueError):
        validate_board_task(bad)
    bad = dict(make_packet_task(state="landed"))
    bad["landed"] = {"base_head": "0" * 40, "merge_commit": "1" * 40, "how": "rebase"}
    with pytest.raises(ValueError):
        validate_board_task(bad)


def test_a_clean_fast_forward_lands_the_branch(world):
    world_ = world
    before = _base_head(world_)
    report = _land(world_)
    branch_head = _git(world_["project"], "rev-parse", "packet/t1").strip()
    assert report["landed"] is True and report["how"] == "ff"
    assert report["base_head"] == before
    # --no-ff even on a fast-forward: a fresh merge commit whose parents are the old
    # base head and the branch head, and whose tree is exactly the branch's tree.
    merge_commit = report["merge_commit"]
    assert merge_commit != branch_head
    parents = _git(world_["project"], "rev-list", "--parents", "-n", "1", merge_commit).split()
    assert parents[1:] == [before, branch_head]
    assert _git(world_["project"], "rev-parse", merge_commit + "^{tree}") == _git(
        world_["project"], "rev-parse", branch_head + "^{tree}"
    )
    assert _base_head(world_) == merge_commit
    task = _task_file(world_)
    assert task["state"] == "landed" and task["blocked_reason"] is None
    assert task["landed"] == {"base_head": before, "merge_commit": merge_commit, "how": "ff"}
    landed = [e for e in _ledger_events(world_) if e["kind"] == "packet_landed"]
    assert len(landed) == 1 and landed[0]["detail"]["task"] == "t1"
    # The lock is released and no scratch worktree survives.
    assert not (world_["packets"] / "land-locks" / "main").exists()
    assert _git(world_["project"], "worktree", "list").strip().count("\n") == 0


def test_a_diverged_base_gets_a_true_merge(world):
    world_ = world
    _git(world_["project"], "checkout", "-q", "--detach", "main")
    (world_["project"] / "other.txt").write_text("other\n")
    _commit_all(world_["project"], "other", "other.txt")
    _git(world_["project"], "update-ref", "refs/heads/main", "HEAD")
    base_after = _base_head(world_)
    branch_head = _git(world_["project"], "rev-parse", "packet/t1").strip()
    report = _land(world_)
    assert report["landed"] is True and report["how"] == "merge"
    merge_commit = report["merge_commit"]
    assert merge_commit != base_after
    parents = _git(world_["project"], "rev-list", "--parents", "-n", "1", merge_commit).split()
    assert len(parents) == 3
    assert set(parents[1:]) == {base_after, branch_head}
    assert _base_head(world_) == merge_commit


def _diverge_on(world_, path, base_text, branch_text):
    """Both sides edit one file: the branch side, then the base side (main moves, detached)."""
    _git(world_["project"], "checkout", "-q", "packet/t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(branch_text)
    _commit_all(world_["project"], "branch side", str(path.relative_to(world_["project"])))
    _git(world_["project"], "checkout", "-q", "--detach", "main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base_text)
    _commit_all(world_["project"], "base side", str(path.relative_to(world_["project"])))
    _git(world_["project"], "update-ref", "refs/heads/main", "HEAD")


def test_a_contributions_conflict_lands_by_union(world):
    world_ = world
    docs = world_["project"] / "docs"
    docs.mkdir()
    _diverge_on(
        world_,
        docs / "CONTRIBUTIONS.md",
        "| Lane | Row |\n| --- | --- |\n| base row |\n",
        "| Lane | Row |\n| --- | --- |\n| feature row |\n",
    )
    feature_head = _git(world_["project"], "rev-parse", "packet/t1").strip()
    report = _land(world_)
    assert report["landed"] is True and report["how"] == "union"
    merged = _git(world_["project"], "show", "main:docs/CONTRIBUTIONS.md")
    assert "feature row" in merged and "base row" in merged
    assert _task_file(world_)["landed"]["how"] == "union"
    assert _git(world_["project"], "rev-parse", "packet/t1").strip() == feature_head


def test_a_source_conflict_blocks_with_the_file_named(world):
    world_ = world
    _diverge_on(world_, world_["project"] / "main.txt", "main's main\n", "feature's main\n")
    before = _base_head(world_)
    report = _land(world_)
    assert report["landed"] is False and "main.txt" in report["reason"]
    task = _task_file(world_)
    assert task["state"] == "blocked" and "main.txt" in task["blocked_reason"]
    assert _base_head(world_) == before  # the base is untouched
    assert _git(world_["project"], "worktree", "list").strip().count("\n") == 0


def test_a_gate_failing_on_the_merged_tree_blocks_and_leaves_the_base_untouched(world):
    world_ = world
    gates = [{"name": "branch-row-only", "argv": [sys.executable, "-c", BRANCH_ROW_ONLY]}]
    _write_task(world_["board"], make_packet_task(gates=gates))
    docs = world_["project"] / "docs"
    docs.mkdir()
    _diverge_on(
        world_,
        docs / "CONTRIBUTIONS.md",
        "| Lane | Row |\n| --- | --- |\n| base row |\n",
        "| Lane | Row |\n| --- | --- |\n| feature row |\n",
    )
    before = _base_head(world_)
    report = _land(world_)
    assert report["landed"] is False
    assert "gate branch-row-only failed on the merged tree" in report["reason"]
    task = _task_file(world_)
    assert task["state"] == "blocked" and "merged tree" in task["blocked_reason"]
    assert _base_head(world_) == before
    assert [e for e in _ledger_events(world_) if e["kind"] == "packet_landed"] == []


def test_a_live_lock_serializes_and_a_stale_one_is_stolen(world, tmp_path):
    world_ = world
    lock = world_["packets"] / "land-locks" / "main"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()) + "\n")  # this test process is alive
    before = _base_head(world_)
    report = _land(world_)
    assert report["landed"] is False and "lock" in report["reason"]
    assert _task_file(world_)["state"] == "passed" and _base_head(world_) == before
    # A lock whose owner is dead does not wedge the base.
    dead = subprocess.Popen(["true"])
    dead.wait()
    (lock / "pid").write_text(str(dead.pid) + "\n")
    report = _land(world_)
    assert report["landed"] is True
    assert not lock.exists()


def test_dry_run_reports_without_touching_anything(world, monkeypatch):
    world_ = world
    before = _base_head(world_)
    # The probe runs under the packets root, as the landing does, never under $TMPDIR:
    # a project's test guards may treat the temp root differently from a real path.
    from inference_grid.board import land as land_module

    seen = {}
    real = land_module.verify_merge

    def spy(*args, **kw):
        seen["work_dir"] = kw.get("work_dir")
        return real(*args, **kw)

    monkeypatch.setattr(land_module, "verify_merge", spy)
    report = _land(world_, dry_run=True)
    assert seen["work_dir"] == world_["packets"] / "t1" / "land"
    assert report["dry_run"] is True and report["would_land"] is True
    assert report["how"] == "ff" and report["landed"] is False
    assert _base_head(world_) == before
    task = _task_file(world_)
    assert task["state"] == "passed" and "landed" not in task
    assert [e for e in _ledger_events(world_) if e["kind"] == "packet_landed"] == []
    assert not (world_["packets"] / "land-locks").exists()
    assert _git(world_["project"], "worktree", "list").strip().count("\n") == 0


def test_only_a_passed_packet_task_lands(world):
    world_ = world
    _write_task(world_["board"], make_packet_task(state="ready"))
    report = _land(world_)
    assert report["landed"] is False and "ready" in report["reason"]
    assert _task_file(world_)["state"] == "ready"


def test_a_base_held_by_another_worktree_is_refused(world, tmp_path):
    world_ = world
    other = tmp_path / "other-checkout"
    _git(world_["project"], "worktree", "add", str(other), "main")
    before = _base_head(world_)
    report = _land(world_)
    assert report["landed"] is False and "checked out" in report["reason"]
    assert _base_head(world_) == before and _task_file(world_)["state"] == "passed"
    _git(world_["project"], "worktree", "remove", str(other))


def test_the_project_root_holding_a_clean_base_is_landed_and_synced(world):
    world_ = world
    _git(world_["project"], "checkout", "-q", "main")  # the board's own checkout holds the base
    report = _land(world_)
    assert report["landed"] is True
    assert (world_["project"] / "feature.txt").read_text() == "feature\n"
    tracked = _git(world_["project"], "status", "--porcelain", "--untracked-files=no")
    assert tracked == ""


def test_a_dirty_project_root_holding_the_base_is_refused(world):
    world_ = world
    _git(world_["project"], "checkout", "-q", "main")
    (world_["project"] / "main.txt").write_text("operator work\n")  # a tracked modification
    before = _base_head(world_)
    report = _land(world_)
    assert report["landed"] is False and "local changes" in report["reason"]
    assert _base_head(world_) == before and _task_file(world_)["state"] == "passed"


def test_a_dirty_board_directory_is_committed_on_the_base_and_landing_proceeds(tick_world):
    """The runner rewrites the task file on every settlement; a board kept in git is dirty
    exactly when a task has just passed. That must not stop the landing (or, worse, be
    reset to `ready` by the checkout sync and dispatched again)."""
    world_ = tick_world
    _git(world_["project"], "checkout", "-q", "main")
    _write_task(world_["board"], make_packet_task(state="ready"))
    _git(world_["project"], "add", "grid/board")
    _git(world_["project"], "commit", "-q", "-m", "board tracked")
    task = json.loads((world_["board"] / "t1.json").read_text())
    (world_["board"] / "t1.json").write_text(json.dumps(dict(task, state="passed")))
    report = land(
        world_["board"],
        world_["project"],
        "t1",
        packets_root=world_["packets"],
        ledger=world_["ledger"],
    )
    assert report["landed"] is True, report["reason"]
    log = _git(world_["project"], "log", "--format=%s", "-3")
    assert "board: state before landing t1" in log
    # The committed state is `passed`; the settlement after the merge writes `landed` on top,
    # so the only remaining tracked change is that settlement — never a revert to `ready`.
    assert _task_file(world_)["state"] == "landed"
    committed = _git(world_["project"], "show", "HEAD:grid/board/t1.json")
    assert json.loads(committed)["state"] in ("passed", "landed")


@pytest.fixture
def tick_world(tmp_path):
    """A board world like the runner's: board inside the project, packets root beside it."""
    project = _packet_branch(tmp_path)
    board = project / "grid" / "board"
    board.mkdir(parents=True)
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    return dict(project=project, board=board, ledger=ledger, packets=tmp_path / "packets")


def test_the_tick_lands_passed_packet_tasks_when_the_config_says_auto_land(tick_world):
    world_ = tick_world
    _write_task(world_["board"], make_packet_task())
    results = runner.tick(
        world_["board"],
        world_["project"],
        world_["ledger"],
        {},
        world_["board"] / "lanes.json",
        {},
        world_["packets"],
        auto_land=True,
    )
    landed = [r for r in results if r["task"] == "t1" and r["result"] == "landed"]
    assert len(landed) == 1 and landed[0]["lane"] is None
    assert _task_file(world_)["state"] == "landed"
    # The base's tree is now the branch's tree (a --no-ff merge commit on top).
    assert _git(world_["project"], "rev-parse", "main^{tree}") == _git(
        world_["project"], "rev-parse", "packet/t1^{tree}"
    )


def test_without_auto_land_a_passed_packet_task_waits(tick_world):
    world_ = tick_world
    _write_task(world_["board"], make_packet_task())
    results = runner.tick(
        world_["board"],
        world_["project"],
        world_["ledger"],
        {},
        world_["board"] / "lanes.json",
        {},
        world_["packets"],
    )
    assert [r for r in results if r["task"] == "t1"] == []
    assert _task_file(world_)["state"] == "passed"


def test_the_cli_lands_and_reports_exit_codes(world, tmp_path, monkeypatch):
    from inference_grid import cli

    spec = {
        "board_dir": str(world["board"]),
        "project_root": str(world["project"]),
        "task": "t1",
        "base": "main",
        "gates": [{"name": "both-files", "argv": [sys.executable, "-c", BOTH_FILES]}],
        "packets_root": str(world["packets"]),
    }
    url = "sqlite:///" + str(tmp_path / "cli-ledger.sqlite")
    Ledger(url).initialize()  # the operator's database exists before the command runs
    # A dry run that would land exits 0 and touches nothing.
    spec["dry_run"] = True
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    monkeypatch.setattr(
        sys,
        "argv",
        ["inference-grid", "--database", url, "land", "--json", str(tmp_path / "spec.json")],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert _task_file(world)["state"] == "passed"
    # A real landing exits 0 and settles the task.
    spec["dry_run"] = False
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    monkeypatch.setattr(
        sys,
        "argv",
        ["inference-grid", "--database", url, "land", "--json", str(tmp_path / "spec.json")],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert _task_file(world)["state"] == "landed"
    # Relanding is refused: the state transition is one-way. Exit 1.
    monkeypatch.setattr(
        sys,
        "argv",
        ["inference-grid", "--database", url, "land", "--json", str(tmp_path / "spec.json")],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
