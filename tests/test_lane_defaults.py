"""Ledger defaults and doctor registration: every packaged lane is named, never silent."""

import pytest

from inference_grid.doctor import diagnose
from inference_grid.ledger import Ledger, Refused, digest


def test_initialize_seeds_placeholder_aliases_idempotently(tmp_path):
    ledger = Ledger("sqlite:///" + str(tmp_path / "l.sqlite"))
    ledger.initialize()
    ledger.initialize()  # second run changes nothing
    from sqlalchemy import select

    from inference_grid.ledger import accounts, aliases

    with ledger.engine.connect() as con:
        aliases = [(r["id"], r["account"]) for r in con.execute(select(aliases)).mappings()]
        accounts = {r["id"]: r for r in con.execute(select(accounts)).mappings()}
    assert ("cline", "cline") in aliases
    assert accounts["cline"]["models"] == []
    # The placeholder claims nothing: expired and model-less, every claim is refused.
    spec = {
        "authorized": True,
        "model": "qwen3.8-max",
        "family": "qwen",
        "argv": ["/usr/bin/true"],
        "workspace": str(tmp_path / "ws"),
        "timeout": 60,
        "output_bytes": 1000,
        "inputs": {},
        "manifest_sha256": digest({}),
    }
    ledger.submit("t1", "p", spec)
    with pytest.raises(Refused, match="quota stale or model ineligible"):
        ledger.claim("t1", "cline", {"five_hour": 0.01, "weekly": 0.01})


def test_doctor_names_packaged_modules_without_records(tmp_path):
    url = "sqlite:///" + str(tmp_path / "l.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    report = diagnose(url)
    assert report["modules_without_lane"] == ["cline", "go", "goat", "zai", "zcode"]
    assert "lane_modules_without_records" in report["findings"]
