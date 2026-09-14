import json
import sys
import time

import pytest

from sqlalchemy import insert

from inference_grid.doctor import diagnose
from inference_grid.ledger import Ledger, attempts, tasks


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.sqlite"
    report = diagnose("sqlite:///" + str(path))
    assert report["findings"] == ["database_missing_run_init"]
    assert not path.exists()


def test_empty_schema_does_not_initialize(tmp_path):
    path = tmp_path / "empty.sqlite"
    path.touch()
    assert diagnose("sqlite:///" + str(path))["findings"] == ["schema_missing_run_init"]
    assert path.stat().st_size == 0


def test_diagnostics_report_attention_without_changing_work(tmp_path):
    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    ledger.configure_account("a", 1, {"weekly": 10}, time.time() + 60, ["m"])
    with ledger.tx() as con:
        con.execute(
            insert(tasks).values(
                id="t", project="p", spec={"argv": ["/no/such/executable", "SECRET_ARG"]}
            )
        )
        con.execute(
            insert(attempts).values(
                id="x",
                task="t",
                account="a",
                generation=1,
                state="held",
                estimate={"weekly": 1},
                workspace="private-workspace",
                updated=1,
            )
        )
    before = ledger.status()
    report = diagnose(url, now=time.time() + 120)
    assert report["counts"]["stale_accounts"] == 1
    assert report["counts"]["held"] == 1
    assert report["counts"]["missing_executables"] == 1
    assert report["provider_auth"] == report["broker"] == "not_checked"
    assert "SECRET_ARG" not in json.dumps(report)
    assert "private-workspace" not in json.dumps(report)
    assert ledger.status() == before


def test_passing_checks_do_not_claim_provider_health(tmp_path):
    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    ledger.configure_account("a", 1, {"weekly": 10}, time.time() + 60, ["m"])
    # Every packaged lane module is registered: nothing is missing for a passing report.
    now = time.time()
    for provider in ("go", "goat", "cline", "zai", "zcode", "codex"):
        ledger.record_lane(
            provider,
            {
                "provider": provider,
                "auth": "ok",
                "quota_observed_at": now - 5,
                "quota_freshness_seconds": 900,
                "used_percent_max": 1.0,
                "admission_limit_percent": 80,
                "cooldown_until": None,
                "qualification": "qualified",
                "blocked_until": None,
                "blocker": None,
            },
        )
    with ledger.tx() as con:
        con.execute(insert(tasks).values(id="t", project="p", spec={"argv": [sys.executable]}))
    report = diagnose(url)
    assert report["status"] == "checks_passed"
    assert report["modules_without_lane"] == []
    assert report["provider_auth"] == "not_checked"


def test_errors_do_not_expose_connection_secrets():
    report = diagnose("invalid://user:SECRET_PASSWORD@host/db")
    assert report["status"] == "attention"
    assert "SECRET_PASSWORD" not in json.dumps(report)


def test_relative_executable_is_not_qualified_from_doctor_directory(tmp_path, monkeypatch):
    from inference_grid.doctor import executable_state

    script = tmp_path / "adapter"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    assert executable_state({"argv": ["./adapter"]}) == "unresolved"
    assert executable_state({"argv": [str(script)]}) == "present"
    assert executable_state(None) == "invalid"
    assert executable_state({"argv": []}) == "invalid"


def test_doctor_separates_usage_and_inference_cooldowns(tmp_path):
    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    ledger.configure_account("a", 1, {"weekly": 10}, time.time() + 60, ["m"])
    ledger.defer("a", "usage", time.time() + 30)
    report = diagnose(url)
    assert report["counts"]["usage_cooldowns"] == 1
    assert report["counts"]["inference_cooldowns"] == 0
    assert "usage_collection_cooldown_active" in report["findings"]


def test_lane_records_are_classified_not_trusted(tmp_path):
    from inference_grid.ledger import Refused, lanes

    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    ledger.configure_account("a", 1, {"weekly": 10}, time.time() + 60, ["m"])
    now = time.time()
    record = dict(
        provider="opencode",
        auth="ok",
        quota_observed_at=now - 10,
        quota_freshness_seconds=900,
        used_percent_max=1.0,
        admission_limit_percent=80,
        cooldown_until=None,
        qualification="qualified",
        blocked_until=None,
        blocker=None,
    )
    assert ledger.record_lane("opencode", record)["state"] == "ready"
    blocked = dict(record, provider="clinepass", blocked_until=now + 3600, blocker="weekly reset")
    assert ledger.record_lane("clinepass", blocked)["state"] == "blocked"
    with pytest.raises(Refused):
        ledger.record_lane("codex", dict(record, provider="claude"))
    with pytest.raises(Refused):
        ledger.record_lane("codex", dict(record, provider="codex", auth="maybe"))
    report = diagnose(url)
    states = {lane["provider"]: (lane["state"], lane["reason"]) for lane in report["lanes"]}
    assert states == {"opencode": ("ready", "ready"), "clinepass": ("blocked", "weekly reset")}
    assert "provider_lanes_not_ready" in report["findings"]
    # A tampered stored record is reported as invalid, never treated as ready.
    with ledger.tx() as con:
        from sqlalchemy import update

        con.execute(
            update(lanes)
            .where(lanes.c.provider == "opencode")
            .values(record={"provider": "opencode"})
        )
    report = diagnose(url)
    assert {lane["provider"]: lane["state"] for lane in report["lanes"]}["opencode"] == "invalid"
    assert json.dumps(report).count("weekly reset") == 1


def test_board_counts_and_missing_dirs_are_reported_read_only(tmp_path):
    from inference_grid.doctor import board_counts, diagnose

    board = tmp_path / "grid/board"
    board.mkdir(parents=True)

    def task(state, tid):
        return json.dumps(
            dict(
                id=tid,
                category="pure_function",
                brief="grid/briefs/t1.txt",
                inputs=["grid/briefs/t1.txt"],
                tests=[],
                artifacts=["out.py"],
                lanes=["go"],
                author_family=None,
                budget={"wall_seconds": 60, "output_bytes": 1000, "thinking_tokens": None},
                state=state,
                blocked_reason="held for review" if state == "blocked" else None,
            )
        )

    (board / "t1.json").write_text(task("ready", "t1"))
    (board / "t2.json").write_text(task("blocked", "t2"))
    (board / "junk.json").write_text("{not json")
    staged = board / "review"
    staged.mkdir()
    (staged / "artifact.json").write_text('{"verdict": "approved"}')  # subdirectory: not a task
    assert board_counts(board) == {"ready": 1, "blocked": 1, "invalid": 1}
    assert board_counts(tmp_path / "absent") == {"missing": 1}

    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    Ledger(url).initialize()
    report = diagnose(url, boards=[str(board), str(tmp_path / "absent")])
    assert report["boards"] == {
        str(board): {"ready": 1, "blocked": 1, "invalid": 1},
        str(tmp_path / "absent"): {"missing": 1},
    }
    # Boards are classified without touching anything.
    assert board_counts(board)["ready"] == 1


def test_superseded_predecessors_are_counted_closed_not_blocked(tmp_path):
    from inference_grid.doctor import board_counts

    def task(state, tid, reason=None):
        return json.dumps(
            dict(
                id=tid,
                category="pure_function",
                brief="grid/briefs/t.txt",
                inputs=["grid/briefs/t.txt"],
                tests=[],
                artifacts=["out.py"],
                lanes=["go"],
                author_family=None,
                budget={"wall_seconds": 60, "output_bytes": 1000, "thinking_tokens": None},
                state=state,
                blocked_reason=reason,
            )
        )

    board = tmp_path / "board"
    board.mkdir()
    (board / "old.json").write_text(
        task("blocked", "old", "superseded: replaced by new (review round 2)")
    )
    (board / "stuck.json").write_text(
        task("blocked", "stuck", "attempt x held; resolve with evidence")
    )
    (board / "new.json").write_text(task("ready", "new"))
    assert board_counts(board) == {"superseded": 1, "blocked": 1, "ready": 1}
