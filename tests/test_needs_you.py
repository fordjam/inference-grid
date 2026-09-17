"""02-A1 (open half) + 02-C3: land requests, heartbeats, data-status alarms, disk use,
and failed/held rows with a log tail and a text suggestion."""

import json
import time

from inference_grid.ledger import Ledger, digest
from inference_grid.needs_you import (
    cached_disk_usage_row,
    data_status_rows,
    disk_usage_row,
    failure_rows,
    heartbeat_rows,
    land_request_rows,
    needs_you,
)


def make_ledger(tmp_path, account="zai"):
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    ledger.configure_account(
        account, 2, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    return ledger


def make_spec(workspace="/tmp/ws"):
    return {
        "authorized": True,
        "model": "glm-5.3-flash",
        "family": "glm",
        "argv": ["/usr/bin/true"],
        "workspace": workspace,
        "timeout": 60,
        "output_bytes": 1000,
        "inputs": {},
        "manifest_sha256": digest({}),
    }


def receipt():
    return {
        "status": "completed",
        "finish_reason": "stop",
        "actual_model": "glm-5.3-flash",
        "manifest_sha256": digest({}),
        "verified_in_lane": True,
        "artifacts": [{"path": "out.py", "sha256": "a" * 64}],
    }


def write_board(tmp_path, tasks, name="board"):
    board = tmp_path / name
    board.mkdir(exist_ok=True)
    for task in tasks:
        (board / (task["id"] + ".json")).write_text(json.dumps(task))
    return board


# --- land_request_rows ---


def test_land_request_rows_lists_passed_and_accepted_packets(tmp_path):
    board = write_board(
        tmp_path,
        [
            {"id": "p1", "category": "packet", "state": "passed"},
            {"id": "p2", "category": "packet", "state": "accepted"},
            {"id": "p3", "category": "packet", "state": "ready"},
            {"id": "canary-1", "category": "canary", "state": "passed"},
            {"id": "review-p1", "category": "independent_review", "state": "passed"},
        ],
    )
    rows = land_request_rows([board])
    assert sorted((r["id"], r["reason"]) for r in rows) == [("p1", "passed"), ("p2", "accepted")]
    assert all(r["kind"] == "land_request" for r in rows)
    assert all(r["since"] for r in rows)


def test_land_request_rows_skip_unreadable_and_missing_board(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    (board / "bad.json").write_text("not json")
    assert land_request_rows([board]) == []
    assert land_request_rows([tmp_path / "missing"]) == []
    assert land_request_rows([]) == []


# --- heartbeat_rows ---


def test_heartbeat_rows_ages_known_fields(tmp_path):
    now = time.time()
    tick = tmp_path / "tick-boards.json"
    tick.write_text(json.dumps({"written_at": now - 100, "last_pass_at": now - 50}))
    collector = tmp_path / "zai-observation.json"
    collector.write_text(json.dumps({"observed_at": "2026-09-17T00:00:00Z"}))
    rows = heartbeat_rows({"tick-boards": tick, "zai": collector}, now=now)
    by_name = {r["name"]: r for r in rows}
    assert by_name["tick-boards"]["age_seconds"] == 50
    assert by_name["zai"]["age_seconds"] > 0
    assert by_name["zai"]["since"].startswith("2026-09-17")


def test_heartbeat_rows_missing_file_reports_unknown_age(tmp_path):
    rows = heartbeat_rows({"tick-boards": tmp_path / "absent.json"})
    assert rows == [{"name": "tick-boards", "age_seconds": None, "since": ""}]


def test_heartbeat_rows_falls_back_to_mtime_without_known_fields(tmp_path):
    path = tmp_path / "obs.json"
    path.write_text(json.dumps({"unrelated": True}))
    now = time.time() + 30
    rows = heartbeat_rows({"obs": path}, now=now)
    assert 25 <= rows[0]["age_seconds"] <= 35


# --- data_status_rows ---


def test_data_status_rows_alarms_only_when_the_file_says_so(tmp_path):
    ok = tmp_path / "ok_status.json"
    ok.write_text(json.dumps({"engine_recorded": True, "status": "ok"}))
    bad = tmp_path / "forward_status.json"
    bad.write_text(json.dumps({"engine_recorded": False, "unavailable_count": 3}))
    rows = data_status_rows({"ok": ok, "forward": bad})
    assert [r["id"] for r in rows] == ["forward"]
    assert rows[0]["kind"] == "data_status"
    assert "engine_recorded" in rows[0]["reason"]


def test_data_status_rows_missing_file_is_quiet(tmp_path):
    assert data_status_rows({"forward": tmp_path / "absent.json"}) == []
    assert data_status_rows(None) == []


# --- disk_usage_row ---


def test_disk_usage_row_sums_files_under_root(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x" * 100)
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.txt").write_bytes(b"y" * 50)
    row = disk_usage_row(root, cap_bytes=1000)
    assert row == {
        "root": str(root),
        "exists": True,
        "total_bytes": 150,
        "cap_bytes": 1000,
        "over_cap": False,
    }


def test_disk_usage_row_over_cap(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x" * 200)
    row = disk_usage_row(root, cap_bytes=100)
    assert row["over_cap"] is True


def test_disk_usage_row_missing_root(tmp_path):
    row = disk_usage_row(tmp_path / "absent")
    assert row["exists"] is False
    assert row["total_bytes"] == 0


# --- failure_rows ---


def test_failure_rows_covers_failed_and_held_attempts(tmp_path):
    ledger = make_ledger(tmp_path)
    spec = make_spec("/tmp/ws-held")

    ledger.submit("t-held", "p", spec)
    aid_held, gen_held = ledger.claim("t-held", "zai", {"five_hour": 0.01, "weekly": 0.01})
    ledger.start(aid_held, gen_held)
    ledger.hold(aid_held, "rounds_exhausted: no round left")

    spec2 = make_spec("/tmp/ws-failed")
    ledger.submit("t-failed", "p", spec2)
    aid_failed, gen_failed = ledger.claim("t-failed", "zai", {"five_hour": 0.01, "weekly": 0.01})
    ledger.start(aid_failed, gen_failed)
    ledger.hold(aid_failed, "same_gate_repeated: gate x")
    ledger.resolve(aid_failed, "consumed", "confirmed the provider ran", "james")

    rows = failure_rows(ledger)
    kinds = {r["id"]: r["kind"] for r in rows}
    assert kinds[aid_held] == "held_attempt"
    assert kinds[aid_failed] == "failed_attempt"
    held_row = next(r for r in rows if r["id"] == aid_held)
    assert held_row["task"] == "t-held"
    assert held_row["log_tail"] == ""  # no packets_root given
    assert "wall budget" not in held_row["suggestion"]
    assert "max_rounds" in held_row["suggestion"]


def test_failure_rows_reads_the_transcript_tail_from_packets_root(tmp_path):
    ledger = make_ledger(tmp_path)
    spec = make_spec()
    ledger.submit("t1", "p", spec)
    aid, gen = ledger.claim("t1", "zai", {"five_hour": 0.01, "weekly": 0.01})
    ledger.start(aid, gen)
    ledger.hold(aid, "agent_idle: stalled")

    packets_root = tmp_path / "packets"
    attempt_dir = packets_root / "t1" / "round-1" / "attempts" / aid
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "native-1.jsonl").write_text('{"line": "hello"}\n')

    rows = failure_rows(ledger, packets_root=packets_root)
    assert "hello" in rows[0]["log_tail"]


def test_failure_rows_ignores_other_states(tmp_path):
    ledger = make_ledger(tmp_path)
    spec = make_spec()
    ledger.submit("t1", "p", spec)
    aid, gen = ledger.claim("t1", "zai", {"five_hour": 0.01, "weekly": 0.01})
    ledger.start(aid, gen)
    ledger.finish(aid, gen, receipt())
    assert failure_rows(ledger) == []


# --- needs_you: the combined overlay ---


def test_needs_you_folds_land_requests_and_data_status_into_operator(tmp_path):
    ledger = make_ledger(tmp_path)
    board = write_board(tmp_path, [{"id": "p1", "category": "packet", "state": "passed"}])
    status = tmp_path / "forward_status.json"
    status.write_text(json.dumps({"engine_recorded": False}))
    # workspace_root is explicit and never the real ~/.grid-workspaces: tests stay on
    # fixtures, never the operator's moving data cache.
    out = needs_you(
        ledger,
        board_dirs=[board],
        data_status_paths={"forward": status},
        workspace_root=tmp_path / "workspaces",
    )
    assert any(r["kind"] == "land_request" and r["id"] == "p1" for r in out["operator"])
    assert any(r["kind"] == "data_status" and r["id"] == "forward" for r in out["operator"])
    assert out["heartbeats"] == []
    assert out["disk_usage"]["exists"] is False  # nothing was ever created at this root
    assert out["failures"] == []


def test_needs_you_reports_no_disk_reading_without_root_or_status_path(tmp_path):
    # The hot overlay-build path must never trigger a live walk by accident: neither
    # workspace_root nor workspace_status_path given means "no reading", not a walk of
    # the real default ~/.grid-workspaces.
    ledger = make_ledger(tmp_path)
    out = needs_you(ledger)
    assert out["disk_usage"] == {
        "root": None,
        "exists": False,
        "total_bytes": 0,
        "cap_bytes": 4 * 1024**3,
    }


# --- cached_disk_usage_row: the safe, hot-path reading (02-A5's pruner writes it) ---


def test_cached_disk_usage_row_reads_the_pruners_reading_file(tmp_path):
    status = tmp_path / "reading.json"
    status.write_text(
        json.dumps(
            {
                "written_at": "2026-09-17T00:00:00Z",
                "root": "/Users/james/.grid-workspaces",
                "total_bytes": 500,
                "cap_bytes": 1000,
            }
        )
    )
    row = cached_disk_usage_row(status)
    assert row == {
        "root": "/Users/james/.grid-workspaces",
        "exists": True,
        "total_bytes": 500,
        "cap_bytes": 1000,
        "over_cap": False,
        "since": "2026-09-17T00:00:00Z",
    }


def test_cached_disk_usage_row_missing_or_malformed_file(tmp_path):
    assert cached_disk_usage_row(None)["exists"] is False
    assert cached_disk_usage_row(tmp_path / "absent.json")["exists"] is False
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert cached_disk_usage_row(bad)["exists"] is False
    bad2 = tmp_path / "bad2.json"
    bad2.write_text(json.dumps({"total_bytes": "nope"}))
    assert cached_disk_usage_row(bad2)["exists"] is False


def test_cached_disk_usage_row_falls_back_to_the_passed_cap_without_one_on_file(tmp_path):
    status = tmp_path / "reading.json"
    status.write_text(json.dumps({"total_bytes": 500}))
    row = cached_disk_usage_row(status, cap_bytes=2000)
    assert row["cap_bytes"] == 2000
    assert row["over_cap"] is False


# --- guards: malformed operator-supplied config never crashes ---


def test_heartbeat_rows_rejects_non_dict_input():
    assert heartbeat_rows(["not", "a", "dict"]) == []
    assert heartbeat_rows(None) == []


def test_data_status_rows_rejects_non_dict_input():
    assert data_status_rows(["not", "a", "dict"]) == []
    assert data_status_rows(None) == []


def test_attempt_dir_never_crashes_on_an_empty_task_or_attempt_id(tmp_path):
    from inference_grid.needs_you import _attempt_dir

    assert _attempt_dir(tmp_path, "", "a1") is None
    assert _attempt_dir(tmp_path, "t1", "") is None
    assert _attempt_dir(None, "t1", "a1") is None
    assert _attempt_dir(tmp_path, "/etc/passwd", "a1") is None
