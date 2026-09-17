"""Board config ordering: priority then name."""

import json

from inference_grid.board_configs import ordered_configs


def write_board(base, name, priority=None):
    board = base / "projects" / name / "grid/board"
    board.mkdir(parents=True)
    task = dict(
        id="t1", category="pure_function", brief="grid/briefs/t1.txt",
        inputs=["grid/briefs/t1.txt"], tests=[], artifacts=["out.py"],
        lanes=["go"], author_family=None,
        budget={"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None},
        state="ready", blocked_reason=None,
    )
    (board / "t1.json").write_text(json.dumps(task))
    (base / "projects" / name / "grid/briefs").mkdir(parents=True, exist_ok=True)
    (base / "projects" / name / "grid/briefs/t1.txt").write_text("do it\n")
    config = {
        "board_dir": str(board),
        "project_root": str(base / "projects" / name),
        "lanes_path": str(base / "lanes.json"),
        "accounts_by_lane": {"go": "go-alias"},
        "packets_root": str(base / "packets" / name),
    }
    if priority is not None:
        config["priority"] = priority
    (base / "boards").mkdir(exist_ok=True)
    (base / "boards" / f"{name}.json").write_text(json.dumps(config))


def test_ordered_configs_sort_by_priority_then_name(tmp_path):
    write_board(tmp_path, "alpha", priority=50)
    write_board(tmp_path, "beta", priority=10)
    write_board(tmp_path, "gamma")
    ordered = ordered_configs(tmp_path / "boards")
    assert [c["project_root"] for c in ordered] == [
        str(tmp_path / "projects/beta"),
        str(tmp_path / "projects/alpha"),
        str(tmp_path / "projects/gamma"),
    ]
