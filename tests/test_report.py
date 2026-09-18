"""`inference-grid report --week`: per-lane outcome counts, reviews, holds and cost."""

import time

import pytest

from inference_grid.ledger import Ledger, digest
from inference_grid.report import render_markdown, report, week_bounds


@pytest.fixture
def ledger(tmp_path):
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    ledger.configure_account("acct", 2, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["m1"])
    return ledger


def submit_and_claim(ledger, task_id, account="acct", family="fam"):
    ledger.submit(
        task_id,
        "p",
        dict(
            authorized=True,
            model="m1",
            family=family,
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
    ledger.configure_account("acct2", 1, {"five_hour": 5, "weekly": 5}, time.time() + 600, ["m1"])
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
    # A review attempt's own ledger *state* is never accepted -- accept_reviewed()
    # (board/runner.py) calls ledger.accept() on the *source* packet attempt, never
    # the review attempt itself -- so the real verdict is the runner's own
    # record_outcome(aid, "independent_review", accepted, ...) call, the same one
    # every settled review gets in production.
    now = time.time()
    aid, gen = submit_and_claim(ledger, "review-approved")
    finish(ledger, aid, gen)
    ledger.record_outcome(aid, "independent_review", True)

    aid2, gen2 = submit_and_claim(ledger, "review-blocked")
    finish(ledger, aid2, gen2)
    ledger.record_outcome(aid2, "independent_review", False)

    # A non-review attempt never counts as a review either way.
    aid3, gen3 = submit_and_claim(ledger, "copy-ok")
    receipt3 = finish(ledger, aid3, gen3)
    ledger.accept(aid3, digest(receipt3), "operator-attested-independent", "approved")

    out = report(ledger, now=now + 10)
    assert out["reviews"] == {"performed": 2, "rejected": 1, "waived": 0}


def test_a_review_attempt_accepted_directly_is_not_counted_rejected_without_an_outcome(ledger):
    # Belt-and-suspenders for the same fact test_reviews_performed_vs_rejected checks:
    # even if a review- attempt somehow reached "accepted" directly (it structurally
    # shouldn't, but this report must never invent a rejection out of thin air), with
    # no outcome_recorded event on it, it counts as performed but not rejected.
    now = time.time()
    aid, gen = submit_and_claim(ledger, "review-x")
    receipt = finish(ledger, aid, gen)
    ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")
    out = report(ledger, now=now + 10)
    assert out["reviews"] == {"performed": 1, "rejected": 0, "waived": 0}


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


# --- 02-C1: packets landed, unattended-land rate, waived, Claude Max share ---


def test_packets_landed_counts_accepted_non_review_attempts(ledger):
    now = time.time()
    for name in ("t1", "t2"):
        aid, gen = submit_and_claim(ledger, name)
        receipt = finish(ledger, aid, gen)
        ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")
    aid3, gen3 = submit_and_claim(ledger, "t3")
    finish(ledger, aid3, gen3)  # completed, never accepted: not landed
    out = report(ledger, now=now + 10)
    assert out["packets_landed"] == 2


def test_a_review_attempt_accepted_directly_never_counts_as_a_packet_landed(ledger):
    # accept_reviewed() (board/runner.py) always calls ledger.accept() on the *source*
    # packet attempt, never the review attempt itself -- but if a review- id were ever
    # accepted directly (this fixture forces it), it still must not inflate the count.
    now = time.time()
    aid, gen = submit_and_claim(ledger, "review-x")
    receipt = finish(ledger, aid, gen)
    ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")
    out = report(ledger, now=now + 10)
    assert out["packets_landed"] == 0


def test_unattended_land_rate_is_none_without_any_landed_packet(ledger):
    assert report(ledger, now=time.time() + 10)["unattended_land_rate"] is None


def test_unattended_land_rate_is_full_when_no_operator_ever_touched_the_attempt(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    receipt = finish(ledger, aid, gen)
    ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")
    out = report(ledger, now=now + 10)
    assert out["unattended_land_rate"] == 1.0


def test_unattended_land_rate_counts_an_attempt_the_operator_touched_before_it_landed(ledger):
    now = time.time()
    # t1 lands untouched; t2 was held and resolved by a human before it went on to land.
    aid1, gen1 = submit_and_claim(ledger, "t1")
    receipt1 = finish(ledger, aid1, gen1)
    ledger.accept(aid1, digest(receipt1), "operator-attested-independent", "approved")

    aid2, gen2 = submit_and_claim(ledger, "t2")
    ledger.start(aid2, gen2)
    ledger.hold(aid2, "ambiguous")
    ledger.resolve(aid2, "consumed", "confirmed it ran", "james")
    # resolve(outcome="consumed") settles "failed", not accepted -- force accepted to
    # isolate exactly what's under test: an attempt an operator touched, that later
    # lands, must not count as unattended, regardless of how it got there.
    with ledger.tx() as con:
        from sqlalchemy import update

        from inference_grid.ledger import attempts as attempts_table

        con.execute(
            update(attempts_table).where(attempts_table.c.id == aid2).values(state="accepted")
        )

    out = report(ledger, now=now + 10)
    assert out["packets_landed"] == 2
    assert out["unattended_land_rate"] == 0.5


def test_claude_max_share_is_none_without_a_review_this_window(ledger):
    assert report(ledger, now=time.time() + 10)["claude_max_share"] is None


def test_claude_max_share_is_the_fraction_of_review_attempts_on_claude(ledger):
    now = time.time()
    aid1, gen1 = submit_and_claim(ledger, "review-1", family="claude")
    finish(ledger, aid1, gen1)
    aid2, gen2 = submit_and_claim(ledger, "review-2", family="go-kimi")
    finish(ledger, aid2, gen2)
    out = report(ledger, now=now + 10)
    assert out["claude_max_share"] == 0.5


def test_review_waived_events_are_counted_should_always_be_zero(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "review-1")
    finish(ledger, aid, gen)
    with ledger.tx() as con:
        ledger.event(con, aid, "review_waived", reason="test-only: this must never happen live")
    out = report(ledger, now=now + 10)
    assert out["reviews"]["waived"] == 1


# --- render_markdown: the page a human actually reads ---


def test_render_markdown_includes_every_required_line(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    receipt = finish(ledger, aid, gen)
    ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")
    aid2, gen2 = submit_and_claim(ledger, "t2")
    ledger.start(aid2, gen2)
    ledger.hold(aid2, "still stuck")
    out = report(ledger, prices={"acct": 14.0}, now=now + 10)
    document = render_markdown(out)
    assert "# Inference Grid — weekly report" in document
    assert "Packets landed: **1**" in document
    assert "Unattended-land rate: **100%**" in document
    assert "fam/m1" in document
    assert "still stuck" in document
    assert "$14.00" in document
    assert "Waived: **0**" in document


def test_report_command_writes_markdown_to_the_given_path_and_returns_it_too(ledger, tmp_path):
    from inference_grid.cli import report_command

    aid, gen = submit_and_claim(ledger, "t1")
    receipt = finish(ledger, aid, gen)
    ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")
    out_path = tmp_path / "report.md"
    document, written = report_command(ledger, out=str(out_path))
    assert written == out_path
    assert out_path.read_text() == document
    assert "Packets landed: **1**" in document


def test_report_command_defaults_the_log_path_to_library_logs_inference_grid(
    ledger, tmp_path, monkeypatch
):
    from inference_grid import cli

    # Point the default log dir at a fixture, never the operator's real ~/Library/Logs.
    monkeypatch.setattr(cli, "DEFAULT_REPORT_LOG_DIR", tmp_path / "Logs" / "inference-grid")
    document, written = cli.report_command(ledger, week="2026-W38")
    assert written == tmp_path / "Logs" / "inference-grid" / "report-2026-W38.md"
    assert written.read_text() == document


def test_report_command_reads_the_default_prices_file_when_present(ledger, tmp_path, monkeypatch):
    import json as jsonlib

    from inference_grid import cli

    prices_path = tmp_path / "prices.json"
    prices_path.write_text(jsonlib.dumps({"acct": 14.0}))
    monkeypatch.setattr(cli, "DEFAULT_PRICES_PATH", prices_path)
    aid, gen = submit_and_claim(ledger, "t1")
    receipt = finish(ledger, aid, gen)
    ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")
    document, _written = cli.report_command(ledger, out=str(tmp_path / "out.md"))
    assert "$14.00" in document


def test_report_command_ignores_a_malformed_default_prices_file(ledger, tmp_path, monkeypatch):
    # Valid JSON, wrong shape (a list, not {account: price}) -- must degrade to no
    # prices, never crash the whole command over one malformed config file.
    import json as jsonlib

    from inference_grid import cli

    prices_path = tmp_path / "prices.json"
    prices_path.write_text(jsonlib.dumps([1, 2, 3]))
    monkeypatch.setattr(cli, "DEFAULT_PRICES_PATH", prices_path)
    document, _written = cli.report_command(ledger, out=str(tmp_path / "out.md"))
    assert "no prices on file" in document


def test_report_command_refuses_a_non_markdown_out_path(ledger, tmp_path):
    from inference_grid.cli import report_command

    target = tmp_path / "not-markdown.txt"
    with pytest.raises(ValueError):
        report_command(ledger, out=str(target))
    assert not target.exists()


# --- 02-C2/C4/C5: numerator/denominator, failure rate, quota use, subscription cost ---


def test_unattended_land_count_is_the_numerator_beside_the_rate(ledger):
    now = time.time()
    aid1, gen1 = submit_and_claim(ledger, "t1")
    receipt1 = finish(ledger, aid1, gen1)
    ledger.accept(aid1, digest(receipt1), "operator-attested-independent", "approved")

    aid2, gen2 = submit_and_claim(ledger, "t2")
    ledger.start(aid2, gen2)
    ledger.hold(aid2, "ambiguous")
    ledger.resolve(aid2, "consumed", "confirmed it ran", "james")
    with ledger.tx() as con:
        from sqlalchemy import update

        from inference_grid.ledger import attempts as attempts_table

        con.execute(
            update(attempts_table).where(attempts_table.c.id == aid2).values(state="accepted")
        )

    out = report(ledger, now=now + 10)
    assert out["packets_landed"] == 2
    assert out["unattended_land_count"] == 1
    assert out["unattended_land_rate"] == 0.5


def test_unattended_land_count_is_none_without_any_landed_packet(ledger):
    assert report(ledger, now=time.time() + 10)["unattended_land_count"] is None


def test_failure_rate_per_lane_and_overall(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    finish(ledger, aid, gen)  # completed

    aid2, gen2 = submit_and_claim(ledger, "t2")
    ledger.start(aid2, gen2)
    ledger.hold(aid2, "bad output")
    ledger.resolve(aid2, "consumed", "provider ran", "james")  # failed

    aid3, gen3 = submit_and_claim(ledger, "t3")
    ledger.start(aid3, gen3)
    ledger.hold(aid3, "ambiguous")
    ledger.resolve(aid3, "released", "nothing ran", "james")  # abandoned

    out = report(ledger, now=now + 10)
    assert out["failure_rate_by_lane"] == {
        "fam/m1": {"failed_or_abandoned": 2, "attempts": 3, "rate": 2 / 3}
    }
    assert out["failure_rate_overall"] == {
        "failed_or_abandoned": 2,
        "attempts": 3,
        "rate": 2 / 3,
        "baseline_2026_09_17": 0.176,
    }


def test_failure_rate_overall_is_none_with_zero_attempts(ledger):
    out = report(ledger, now=time.time() + 10)
    assert out["failure_rate_by_lane"] == {}
    assert out["failure_rate_overall"] == {
        "failed_or_abandoned": 0,
        "attempts": 0,
        "rate": None,
        "baseline_2026_09_17": 0.176,
    }


def test_quota_use_is_a_sorted_passthrough_of_the_argument(ledger):
    out = report(
        ledger,
        quota_use={
            "Z.ai": {
                "used_percent": 100,
                "numerator": 10000,
                "denominator": 10000,
                "window": "weekly",
                "observed_at": "2026-09-18T14:35:10Z",
            },
            "Go": {"used_percent": 92.3, "window": "weekly"},
        },
        now=time.time() + 10,
    )
    assert out["quota_use"] == [
        {
            "subscription": "Go",
            "used_percent": 92.3,
            "numerator": None,
            "denominator": None,
            "window": "weekly",
            "observed_at": None,
        },
        {
            "subscription": "Z.ai",
            "used_percent": 100,
            "numerator": 10000,
            "denominator": 10000,
            "window": "weekly",
            "observed_at": "2026-09-18T14:35:10Z",
        },
    ]


def test_quota_use_is_empty_without_the_argument(ledger):
    assert report(ledger, now=time.time() + 10)["quota_use"] == []


def test_subscription_cost_per_landed_packet(ledger):
    now = time.time()
    for name in ("t1", "t2"):
        aid, gen = submit_and_claim(ledger, name)
        receipt = finish(ledger, aid, gen)
        ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")

    out = report(
        ledger,
        subscriptions={
            "Z.ai": {"account": "acct", "weekly_cost_usd": 14.0 / (52 / 12)},
            "Codex": {"account": None, "weekly_cost_usd": 0.0},
            "Max": {"account": "no-such-account", "weekly_cost_usd": None},
        },
        now=now + 10,
    )
    zai = next(c for c in out["subscription_costs"] if c["subscription"] == "Z.ai")
    assert zai["landed_packets"] == 2
    assert zai["cost_per_landed_packet_usd"] == pytest.approx((14.0 / (52 / 12)) / 2)
    codex = next(c for c in out["subscription_costs"] if c["subscription"] == "Codex")
    assert codex == {
        "subscription": "Codex",
        "weekly_cost_usd": 0.0,
        "landed_packets": 0,
        "cost_per_landed_packet_usd": None,
    }
    mx = next(c for c in out["subscription_costs"] if c["subscription"] == "Max")
    assert mx == {
        "subscription": "Max",
        "weekly_cost_usd": None,
        "landed_packets": 0,
        "cost_per_landed_packet_usd": None,
    }


def test_subscription_cost_sums_landed_packets_across_a_list_of_accounts(ledger):
    now = time.time()
    ledger.configure_account(
        "zai-account", 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["m1"]
    )
    ledger.configure_account(
        "zcode-account", 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["m1"]
    )
    aid1, gen1 = submit_and_claim(ledger, "t1", account="zai-account")
    receipt1 = finish(ledger, aid1, gen1)
    ledger.accept(aid1, digest(receipt1), "operator-attested-independent", "approved")
    aid2, gen2 = submit_and_claim(ledger, "t2", account="zcode-account")
    receipt2 = finish(ledger, aid2, gen2)
    ledger.accept(aid2, digest(receipt2), "operator-attested-independent", "approved")

    out = report(
        ledger,
        subscriptions={"Z.ai": {"account": ["zai-account", "zcode-account"], "weekly_cost_usd": 3.23}},
        now=now + 10,
    )
    zai = next(c for c in out["subscription_costs"] if c["subscription"] == "Z.ai")
    assert zai["landed_packets"] == 2
    assert zai["cost_per_landed_packet_usd"] == pytest.approx(3.23 / 2)


def test_subscription_cost_is_empty_without_the_argument(ledger):
    assert report(ledger, now=time.time() + 10)["subscription_costs"] == []


def test_render_markdown_includes_c4_c5_sections(ledger):
    now = time.time()
    aid, gen = submit_and_claim(ledger, "t1")
    ledger.start(aid, gen)
    ledger.hold(aid, "bad output")
    ledger.resolve(aid, "consumed", "provider ran", "james")  # failed

    out = report(
        ledger,
        subscriptions={"Z.ai": {"account": "acct", "weekly_cost_usd": 3.23}},
        quota_use={"Z.ai": {"used_percent": 100, "window": "weekly"}},
        now=now + 10,
    )
    document = render_markdown(out)
    assert "## Failure rate (C4: failed + abandoned ÷ attempts)" in document
    assert "Overall: **1** of **1**" in document
    assert "baseline: 17.6%" in document
    assert "## Quota use per subscription (C5)" in document
    assert "100% (provider-reported; raw units unrecorded)" in document
    assert "## Subscription cost per landed packet (C5)" in document
    assert "$3.23" in document


def test_report_command_loads_default_subscriptions_and_quota_use(ledger, tmp_path, monkeypatch):
    from inference_grid import cli

    monkeypatch.setattr(cli, "DEFAULT_SUBSCRIPTIONS_PATH", tmp_path / "no-subscriptions.json")
    monkeypatch.setattr(cli, "DEFAULT_CAPACITY_OBSERVATIONS_DIR", tmp_path / "no-capacity-dir")
    now = time.time()
    for name in ("t1", "t2"):
        aid, gen = submit_and_claim(ledger, name, account="acct")
        receipt = finish(ledger, aid, gen)
        ledger.accept(aid, digest(receipt), "operator-attested-independent", "approved")
    document, _written = cli.report_command(ledger, out=str(tmp_path / "out.md"))
    # "acct" isn't any of the default subscriptions' accounts, so every default
    # subscription reports 0 landed packets and "unrecorded" cost -- never a guess.
    assert "Codex" in document
    assert "unrecorded (no price on file)" in document


def test_load_quota_use_reads_the_weekly_window_from_observation_files(tmp_path):
    import json as jsonlib

    from inference_grid.cli import _load_quota_use

    (tmp_path / "zai-observation.json").write_text(
        jsonlib.dumps(
            {
                "observed_at": "2026-09-18T14:35:10Z",
                "windows": [
                    {"id": "five_hour", "used_percent": 0},
                    {
                        "id": "weekly",
                        "used_percent": 100,
                        "remaining_units": 0,
                        "plan_units": 10000,
                    },
                ],
            }
        )
    )
    out = _load_quota_use(None, capacity_dir=tmp_path)
    assert out == {
        "Z.ai": {
            "used_percent": 100,
            "numerator": 10000,
            "denominator": 10000,
            "window": "weekly",
            "observed_at": "2026-09-18T14:35:10Z",
        }
    }


def test_load_quota_use_ignores_a_missing_directory(tmp_path):
    from inference_grid.cli import _load_quota_use

    assert _load_quota_use(None, capacity_dir=tmp_path / "does-not-exist") == {}


def test_load_subscriptions_converts_monthly_to_weekly_by_default(monkeypatch):
    from pathlib import Path

    from inference_grid import cli

    monkeypatch.setattr(cli, "DEFAULT_SUBSCRIPTIONS_PATH", Path("/no/such/file.json"))
    out = cli._load_subscriptions(None)
    assert out["Z.ai"]["account"] == ["zai", "zcode"]
    assert out["Z.ai"]["weekly_cost_usd"] == pytest.approx(14.0 / (52 / 12))
    assert out["Max"]["account"] is None
    assert out["Max"]["weekly_cost_usd"] is None
    assert out["Codex"]["weekly_cost_usd"] == 0.0


def test_report_week_label_uses_the_windows_start_not_its_end(ledger):
    # The Monday 07:00 scheduled run's own case: `end` (now) has already crossed into
    # the new ISO week, but the report's content is entirely the week before it -- the
    # filename must match the content, not the moment the report was generated.
    from inference_grid.cli import _report_week_label

    # A Monday 2026-09-21 07:00 UTC run, no --week: window is the 7 days ending then,
    # i.e. 2026-09-14 07:00 (still ISO week 38) through 2026-09-21 07:00 (ISO week 39).
    import datetime

    end = datetime.datetime(2026, 9, 21, 7, 0, tzinfo=datetime.timezone.utc).timestamp()
    out = report(ledger, now=end)
    assert out["week"] is None
    assert _report_week_label(out) == "2026-W38"


def test_render_markdown_never_crashes_on_an_empty_window():
    empty = {
        "week": None,
        "start": 0,
        "end": 0,
        "lanes": [],
        "reviews": {"performed": 0, "rejected": 0, "waived": 0},
        "held": [],
        "resolved": [],
        "packets_landed": 0,
        "unattended_land_rate": None,
        "unattended_land_count": None,
        "claude_max_share": None,
        "cost_per_landed_packet_usd": {},
        "failure_rate_by_lane": {},
        "failure_rate_overall": {
            "failed_or_abandoned": 0,
            "attempts": 0,
            "rate": None,
            "baseline_2026_09_17": 0.176,
        },
        "quota_use": [],
        "subscription_costs": [],
    }
    document = render_markdown(empty)
    assert "no attempts settled" in document
    assert "nothing held right now" in document
    assert "no prices on file" in document
    assert "no capacity observations on file" in document
    assert "no subscriptions configured" in document
