"""The plans' model catalogues: parsed by header, priced with promos, kept as a series."""

import json
from datetime import date

import pytest

from inference_grid import catalogue as C
from inference_grid.ledger import Ledger

GO_HTML = """
<h2>Pricing</h2>
<table><thead><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th><th>Monthly limit</th></tr></thead>
<tbody>
<tr><td>Kimi K3</td><td>$3.00</td><td>$15.00</td><td>$0.30</td><td>-</td><td>$15</td></tr>
<tr><td>Qwen3.8 Flash</td><td>$0.15</td><td>$0.47</td><td>$0.016</td><td>$0.20</td><td>$30</td></tr>
<tr><td>DeepSeek V4.1 Flash (Off-Peak)</td><td>$0.15</td><td>$0.60</td><td>$0.003</td><td>-</td><td><del>$15</del> <strong>$60</strong><br><small>4x · Ends Sep 20</small></td></tr>
<tr><td>DeepSeek V4.1 Flash (Peak)</td><td>$0.30</td><td>$1.20</td><td>$0.006</td><td>-</td><td><del>$15</del> <strong>$60</strong><br><small>4x · Ends Sep 20</small></td></tr>
<tr><td>Union Alpha Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td><td>Unlimited<br><small>limited time</small></td></tr>
<tr><td>Broken Row</td><td>n/a</td><td>$1.00</td><td>$0.10</td><td>-</td><td>$60</td></tr>
</tbody></table>
<table><thead><tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr></thead>
<tbody>
<tr><td>Kimi K3</td><td>kimi-k3</td><td>https://opencode.ai/zen/go/v1/chat/completions</td><td>x</td></tr>
<tr><td>Qwen3.8 Flash</td><td>qwen3.8-flash</td><td>https://opencode.ai/zen/go/v1/messages</td><td>x</td></tr>
<tr><td>DeepSeek V4.1 Flash</td><td>deepseek-v4.1-flash</td><td>https://opencode.ai/zen/go/v1/chat/completions</td><td>x</td></tr>
<tr><td>Union Alpha Free</td><td>union-alpha</td><td>https://opencode.ai/zen/go/v1/messages</td><td>x</td></tr>
</tbody></table>
<table><thead><tr><th>Model</th><th>Model training</th><th>Data retention</th></tr></thead>
<tbody>
<tr><td>Kimi K3</td><td>Not used</td><td>0 days</td></tr>
<tr><td>Qwen3.8 Flash</td><td>Not used</td><td>0 days</td></tr>
<tr><td>Broken Row</td><td>Yes</td><td>Not ZDR</td></tr>
</tbody></table>
<table><thead><tr><th>Client</th><th>Session support</th></tr></thead><tbody><tr><td>Hermes</td><td>yes</td></tr></tbody></table>
"""

CMD_TEXT = """Available models  ·  5 models

Open Source

z-ai/glm-5.3-flash                     fast, affordable GLM coding with 1M context
meituan/longcat-2.0:free               FREE trillion-parameter agentic coding
poolside/laguna-s-2.1-free             FREE open-weight agentic coding

Anthropic

claude-opus-5                          most intelligent Opus for agents and coding
"""


def test_go_table_joins_four_tables_by_display_name():
    rows = {r["model"]: r for r in C.parse_go_table(GO_HTML, year=2026)}
    assert set(rows) == {
        "kimi-k3",
        "qwen3.8-flash",
        "deepseek-v4.1-flash",
        "union-alpha",
        "broken-row",
    }
    kimi = rows["kimi-k3"]
    assert (kimi["input_per_m"], kimi["output_per_m"], kimi["cache_read_per_m"]) == (
        3.0,
        15.0,
        0.30,
    )
    assert kimi["monthly_limit_usd"] == 15.0 and kimi["retention"] == "zero"
    assert kimi["section"] == "chat/completions" and kimi["parse_error"] is None
    # The endpoint protocol is a fact of the model: Qwen speaks the Messages API here.
    assert rows["qwen3.8-flash"]["section"] == "messages"
    assert rows["qwen3.8-flash"]["cache_write_per_m"] == 0.20
    # A promo in the limit cell: multiplier, end date this year, the raised bucket, the base.
    ds = rows["deepseek-v4.1-flash"]
    assert ds["promo_multiplier"] == 4.0 and ds["promo_ends"] == "2026-09-20"
    assert ds["monthly_limit_usd"] == 60.0 and "base $15/month" in ds["promo"]
    # Peak/off-peak: one row, off-peak price kept, peak noted.
    assert ds["input_per_m"] == 0.15 and "Peak" in ds["tier_note"]
    # Free and unlimited.
    union = rows["union-alpha"]
    assert (
        union["free"] is True and union["input_per_m"] == 0.0 and union["monthly_limit_usd"] is None
    )
    assert union["promo"] == "limited time"
    # An unreadable price is recorded as None with the cell quoted, never dropped.
    broken = rows["broken-row"]
    assert broken["input_per_m"] is None and "input_per_m: 'n/a'" in broken["parse_error"]
    assert broken["retention"] == "not-zdr"


def test_cmd_list_models_reads_sections_and_free_tags():
    rows = {r["model"]: r for r in C.parse_cmd_models(CMD_TEXT)}
    assert set(rows) == {
        "z-ai/glm-5.3-flash",
        "meituan/longcat-2.0:free",
        "poolside/laguna-s-2.1-free",
        "claude-opus-5",
    }
    assert (
        rows["z-ai/glm-5.3-flash"]["section"] == "Open Source"
        and not rows["z-ai/glm-5.3-flash"]["free"]
    )
    assert rows["meituan/longcat-2.0:free"]["free"] and rows["poolside/laguna-s-2.1-free"]["free"]
    assert rows["claude-opus-5"]["section"] == "Anthropic"
    assert all(r["input_per_m"] is None for r in rows.values() if not r["free"])


def test_observed_rates_sum_command_code_session_logs(tmp_path):
    proj = tmp_path / "projects" / "some-cwd"
    proj.mkdir(parents=True)
    lines = [
        {
            "type": "assistant",
            "message": {
                "model": "z-ai/glm-5.3-flash",
                "usage": {
                    "inputTokens": 900_000,
                    "outputTokens": 100_000,
                    "cacheReadTokens": 0,
                    "costUsd": 0.2,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "model": "z-ai/glm-5.3-flash",
                "usage": {
                    "inputTokens": 500_000,
                    "outputTokens": 0,
                    "cacheReadTokens": 500_000,
                    "costUsd": 0.1,
                },
            },
        },
        {"type": "user", "text": "no usage here"},
    ]
    (proj / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    (proj / "s1.checkpoints.jsonl").write_text(json.dumps(lines[0]) + "\n")  # never read
    rates = C.observed_rates(tmp_path / "projects")
    glm = rates["z-ai/glm-5.3-flash"]
    assert glm["requests"] == 2 and glm["cost_usd"] == pytest.approx(0.3)
    # 2.0M tokens for $0.30 -> $0.15 per million, blended.
    assert glm["input_per_m"] == pytest.approx(0.15)
    joined = {r["model"]: r for r in C.command_code_rows(CMD_TEXT, tmp_path / "projects")}
    assert joined["z-ai/glm-5.3-flash"]["input_per_m"] == pytest.approx(0.15)
    assert joined["z-ai/glm-5.3-flash"]["source"] == "cmd-list-models+observed"
    assert joined["claude-opus-5"]["input_per_m"] is None  # never observed: no invented price


def test_cost_estimate_applies_promo_and_free_floor():
    priced = C.normalise(
        {"model": "m", "input_per_m": 1.0, "output_per_m": 10.0, "cache_read_per_m": 0.1}
    )
    assert C.cost_estimate(priced, 1_000_000, 100_000, 0) == pytest.approx(2.0)
    assert C.cost_estimate(priced, 0, 0, 1_000_000) == pytest.approx(0.1)
    promo = C.normalise({**priced, "promo_multiplier": 4.0, "promo_ends": "2026-09-20"})
    assert C.cost_estimate(promo, 1_000_000, 100_000, at=date(2026, 9, 16)) == pytest.approx(0.5)
    assert C.cost_estimate(promo, 1_000_000, 100_000, at=date(2026, 9, 21)) == pytest.approx(2.0)
    free = C.normalise({"model": "f", "free": True})
    assert C.cost_estimate(free, 10**9, 10**9) == C.FREE_COST_FLOOR_USD
    assert C.cost_estimate(C.normalise({"model": "u"}), 1000, 10) is None
    assert C.cost_estimate(None, 1, 1) is None


def test_normalise_refuses_junk():
    with pytest.raises(ValueError, match="unknown keys"):
        C.normalise({"model": "m", "price": 1})
    with pytest.raises(ValueError, match="non-negative"):
        C.normalise({"model": "m", "input_per_m": -1})
    with pytest.raises(ValueError, match="ISO date"):
        C.normalise({"model": "m", "promo_ends": "Sep 20"})
    with pytest.raises(ValueError, match="model id"):
        C.normalise({"model": ""})


def test_ledger_keeps_the_catalogue_as_a_series_and_reads_the_newest(tmp_path):
    ledger = Ledger("sqlite:///" + str(tmp_path / "l.sqlite"))
    ledger.initialize()
    first = [C.normalise({"model": "kimi-k3", "input_per_m": 3.0, "output_per_m": 15.0})]
    later = [
        C.normalise({"model": "kimi-k3", "input_per_m": 2.0, "output_per_m": 10.0}),
        C.normalise({"model": "qwen3.8-flash", "input_per_m": 0.15, "output_per_m": 0.47}),
    ]
    assert ledger.record_catalogue("opencode", first, observed_at=1000.0) == 1
    assert ledger.record_catalogue("opencode", later, observed_at=2000.0) == 2
    # Re-recording the same reading replaces, never refuses.
    assert ledger.record_catalogue("opencode", later, observed_at=2000.0) == 2
    now = {r["model"]: r for r in ledger.catalogue("opencode")}
    assert now["kimi-k3"]["input_per_m"] == 2.0 and now["qwen3.8-flash"]["provider"] == "opencode"
    then = {r["model"]: r for r in ledger.catalogue("opencode", at=1500.0)}
    assert then["kimi-k3"]["input_per_m"] == 3.0 and "qwen3.8-flash" not in then


def test_deals_name_what_using_a_promo_takes():
    rows = [
        dict(
            C.normalise(
                {
                    "model": "deepseek-v4.1-flash",
                    "input_per_m": 0.15,
                    "output_per_m": 0.6,
                    "promo_multiplier": 4.0,
                    "promo_ends": "2026-09-20",
                    "monthly_limit_usd": 60,
                }
            ),
            provider="opencode",
        ),
        dict(C.normalise({"model": "union-alpha", "free": True}), provider="opencode"),
        dict(
            C.normalise({"model": "meituan/longcat-2.0:free", "free": True}),
            provider="command-code",
        ),
        dict(
            C.normalise({"model": "kimi-k3", "input_per_m": 3.0, "output_per_m": 15.0}),
            provider="opencode",
        ),
        dict(
            C.normalise(
                {
                    "model": "old-promo",
                    "input_per_m": 1.0,
                    "output_per_m": 1.0,
                    "promo_multiplier": 2.0,
                    "promo_ends": "2026-09-01",
                }
            ),
            provider="opencode",
        ),
    ]
    lanes = {
        "go-deepseek": {"model": "deepseek-v4.1-flash"},
        "go-union-alpha": {"model": "union-alpha"},
    }
    readiness = {"go-deepseek": {"state": "ready"}, "go-union-alpha": {"state": "unqualified"}}
    out = C.deals(rows, lanes, readiness, at=date(2026, 9, 17))
    by = {d["model"]: d for d in out}
    assert set(by) == {
        "deepseek-v4.1-flash",
        "union-alpha",
        "meituan/longcat-2.0:free",
    }  # no expired promo, no plain price
    assert (
        by["deepseek-v4.1-flash"]["state"] == "qualified"
        and by["deepseek-v4.1-flash"]["days_left"] == 3
    )
    assert by["deepseek-v4.1-flash"]["ending_soon"] is True
    assert by["union-alpha"]["state"] == "lane unqualified: canary pending"
    assert by["meituan/longcat-2.0:free"]["state"] == "not a lane"
    lines = C.deals_lines(out)
    assert (
        lines[0].startswith("opencode/deepseek-v4.1-flash: 4x promo") and "ends in 3d" in lines[0]
    )
