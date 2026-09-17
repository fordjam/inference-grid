"""`inference-grid report --week`: per-lane outcome counts, reviews, holds and cost."""

import time

import pytest

from inference_grid.ledger import Ledger, digest
from inference_grid.report import report, week_bounds


@pytest.fixture
def ledger(tmp_path):
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    ledger.configure_account("acct", 2, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["m1"])
    return ledger


def submit_and_claim(ledger, task_id, account="acct"):
    ledger.submit(
        task_id,
        "p",
        dict(
            authorized=True,
            model="m1",
            family="fam",
            argv=["/bin/true"],
            workspace="/tmp/" + task_id,
            timeout=1,
            output_bytes=100,
            inputs={},
            manifest_sha256=digest({}),
        ),
    )
    return ledger.claim(task_id, account, {"five_hour": 1, "weekly": 1})


def finish(ledger, aid, gen):
    ledger.start(aid, gen)
    receipt = dict(
        status="completed",
        finish_reason="stop",
        actual_model="m1",
        manifest_sha256=digest({}),
        artifacts=[{"path": "a.txt", "sha256": "a" * 64}],
    )
    ledger.finish(aid, gen, receipt)
    return receipt


def test_lane_counts_split_completed_failed_abandoned(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    finish(ledger, aid, gen)  # completed, not accepted

    aid2, gen2 = submit_and_claim(ledger, "t2")
    ledger.start(aid2, gen2)
    ledger.hold(aid2, "bad output")
    ledger.resolve(aid2, "consumed", "provider ran", "james")  # failed

    aid3, gen3 = submit_and_claim(ledger, "t3")
    ledger.start(aid3, gen3)
    ledger.hold(aid3, "ambiguous")
    ledger.resolve(aid3, "released", "nothing ran", "james")  # abandoned

    out = report(ledger, now=now + 10)
    assert out["lanes"] == [{"lane": "fam/m1", "completed": 1, "failed": 1, "abandoned": 1}]


def test_lanes_map_labels_rows_by_lane_id_when_supplied(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    finish(ledger, aid, gen)
    out = report(
        ledger,
        lanes={"go": {"family": "fam", "model": "m1"}},
        accounts_by_lane={"go": "acct"},
        now=now + 10,
    )
    assert out["lanes"] == [{"lane": "go", "completed": 1, "failed": 0, "abandoned": 0}]


def test_lanes_without_a_resolvable_account_fall_back_to_family_model(ledger):
    # No accounts_by_lane entry for "go": the lane can't be disambiguated from another
    # lane on the same model, so the row is labeled family/model instead of merging
    # silently into whatever lane_id happens to match.
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    finish(ledger, aid, gen)
    out = report(ledger, lanes={"go": {"family": "fam", "model": "m1"}}, now=now + 10)
    assert out["lanes"] == [{"lane": "fam/m1", "completed": 1, "failed": 0, "abandoned": 0}]


def test_two_lanes_on_the_same_model_do_not_merge(ledger):
    # Exactly the shape B4's quota tiebreak exists for: one model, two accounts. Without
    # per-account disambiguation these would collapse into a single "fam/m1" row.
    ledger.configure_account(
        "acct2", 1, {"five_hour": 5, "weekly": 5}, time.time() + 600, ["m1"]
    )
    now = time.time()
    aid1, gen1 = submit_and_claim(ledger, "t1", account="acct")
    finish(ledger, aid1, gen1)
    aid2, gen2 = submit_and_claim(ledger, "t2", account="acct2")
    finish(ledger, aid2, gen2)

    out = report(
        ledger,
        lanes={
            "go": {"family": "fam", "model": "m1"},
            "go-2": {"family": "fam", "model": "m1"},
        },
        accounts_by_lane={"go": "acct", "go-2": "acct2"},
        now=now + 10,
    )
    assert sorted(out["lanes"], key=lambda e: e["lane"]) == [
        {"lane": "go", "completed": 1, "failed": 0, "abandoned": 0},
        {"lane": "go-2", "completed": 1, "failed": 0, "abandoned": 0},
    ]


def test_reviews_performed_vs_rejected(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "review-approved")
    receipt = finish(ledger, aid, gen)
    ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")

    aid2, gen2 = submit_and_claim(ledger, "review-blocked")
    finish(ledger, aid2, gen2)  # completed but never accepted: rejected

    # A non-review attempt never counts as a review either way.
    aid3, gen3 = submit_and_claim(ledger, "copy-ok")
    receipt3 = finish(ledger, aid3, gen3)
    ledger.accept(aid3, digest(receipt3), "operator-attested-independent", "approved")

    out = report(ledger, now=now + 10)
    assert out["reviews"] == {"performed": 2, "rejected": 1}


def test_held_and_resolved_carry_the_resolver(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    ledger.start(aid, gen)
    ledger.hold(aid, "still stuck")

    aid2, gen2 = submit_and_claim(ledger, "t2")
    ledger.start(aid2, gen2)
    ledger.hold(aid2, "ambiguous timeout")
    ledger.resolve(aid2, "released", "confirmed nothing ran", "james")

    out = report(ledger, now=now + 10)
    assert [h["attempt"] for h in out["held"]] == [aid]
    assert out["held"][0]["reason"] == "still stuck"
    assert len(out["resolved"]) == 1
    assert out["resolved"][0] == {
        "attempt": aid2,
        "operator": "james",
        "outcome": "released",
        "reason": "confirmed nothing ran",
    }


def test_cost_is_price_over_landed_packets_and_none_without_a_price(ledger):
    now = time.time()
    for name in ("t1", "t2"):
        aid, gen = submit_and_claim(ledger, name)
        receipt = finish(ledger, aid, gen)
        ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")

    out = report(ledger, prices={"acct": 14.0}, now=now + 10)
    assert out["cost_per_landed_packet_usd"] == {"acct": 7.0}

    out = report(ledger, now=now + 10)
    assert out["cost_per_landed_packet_usd"] == {}

    out = report(ledger, prices={"acct": 14.0, "unused-acct": 10.0}, now=now + 10)
    assert out["cost_per_landed_packet_usd"] == {"acct": 7.0, "unused-acct": None}


def test_prices_keyed_by_alias_resolve_to_the_primary_account(ledger):
    # attempts.account is always the primary id (ledger.claim() resolves the alias
    # before recording it); an operator's prices map, like accounts_by_lane, is
    # naturally keyed however they think of the account — alias included.
    ledger.configure_account(
        "acct3",
        1,
        {"five_hour": 5, "weekly": 5},
        time.time() + 600,
        ["m1"],
        alias_names=["acct3-alias"],
    )
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1", account="acct3-alias")
    receipt = finish(ledger, aid, gen)
    ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")

    out = report(ledger, prices={"acct3-alias": 10.0}, now=now + 10)
    assert out["cost_per_landed_packet_usd"] == {"acct3-alias": 10.0}


def test_outcomes_outside_the_window_are_not_counted(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    finish(ledger, aid, gen)
    # A week that ended long before this attempt finished sees nothing.
    out = report(ledger, week=None, now=now - 30 * 86400)
    assert out["lanes"] == []


def test_week_bounds_reads_an_iso_week():
    start, end = week_bounds("2026-W01", now=0)
    assert end - start == 7 * 86400
    # 2026-W01 starts on the Monday nearest the year — verify it's a Monday in UTC.
    import datetime

    assert datetime.datetime.fromtimestamp(start, datetime.timezone.utc).weekday() == 0


def test_week_bounds_refuses_a_malformed_week(ledger):
    with pytest.raises(ValueError):
        week_bounds("", now=0)
    with pytest.raises(ValueError):
        week_bounds("not-a-week", now=0)
    with pytest.raises(ValueError):
        report(ledger, week="")
