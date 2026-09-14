"""Work the operator ran outside the grid reaches the scorecard without the ledger
pretending it dispatched it."""

import os
import time
import uuid

import pytest

from inference_grid.ledger import Ledger, Refused
from inference_grid.lanes.select import select_lane


@pytest.fixture
def ledger(tmp_path):
    url = os.environ.get("GRID_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger = Ledger(url)
    ledger.initialize()
    ledger.configure_account(
        "zai", 1, {"five_hour": 10}, time.time() + 300, ["glm-5.3-flash"], ["zai-alias"]
    )
    return ledger


def spec(**over):
    base = {
        "authorized": True,
        "model": "glm-5.3-flash",
        "family": "glm",
        "argv": ["cmd", "--print"],
        "workspace": "/tmp/lane",
        "account": "zai",
    }
    base.update(over)
    return base


def receipt(**over):
    base = {"verified_in_lane": True, "elapsed_s": 1909, "returncode": 0}
    base.update(over)
    return base


def test_external_work_is_a_completed_attempt_with_operator_provenance(ledger):
    out = ledger.record_external(
        "lane-glm-t2", "monarch", spec(), receipt(), "renderer-test", True
    )
    status = ledger.status()
    row = next(a for a in status if a["id"] == out["attempt"])
    assert row["state"] == "completed"
    assert row["receipt"]["provenance"] == "operator"
    card = ledger.scorecard(account="zai")
    assert [(e["category"], e["attempts"], e["accepted"], e["external"]) for e in card] == [
        ("renderer-test", 1, 1, 1)
    ]


def test_unverified_work_carries_the_operator_s_repair_count(ledger):
    ledger.record_external(
        "lane-glm-t1", "monarch", spec(), receipt(verified_in_lane=False),
        "e2e-spec", False, repairs=6, note="6 of 8 specs failed on the operator run",
    )
    (entry,) = ledger.scorecard(account="zai")
    assert (entry["accepted"], entry["repairs"], entry["external"]) == (0, 6, 1)


def test_external_work_is_written_once_and_refuses_what_it_cannot_vouch_for(ledger):
    ledger.record_external("lane-1", "monarch", spec(), receipt(), "tests", True)
    with pytest.raises(Refused, match="written once"):
        ledger.record_external("lane-1", "monarch", spec(), receipt(), "tests", True)
    with pytest.raises(Refused, match="cannot qualify"):
        ledger.record_external("lane-2", "monarch", spec(), receipt(), "canary", True)
    with pytest.raises(Refused, match="unknown account"):
        ledger.record_external("lane-3", "monarch", spec(account="nobody"), receipt(), "t", True)
    with pytest.raises(Refused, match="verified its own work"):
        ledger.record_external("lane-4", "monarch", spec(), {"elapsed_s": 1}, "t", True)
    with pytest.raises(Refused, match="authorization"):
        ledger.record_external("lane-5", "monarch", spec(authorized=False), receipt(), "t", True)
    with pytest.raises(Refused, match="workspace"):
        ledger.record_external("lane-6", "monarch", spec(workspace="rel"), receipt(), "t", True)


def test_no_quota_outbox_or_admission_is_invented(ledger):
    """The grid never admitted this attempt, so nothing downstream may act on it."""
    from sqlalchemy import select

    from inference_grid.ledger import events, outbox

    out = ledger.record_external("lane-1", "monarch", spec(), receipt(), "tests", True)
    with ledger.engine.connect() as con:
        assert not list(con.execute(select(outbox)))
        kinds = [
            e["kind"]
            for e in con.execute(select(events).where(events.c.attempt == out["attempt"])).mappings()
        ]
    assert kinds == ["external_recorded", "outcome_recorded"]


def test_the_scorecard_evidence_steers_select_lane(ledger):
    """Two lanes, same category; the one whose model the operator found wanting loses."""
    ledger.configure_account(
        "go", 1, {"five_hour": 10}, time.time() + 300, ["deepseek-v4.1-flash"], ["go-alias"]
    )
    for n in range(3):
        ledger.record_external(
            f"glm-{n}", "monarch", spec(), receipt(verified_in_lane=False), "e2e-spec", False,
            repairs=6,
        )
    ledger.record_external(
        "ds-0", "monarch", spec(model="deepseek-v4.1-flash", family="deepseek", account="go"),
        receipt(), "e2e-spec", True,
    )
    lanes = {
        "zai": {"family": "glm", "model": "glm-5.3-flash", "categories": ["e2e-spec"]},
        "go": {"family": "deepseek", "model": "deepseek-v4.1-flash", "categories": ["e2e-spec"]},
    }
    ready = {"zai": {"state": "ready"}, "go": {"state": "ready"}}
    choice = select_lane({"category": "e2e-spec"}, lanes, ready, ledger.scorecard(), time.time())
    assert choice["lane"] == "go"
    # Without the evidence the tie breaks on the lane id alone.
    assert select_lane({"category": "e2e-spec"}, lanes, ready, [], time.time())["lane"] == "go"
    lanes_rev = {"a-zai": lanes["zai"], "go": lanes["go"]}
    ready_rev = {"a-zai": {"state": "ready"}, "go": {"state": "ready"}}
    assert select_lane({"category": "e2e-spec"}, lanes_rev, ready_rev, [], time.time())["lane"] == "a-zai"
    assert select_lane({"category": "e2e-spec"}, lanes_rev, ready_rev, ledger.scorecard(), time.time())["lane"] == "go"
