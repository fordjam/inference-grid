import json
import sys
import time

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
    with ledger.tx() as con:
        con.execute(insert(tasks).values(id="t", project="p", spec={"argv": [sys.executable]}))
    report = diagnose(url)
    assert report["status"] == "checks_passed"
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
