"""lane-init: the one-task canary board for a lane id."""

import json
from pathlib import Path

import pytest

from inference_grid.board.new import CANARY_BRIEF, CANARY_TEST, lane_init


def test_lane_init_writes_the_canary_board(tmp_path):
    board = tmp_path / "grid/board"
    created = lane_init(board, tmp_path, "go-kimi")
    assert created["id"] == "canary-go-kimi"
    task = json.loads((board / "canary-go-kimi.json").read_text())
    assert task["category"] == "canary"
    assert task["lanes"] == ["go-kimi"]
    assert task["artifacts"] == ["reply.txt"]
    assert task["tests"] == ["grid/tests/test_canary_reply.py"]
    assert task["inputs"] == [task["brief"]]
    assert task["state"] == "ready" and task["author_family"] is None
    assert (tmp_path / "grid/briefs/canary-go-kimi.txt").read_text() == CANARY_BRIEF + "\n"
    assert (tmp_path / "grid/tests/test_canary_reply.py").read_text() == CANARY_TEST
    # The operator is told exactly what to run next: a dry run, then the real tick.
    assert '"dry_run": true' in created["dry_run_command"]
    assert '"dry_run": false' in created["tick_command"]


def test_lane_init_refuses_an_existing_task_and_shares_the_test(tmp_path):
    board = tmp_path / "grid/board"
    first = lane_init(board, tmp_path, "go-kimi")
    with pytest.raises(FileExistsError):
        lane_init(board, tmp_path, "go-kimi")
    # A second lane reuses the shared canary test, never rewrites it.
    marker = first["test"]
    before = Path(marker).read_bytes()
    second = lane_init(board, tmp_path, "go-deepseek")
    assert Path(marker).read_bytes() == before
    assert second["test"] == marker
    assert json.loads((board / "canary-go-deepseek.json").read_text())["lanes"] == ["go-deepseek"]
