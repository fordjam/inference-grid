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


def test_retry_picks_the_next_free_suffix_and_supersedes(tmp_path):
    from inference_grid.board.new import retry_task

    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task())
    # An existing -2 must push the retry to -3.
    (board / "write-mod-2.json").write_text(
        (board / "write-mod.json").read_text().replace('"id": "write-mod"', '"id": "write-mod-2"')
    )
    created = retry_task(board, tmp_path, "write-mod", "900 s budget after two transport holds")
    assert created["id"] == "write-mod-3"
    new_task_json = json.loads((board / "write-mod-3.json").read_text())
    assert new_task_json["state"] == "ready" and new_task_json["blocked_reason"] is None
    assert new_task_json["brief"] == "grid/briefs/write-mod-3.txt"
    assert "grid/briefs/write-mod-3.txt" in new_task_json["inputs"]
    # The brief is copied verbatim - the one non-empty brief board-new may write.
    assert (tmp_path / "grid/briefs/write-mod-3.txt").read_text() == (
        tmp_path / "grid/briefs/write-mod.txt"
    ).read_text()
    predecessor = json.loads((board / "write-mod.json").read_text())
    assert predecessor["state"] == "blocked"
    assert predecessor["blocked_reason"] == (
        "superseded: write-mod-3 — 900 s budget after two transport holds"
    )
    assert load_board(board)["write-mod-3"][1]["state"] == "ready"


def test_retry_applies_overrides(tmp_path):
    from inference_grid.board.new import retry_task

    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task())
    retry_task(
        board,
        tmp_path,
        "write-mod",
        "wider lanes for the reviewer pool",
        budget={"wall_seconds": 900, "output_bytes": 2000000, "thinking_tokens": None},
        lanes=["zcode", "go"],
        author_family="glm",
    )
    retried = json.loads((board / "write-mod-2.json").read_text())
    assert retried["budget"]["wall_seconds"] == 900
    assert retried["lanes"] == ["zcode", "go"]
    assert retried["author_family"] == "glm"


def test_retry_of_a_review_task_keeps_the_schema_test(tmp_path):
    from inference_grid.board.new import retry_task

    board = tmp_path / "grid/board"
    review = task(category="independent_review")
    new_task(board, tmp_path, review)
    schema = tmp_path / "grid/tests/test_review_schema.py"
    original = schema.read_text()
    retry_task(board, tmp_path, "write-mod", "reviewer family was wrong")
    retried = json.loads((board / "write-mod-2.json").read_text())
    assert retried["category"] == "independent_review"
    assert schema.read_text() == original  # reused, never rewritten
    assert retried["brief"] == "grid/briefs/write-mod-2.txt"
    assert (tmp_path / "grid/briefs/write-mod-2.txt").read_text() == (
        tmp_path / "grid/briefs/write-mod.txt"
    ).read_text()


def test_retry_refuses_a_closed_predecessor_and_writes_nothing(tmp_path):
    from inference_grid.board.new import retry_task

    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task())
    accepted = json.loads((board / "write-mod.json").read_text())
    accepted["state"] = "accepted"
    (board / "write-mod.json").write_text(json.dumps(accepted))
    before = (board / "write-mod.json").read_text()
    with pytest.raises(ValueError, match="accepted"):
        retry_task(board, tmp_path, "write-mod", "retry after accept")
    assert not (board / "write-mod-2.json").exists()
    assert (board / "write-mod.json").read_text() == before


def test_retry_with_an_invalid_override_writes_nothing(tmp_path):
    from inference_grid.board.new import retry_task

    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task())
    before = (board / "write-mod.json").read_text()
    with pytest.raises(ValueError):
        retry_task(board, tmp_path, "write-mod", "bad budget", budget={"wall_seconds": 900})
    assert not (board / "write-mod-2.json").exists()
    assert not (tmp_path / "grid/briefs/write-mod-2.txt").exists()
    assert (board / "write-mod.json").read_text() == before
    with pytest.raises(ValueError, match="recorded change"):
        retry_task(board, tmp_path, "write-mod", "  ")


def test_retry_of_a_missing_task_refuses(tmp_path):
    from inference_grid.board.new import retry_task

    board = tmp_path / "grid/board"
    board.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        retry_task(board, tmp_path, "ghost", "nothing to retry")
