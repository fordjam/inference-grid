"""--qualify: the standard qualification packets authored from the fixed set."""

import json

import pytest

from inference_grid.board.new import qualify_task


def test_pure_function_packet(tmp_path):
    board = tmp_path / "grid/board"
    created = qualify_task(board, tmp_path, "go-qwen", "pure_function")
    assert created["id"] == "qualify-go-qwen-pure"
    task = json.loads((board / "qualify-go-qwen-pure.json").read_text())
    assert task["category"] == "pure_function"
    assert task["artifacts"] == ["normalize.py"]
    assert task["tests"] == ["grid/qualification/pure_function/test_normalize.py"]
    assert task["brief"] in task["inputs"]
    assert task["lanes"] == ["go-qwen"]
    # The fixed set is materialized into the project unmodified.
    assert "normalize_ratio" in (tmp_path / "grid/qualification/pure_function/test_normalize.py").read_text()
    assert (tmp_path / task["brief"]).read_text().startswith("Write normalize.py")


def test_tests_multi_file_packet(tmp_path):
    board = tmp_path / "grid/board"
    created = qualify_task(board, tmp_path, "goat", "tests_multi_file")
    task = json.loads((board / (created["id"] + ".json")).read_text())
    assert task["artifacts"] == ["store.py", "report.py"]


def test_independent_review_packet_carries_schema_and_findings_tests(tmp_path):
    board = tmp_path / "grid/board"
    created = qualify_task(board, tmp_path, "go-kimi", "independent_review")
    task = json.loads((board / (created["id"] + ".json")).read_text())
    assert task["tests"] == [
        "grid/tests/test_review_schema.py",
        "grid/qualification/independent_review/test_findings.py",
    ]
    assert task["inputs"][-1] == "grid/qualification/independent_review/seeded_defects.py"
    assert (tmp_path / "grid/tests/test_review_schema.py").is_file()


def test_unknown_category_and_existing_task_refuse(tmp_path):
    board = tmp_path / "grid/board"
    with pytest.raises(ValueError, match="category must be one of"):
        qualify_task(board, tmp_path, "go", "canary")
    qualify_task(board, tmp_path, "go", "pure_function")
    with pytest.raises(FileExistsError):
        qualify_task(board, tmp_path, "go", "pure_function")
