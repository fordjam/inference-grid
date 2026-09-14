"""tick-all: ordered board configs, prepare once, one JSON log line per board."""

import json

from inference_grid.tick_all import ordered_configs, tick_all
from inference_grid.ledger import Ledger


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


def test_tick_all_prepares_once_logs_one_line_per_board(tmp_path):
    write_board(tmp_path, "alpha", priority=10)
    write_board(tmp_path, "beta", priority=20)
    (tmp_path / "lanes.json").write_text(
        json.dumps(
            {
                "lanes": {
                    "go": {
                        "provider": "opencode", "family": "glm", "model": "glm-5.3-flash",
                        "kind": "go_http", "credential_path": None, "executable": None,
                        "plan_units": {}, "window": None, "max_concurrency": 1,
                        "wall_seconds": 600, "categories": ["pure_function"],
                    }
                }
            }
        )
    )
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    prepared = []
    log = tmp_path / "tick-all.log"
    lines = tick_all(
        ledger,
        tmp_path / "boards",
        log_path=log,
        prepare=lambda: prepared.append(1),
    )
    assert len(lines) == 2
    assert [line["board"] for line in lines] == [
        str(tmp_path / "projects/alpha/grid/board"),
        str(tmp_path / "projects/beta/grid/board"),
    ]
    assert prepared == [1]  # once, not once per board
    logged = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["board"] for entry in logged] == [line["board"] for line in lines]
    # No lane record in the ledger: the plans carry the skip reason, nothing dispatches.
    assert all(r["result"] == "no_ready_lane" for r in lines[0]["results"])
