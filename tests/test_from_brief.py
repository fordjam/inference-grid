"""A brief becomes board work: from_brief's done signals, authoring and CLI round trip.

Every repository read goes through from_brief.run, so the tests fake git (the sandbox
denies it); the files themselves are real.
"""

import json
import sys

import pytest

from inference_grid.board import from_brief as from_brief_module
from inference_grid.board.from_brief import from_brief
from inference_grid.board.runner import load_board

BRIEF_DOC = """# Handoff brief 17 — backlog

## 1. Hard rules

### Never run
- No `git push`, no tags, no releases.

---

## Your packet

#### J5. Item five
Do five.
- Size: small.

#### J6. Item six
Do six.

#### J7. Item seven
Do seven.

#### I2. Item two
Do two.
"""

GATES = [{"name": "pytest", "argv": ["python3", "-m", "pytest", "-q"]}]
OWN = "a" * 40


def fake_git(project, subjects=(), branch="main", adding_commit=OWN):
    """The git seam, faked: branch, the brief's adding commit, the subjects since it."""

    def run(argv, cwd=None):
        assert argv[0] == "git", argv
        rest = argv[3:]
        if rest == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return 0, branch + "\n"
        if rest[:3] == ["log", "--format=%H", "--diff-filter=A"]:
            return 0, (adding_commit + "\n") if adding_commit else ""
        if rest[:2] == ["log", "--format=%h %s"]:
            return 0, "".join(f"{i:06x}bcd {s}\n" for i, s in enumerate(subjects))
        raise AssertionError("unexpected git call: " + repr(argv))

    return run


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A swept repository: the brief committed (its own commit), one signal per kind."""
    project = tmp_path / "proj"
    board = project / "grid/board"
    (project / "docs/reports").mkdir(parents=True)
    board.mkdir(parents=True)
    (project / "docs/reports/packet-j6.md").write_text("J6's report\n")
    (project / "docs/CONTRIBUTIONS.md").write_text(
        "| Id | Why | Where |\n| --- | --- | --- |\n| I2 | did it | src/x.py |\n"
    )
    (project / "docs/handoff-glm-17.md").write_text(BRIEF_DOC)
    monkeypatch.setattr(
        from_brief_module, "run", fake_git(project, subjects=["bcdef12 brief 17, J7: done"])
    )
    return project, board


def call(project, board, **kw):
    return from_brief(board, project, "docs/handoff-glm-17.md", lanes=["goat"], gates=GATES, **kw)


def ids(report, key):
    return [entry["id"] for entry in report[key]]


def test_a_report_file_decides_done(world):
    project, board = world
    report = call(project, board)
    # J7 is commit-done and I2 CONTRIBUTIONS-done by the fixture; J6 by its report file.
    assert ids(report, "done") == ["J6", "J7", "I2"]
    evidence = next(e for e in report["done"] if e["id"] == "J6")["evidence"]
    assert "docs/reports/packet-j6.md" in evidence


def test_a_contributions_row_decides_done(world):
    project, board = world
    report = call(project, board)
    assert "I2" in ids(report, "done")
    assert "CONTRIBUTIONS" in next(e for e in report["done"] if e["id"] == "I2")["evidence"]


def test_a_lane_named_contributions_row_also_decides_done(world, monkeypatch):
    # The shape this repository's own tables write: the lane names the item first.
    project, board = world
    monkeypatch.setattr(from_brief_module, "run", fake_git(project))
    (project / "docs/CONTRIBUTIONS.md").write_text(
        "| Lane | Assignment | Outcome |\n| --- | --- | --- |\n"
        "| GLM-5.3-Flash (I2, branch `packet/packet-i2`) | did it | landed |\n"
    )
    report = call(project, board)
    assert "I2" in ids(report, "done")


def test_a_commit_subject_since_the_briefs_own_commit_decides_done(world, monkeypatch):
    project, board = world
    monkeypatch.setattr(
        from_brief_module, "run", fake_git(project, subjects=["bcdef12 brief 17, J7: done"])
    )
    report = call(project, board)
    assert "J7" in ids(report, "done")
    evidence = next(e for e in report["done"] if e["id"] == "J7")["evidence"]
    assert "brief 17, J7" in evidence


def test_commit_subject_shapes_and_numbers(monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    brief_rel = "docs/handoff-glm-17.md"
    # The three shapes seen in the swept repositories.
    for subject in ("brief 17, J5: landed", "brief 17: J5 — landed", "(brief 17 J5)"):
        monkeypatch.setattr(
            from_brief_module, "run", fake_git(project, subjects=[f"bcdef12 {subject}"])
        )
        evidence = from_brief_module.commit_evidence(project, brief_rel, 17, "J5")
        assert evidence and subject in evidence
    # The handoff shape of the other repositories.
    monkeypatch.setattr(
        from_brief_module,
        "run",
        fake_git(project, subjects=["bcdef12 fix [handoff-17/J5] done"]),
    )
    assert "handoff-17/J5" in from_brief_module.commit_evidence(project, brief_rel, 17, "J5")
    # An id without the brief's number decides nothing; nor does another brief's number.
    monkeypatch.setattr(
        from_brief_module,
        "run",
        fake_git(
            project, subjects=["bcdef12 item two landed (I2)", "bcdef13 brief 16, I2: elsewhere"]
        ),
    )
    assert from_brief_module.commit_evidence(project, brief_rel, 17, "I2") is None


def test_nothing_before_the_briefs_own_commit_counts(monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    # An uncommitted brief has no own commit; neither has it commit evidence.
    monkeypatch.setattr(
        from_brief_module,
        "run",
        fake_git(project, subjects=["bcdef12 brief 17, I2: after"], adding_commit=None),
    )
    assert from_brief_module.commit_evidence(project, "docs/handoff-glm-17.md", 17, "I2") is None


def test_an_undone_item_is_authored_with_the_right_lanes_and_gates(world):
    project, board = world
    report = call(project, board)
    assert ids(report, "authored") == ["J5"]
    task = json.loads((board / "packet-j5.json").read_text())
    assert (task["category"], task["state"], task["lanes"]) == ("packet", "ready", ["goat"])
    assert task["spec"]["packet_id"] == "J5"
    assert task["spec"]["base"] == "main"
    assert task["spec"]["gates"] == GATES
    assert task["spec"]["brief"] == task["brief"] == "grid/briefs/packet-j5.txt"
    assert task["inputs"] == ["grid/briefs/packet-j5.txt"]
    assert task["artifacts"] == ["docs/reports/packet-j5.md"]
    # The board loads the authored task through the runner's validation.
    assert load_board(board)["packet-j5"][1]["state"] == "ready"
    text = (project / "grid/briefs/packet-j5.txt").read_text()
    assert text.startswith("## 1. Hard rules")
    assert "#### J5. Item five" in text
    assert "No `git push`" in text  # the umbrella's rules ride along
    assert "#### J6." not in text  # the item alone, not the whole packet phase


def test_a_brief_whose_rules_lack_the_closing_rule_is_refused_with_the_line(tmp_path):
    project = tmp_path / "proj"
    (project / "docs").mkdir(parents=True)
    (project / "docs/handoff-glm-17.md").write_text(
        "# brief\n\n## 1. Hard rules\n\n- No `git push`.\n"
    )
    with pytest.raises(ValueError, match="---"):
        from_brief(
            project / "grid/board",
            project,
            "docs/handoff-glm-17.md",
            lanes=["goat"],
            gates=GATES,
        )


def test_gates_default_to_the_tick_configs_test_argv(world, tmp_path):
    project, board = world
    boards_dir = tmp_path / "boards"
    boards_dir.mkdir()
    (boards_dir / "tick-proj.json").write_text(
        json.dumps(
            {
                "board_dir": str(board),
                "project_root": str(project),
                "test_argv": ["python3", "-m", "pytest", "-q"],
            }
        )
    )
    from_brief(board, project, "docs/handoff-glm-17.md", lanes=["goat"], boards_dir=boards_dir)
    task = json.loads((board / "packet-j5.json").read_text())
    assert task["spec"]["gates"] == [{"name": "tests", "argv": ["python3", "-m", "pytest", "-q"]}]


def test_a_tick_config_without_test_argv_is_refused_with_the_fix(world, tmp_path):
    project, board = world
    boards_dir = tmp_path / "boards"
    boards_dir.mkdir()
    (boards_dir / "tick-proj.json").write_text(
        json.dumps({"board_dir": str(board), "project_root": str(project)})
    )
    with pytest.raises(ValueError, match="test_argv"):
        from_brief(board, project, "docs/handoff-glm-17.md", lanes=["goat"], boards_dir=boards_dir)


def test_a_dry_run_writes_nothing(world):
    project, board = world
    report = call(project, board, dry_run=True)
    assert report["dry_run"] is True
    assert ids(report, "authored") == ["J5"]
    assert not (board / "packet-j5.json").exists()
    assert not (project / "grid/briefs/packet-j5.txt").exists()


def test_a_rerun_skips_what_the_board_already_holds(world):
    project, board = world
    call(project, board)
    report = call(project, board)
    assert ids(report, "authored") == []
    assert ids(report, "skipped") == ["J5"]


def test_lanes_are_required(world):
    project, board = world
    with pytest.raises(ValueError, match="lanes"):
        from_brief(board, project, "docs/handoff-glm-17.md", gates=GATES)


def test_board_new_round_trips_through_main(tmp_path, monkeypatch, world):
    import io

    from inference_grid import cli

    project, board = world
    payload = {
        "board_dir": str(board),
        "project_root": str(project),
        "from_brief": "docs/handoff-glm-17.md",
        "lanes": ["goat"],
        "gates": GATES,
        "base": "main",
    }
    argfile = tmp_path / "from-brief.json"
    argfile.write_text(json.dumps(payload))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inference-grid",
            "--database",
            "sqlite:///" + str(tmp_path / "ledger.sqlite"),
            "board-new",
            "--json",
            str(argfile),
        ],
    )
    buffer = io.StringIO()
    real = sys.stdout
    sys.stdout = buffer
    try:
        cli.main()
    finally:
        sys.stdout = real
    report = json.loads(buffer.getvalue())
    assert ids(report, "authored") == ["J5"]
    assert (board / "packet-j5.json").exists()
