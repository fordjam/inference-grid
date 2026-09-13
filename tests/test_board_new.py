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
    # A review retry needs a staged source link to move to the new review id.
    write_source_link(board, "write-mod", "write-mod")
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


def test_retry_of_a_plain_id_gets_the_first_suffix(tmp_path):
    from inference_grid.board.new import next_retry_id

    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task(tid="x", brief="grid/briefs/x.txt"))
    assert next_retry_id(board, "x") == "x-2"


def test_retry_of_a_retry_increments_the_suffix(tmp_path):
    # A chain of retries must read as a sequence (x, x-2, x-3, ...), not stack suffixes.
    from inference_grid.board.new import next_retry_id, retry_task

    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task(tid="x", brief="grid/briefs/x.txt"))
    retry_task(board, tmp_path, "x", "first retry")
    assert next_retry_id(board, "x-2") == "x-3"
    retry_task(board, tmp_path, "x-2", "second retry")
    assert next_retry_id(board, "x-3") == "x-4"
    assert json.loads((board / "x-3.json").read_text())["state"] == "ready"


def test_a_superseded_suffixed_task_counts_on_without_its_predecessor(tmp_path):
    # The blocked_reason alone marks the id as part of a retry chain.
    from inference_grid.board.new import next_retry_id

    board = tmp_path / "grid/board"
    board.mkdir(parents=True)
    superseded = task(tid="x-2", brief="grid/briefs/x.txt")
    superseded["state"] = "blocked"
    superseded["blocked_reason"] = "superseded: x-3 — earlier chain"
    (board / "x-2.json").write_text(json.dumps(superseded))
    assert next_retry_id(board, "x-2") == "x-3"


def test_a_stacked_legacy_id_is_left_alone_and_its_retry_continues_it(tmp_path):
    # review-board-runner-3-2 was produced before suffixes counted; it is never renamed
    # and its retry continues its own chain (x-3-2 → x-3-3).
    from inference_grid.board.new import next_retry_id

    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task(tid="x-3", brief="grid/briefs/x-3.txt"))
    (board / "x-3-2.json").write_text(
        (board / "x-3.json").read_text().replace('"id": "x-3"', '"id": "x-3-2"')
    )
    assert next_retry_id(board, "x-3-2") == "x-3-3"
    assert json.loads((board / "x-3-2.json").read_text())["id"] == "x-3-2"


def write_source_link(board, source_id, review_task):
    """A review source link; review_task None models a link written before the field existed."""
    link = dict(
        task=source_id,
        attempt="a" * 8 + "-0000",
        lane="go",
        family="glm",
        receipt_digest="d" * 64,
        artifacts=["reply.txt"],
    )
    if review_task is not None:
        link["review_task"] = review_task
    stage = board / "review" / source_id
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "source.json").write_text(json.dumps(link))
    return link


def review_board(tmp_path):
    """A board holding one independent_review task with its schema test."""
    board = tmp_path / "grid/board"
    new_task(
        board,
        tmp_path,
        task(
            tid="review-x",
            category="independent_review",
            brief="grid/briefs/review-x.txt",
        ),
    )
    return board


def test_retry_of_a_review_moves_the_source_link(tmp_path):
    from inference_grid.board.new import retry_task

    board = review_board(tmp_path)
    link = write_source_link(board, "x", "review-x")
    created = retry_task(board, tmp_path, "review-x", "reviewer family was wrong")
    assert created["id"] == "review-x-2"
    assert created["source_link"].endswith("review/x/source.json")
    moved = json.loads((board / "review/x/source.json").read_text())
    assert moved["review_task"] == "review-x-2"
    assert moved["review_task_history"] == ["review-x"]
    # Everything else about the link is preserved.
    assert moved["task"] == "x" and moved["attempt"] == link["attempt"]


def test_retry_gives_a_legacy_link_its_review_task(tmp_path):
    from inference_grid.board.new import retry_task

    board = review_board(tmp_path)
    write_source_link(board, "x", None)  # written before handoff-2 A2 named the review
    retry_task(board, tmp_path, "review-x", "brief amended")
    moved = json.loads((board / "review/x/source.json").read_text())
    assert moved["review_task"] == "review-x-2"
    assert moved["review_task_history"] == ["review-x"]


def test_retry_history_accumulates_over_two_retries(tmp_path):
    from inference_grid.board.new import retry_task

    board = review_board(tmp_path)
    write_source_link(board, "x", "review-x")
    retry_task(board, tmp_path, "review-x", "first change")
    retry_task(board, tmp_path, "review-x-2", "second change")
    moved = json.loads((board / "review/x/source.json").read_text())
    assert moved["review_task"] == "review-x-3"
    assert moved["review_task_history"] == ["review-x", "review-x-2"]


def test_a_non_review_retry_touches_no_link(tmp_path):
    from inference_grid.board.new import retry_task

    board = tmp_path / "grid/board"
    new_task(board, tmp_path, task())
    write_source_link(board, "write-mod", "review-write-mod")
    before = (board / "review/write-mod/source.json").read_text()
    retry_task(board, tmp_path, "write-mod", "wider budget")
    assert (board / "review/write-mod/source.json").read_text() == before


def test_retry_of_a_review_without_a_source_link_refuses(tmp_path):
    from inference_grid.board.new import retry_task

    board = review_board(tmp_path)
    with pytest.raises(ValueError, match="source"):
        retry_task(board, tmp_path, "review-x", "nothing staged to retry")
    assert not (board / "review-x-2.json").exists()
    assert not (tmp_path / "grid/briefs/review-x-2.txt").exists()
    assert json.loads((board / "review-x.json").read_text())["state"] == "ready"
