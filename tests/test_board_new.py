"""Board task authoring helper: validated files, empty brief, schema test, no overwrites."""

import json

import pytest

from inference_grid.board.new import SCHEMA_TEST, new_task
from inference_grid.board.runner import load_board


def task(tid="write-mod", category="pure_function", brief="grid/briefs/write-mod.txt"):
    return dict(
        id=tid,
        category=category,
        brief=brief,
        inputs=[brief],
        tests=["grid/tests/test_review_schema.py"] if category == "independent_review" else [],
        artifacts=["reply.txt"] if category == "independent_review" else ["mod.py"],
        lanes=["zcode"],
        author_family=None,
        budget={"wall_seconds": 120, "output_bytes": 100000, "thinking_tokens": None},
        state="ready",
        blocked_reason=None,
    )


def test_work_task_writes_file_and_empty_brief(tmp_path):
    board = tmp_path / "grid/board"
    created = new_task(board, tmp_path, task())
    assert json.loads((board / "write-mod.json").read_text())["id"] == "write-mod"
    assert created["brief"].endswith("grid/briefs/write-mod.txt")
    assert (tmp_path / "grid/briefs/write-mod.txt").read_text() == ""
    # The board loads cleanly through the runner.
    assert load_board(board)["write-mod"][1]["state"] == "ready"
    assert "schema_test" not in created


def test_review_task_also_writes_the_schema_test(tmp_path):
    board = tmp_path / "grid/board"
    created = new_task(board, tmp_path, task(category="independent_review"))
    schema = tmp_path / created["schema_test"]
    assert schema.read_text() == SCHEMA_TEST
    compile(schema.read_text(), "test_review_schema.py", "exec")


def test_existing_files_are_refused_and_left_unchanged(tmp_path):
    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task())
    brief = tmp_path / "grid/briefs/write-mod.txt"
    brief.write_text("the real brief")
    before = (board / "write-mod.json").read_text()
    with pytest.raises(FileExistsError, match="task file already exists"):
        new_task(board, tmp_path, task())
    with pytest.raises(FileExistsError, match="brief already exists"):
        new_task(board, tmp_path, task(tid="other", brief="grid/briefs/write-mod.txt"))
    assert (board / "write-mod.json").read_text() == before
    assert brief.read_text() == "the real brief"
    assert not (board / "other.json").exists()


def test_existing_schema_test_is_reused_not_overwritten(tmp_path):
    board = tmp_path / "grid/board"
    schema = tmp_path / "grid/tests/test_review_schema.py"
    schema.parent.mkdir(parents=True)
    schema.write_text("# coordinator's own schema test\n")
    created = new_task(board, tmp_path, task(category="independent_review"))
    assert schema.read_text() == "# coordinator's own schema test\n"
    assert created["schema_test"] == str(schema)
    assert (board / "write-mod.json").exists()


def test_invalid_task_writes_nothing(tmp_path):
    board, project = tmp_path / "grid/board", tmp_path
    broken = task()
    broken["state"] = "wrong-state"
    with pytest.raises(ValueError):
        new_task(board, project, broken)
    assert not board.exists() or not any(board.iterdir())
    assert not (project / "grid/briefs/write-mod.txt").exists()
