"""Ledger defaults and doctor registration: every packaged lane is named, never silent."""

from inference_grid.doctor import diagnose
from inference_grid.ledger import Ledger


def test_initialize_seeds_no_placeholder_aliases_idempotently(tmp_path):
    """DEFAULT_ACCOUNTS is empty now that every packaged lane needing a pre-seeded
    alias (the retired cline_cli lane) is gone; initialize() seeds nothing, twice."""
    ledger = Ledger("sqlite:///" + str(tmp_path / "l.sqlite"))
    ledger.initialize()
    ledger.initialize()  # second run changes nothing
    from sqlalchemy import select

    from inference_grid.ledger import accounts, aliases

    with ledger.engine.connect() as con:
        aliases = [(r["id"], r["account"]) for r in con.execute(select(aliases)).mappings()]
        accounts = {r["id"]: r for r in con.execute(select(accounts)).mappings()}
    assert aliases == []
    assert accounts == {}


def test_doctor_names_packaged_modules_without_records(tmp_path):
    url = "sqlite:///" + str(tmp_path / "l.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    report = diagnose(url)
    assert report["modules_without_lane"] == [
        "codex",
        "go",
        "goat",
        "opencode",
        "zai",
        "zcode",
    ]
    assert "lane_modules_without_records" in report["findings"]
