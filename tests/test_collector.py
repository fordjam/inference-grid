import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from inference_grid.ledger import Ledger, Refused
from inference_grid.collector import (
    collect,
    initialize_collections,
    CollectionResponse as R,
    collection_claims,
)


@pytest.fixture
def ledger(tmp_path):
    store = Ledger("sqlite:///" + str(tmp_path / "db"))
    store.initialize()
    initialize_collections(store)
    store.configure_account("a", 1, {"weekly": 10}, time.time() + 300, ["x"], ["alias"])
    return store


def test_existing_usage_block_never_calls(ledger):
    ledger.defer("a", "usage", time.time() + 200)
    assert collect(ledger, "alias", lambda **kw: pytest.fail("called"))["status"] == "cooldown"


def test_429_preserves_longer_deadline_and_original_time(ledger, monkeypatch):
    original = time.time()
    long = original + 200

    def request(**kw):
        ledger.defer("a", "usage", long)
        return R(429, retry_after="30", received_at=original)

    monkeypatch.setattr("inference_grid.collector.time.time", lambda: original)
    assert collect(ledger, "alias", request)["until"] == long
    assert ledger.cooldown_status("a", "usage")["until"] == long


def test_fallback_malformed_and_auth_sanitization(ledger):
    now = time.time()
    r = collect(ledger, "a", lambda **kw: R(429, retry_after="secret-header"))
    assert r["until"] >= now + 60
    assert "secret" not in str(r)


def test_auth_and_unvalidated_success(ledger):
    assert collect(ledger, "a", lambda **kw: R(401, observation={"secret": "token"})) == {
        "status": "auth_required",
        "observation": None,
    }
    assert collect(ledger, "a", lambda **kw: R(200)) == {
        "status": "response_ok_unvalidated",
        "observation": None,
    }
    r = collect(ledger, "a", lambda **kw: R(200, observation={"secret": "token"}))
    assert r == {"status": "invalid_observation", "observation": None}


def test_normalized_observation_returned_without_extra_fields(ledger):
    raw = {
        "provider": "claude",
        "observed_at": "2026-09-12T14:00:00Z",
        "windows": [{"id": "weekly", "used_percent": 40, "cookie": "secret"}],
        "cookie": "secret",
    }
    r = collect(ledger, "a", lambda **kw: R(200, observation=raw))
    assert r["status"] == "observation"
    assert r["observation"]["windows"] == [
        {"id": "weekly", "used_percent": 40.0, "resets_at": None}
    ]
    assert "secret" not in str(r)
    future = {**raw, "observed_at": "2999-01-01T00:00:00Z"}
    assert (
        collect(ledger, "a", lambda **kw: R(200, observation=future))["status"]
        == "invalid_observation"
    )


def test_error_not_leaked(ledger):
    def fail(**kw):
        raise RuntimeError("secret token")

    assert collect(ledger, "a", fail) == {"status": "collector_error", "observation": None}


def test_unknown_account_no_call(ledger):
    with pytest.raises(Refused):
        collect(ledger, "no", lambda **kw: pytest.fail("called"))


def test_alias_concurrent_single_flight(ledger):
    entered = threading.Event()
    release = threading.Event()

    def request(**kw):
        entered.set()
        release.wait(3)
        return R(200)

    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(collect, ledger, "a", request)
        assert entered.wait(2)
        try:
            assert collect(ledger, "alias", lambda **kw: pytest.fail("called"))["status"] == "busy"
        finally:
            release.set()
        assert future.result()["status"] == "response_ok_unvalidated"


def test_expired_claim_does_not_takeover(ledger):
    with ledger.tx() as c:
        c.execute(
            collection_claims.insert().values(
                account="a", token="old", deadline=0, last_status="collecting"
            )
        )
    assert (
        collect(ledger, "alias", lambda **kw: pytest.fail("called"))["status"]
        == "reconciliation_required"
    )


def test_late_success_rejected(ledger, monkeypatch):
    now = time.time()
    clock = [now]
    monkeypatch.setattr("inference_grid.collector.time.time", lambda: clock[0])

    def request(**kw):
        clock[0] = now + 40
        return R(200)

    assert collect(ledger, "a", request)["status"] == "deadline_exceeded"


def test_failed_429_persistence_holds_claim(ledger, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError("secret db")

    monkeypatch.setattr(ledger, "defer", fail)
    assert collect(ledger, "a", lambda **kw: R(429))["status"] == "reconciliation_required"
    assert collect(ledger, "alias", lambda **kw: pytest.fail("called"))["status"] == "busy"


def test_success_cannot_clear_concurrent_cooldown(ledger):
    until = time.time() + 200

    def request(**kw):
        ledger.defer("a", "usage", until)
        return R(200)

    collect(ledger, "a", request)
    assert ledger.cooldown_status("a", "usage")["until"] == until


def test_unknown_and_provider_error_preserved(ledger):
    assert collect(
        ledger, "a", lambda **kw: R(200, observation={"status": "unknown", "token": "secret"})
    ) == {"status": "unknown", "observation": None}
    assert collect(
        ledger, "a", lambda **kw: R(200, observation={"status": "error", "error": "secret"})
    ) == {"status": "provider_error", "observation": None}


def test_overflow_wait_keeps_claim_instead_of_shortening(ledger):
    result = collect(ledger, "a", lambda **kw: R(429, retry_after="9" * 400))
    assert result["status"] == "reconciliation_required"
    assert ledger.cooldown_status("a", "usage")["until"] is None
    assert collect(ledger, "alias", lambda **kw: pytest.fail("must not repeat"))["status"] == "busy"


@pytest.mark.parametrize("received", [float("nan"), float("inf"), -1, 10**400])
def test_invalid_429_timestamp_cannot_clear_claim(ledger, received):
    result = collect(ledger, "a", lambda **kw: R(429, retry_after="60", received_at=received))
    assert result["status"] == "reconciliation_required"
    assert collect(ledger, "alias", lambda **kw: pytest.fail("must not repeat"))["status"] == "busy"


def observation(offset=0, **changes):
    from datetime import datetime, timezone

    raw = {
        "provider": "opencode",
        "observed_at": datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat(),
        "windows": [{"id": "weekly", "used_percent": 25}, {"id": "monthly", "used_percent": None}],
        "cookie": "secret",
    }
    raw.update(changes)
    return raw


def test_publish_observation_updates_capacity(ledger):
    from sqlalchemy import select

    from inference_grid.collector import publish_observation
    from inference_grid.ledger import accounts

    plan = {"units": {"weekly": 40}, "models": ["m"]}
    raw = observation()
    result = publish_observation(ledger, "alias", raw, plan)
    assert result["status"] == "published" and result["windows"] == {"weekly": 30.0}
    assert result["account"] == "a" and "secret" not in str(result)
    with ledger.engine.connect() as con:
        row = con.execute(select(accounts).where(accounts.c.id == "a")).mappings().one()
    assert row["windows"] == {"weekly": 30.0} and row["models"] == ["m"]
    assert abs(row["expires"] - (time.time() + 900)) < 5
    # Exact replay is accepted by the ledger; an older observation is refused.
    assert publish_observation(ledger, "a", raw, plan)["status"] == "published"
    assert publish_observation(ledger, "a", observation(-60), plan)["status"] == "refused"


def test_publish_observation_refuses_unknown_usage_and_stale(ledger):
    from inference_grid.collector import publish_observation

    unknown = publish_observation(
        ledger, "a", observation(), {"units": {"monthly": 60}, "models": ["m"]}
    )
    assert unknown["status"] == "refused" and "unknown" in unknown["reason"]
    stale = publish_observation(
        ledger, "a", observation(-1000), {"units": {"weekly": 40}, "models": ["m"]}
    )
    assert stale["status"] == "stale"
    invalid = publish_observation(
        ledger, "a", {"secret": "x"}, {"units": {"weekly": 40}, "models": ["m"]}
    )
    assert invalid["status"] == "refused" and "secret" not in invalid["reason"]
    with pytest.raises(Refused):
        publish_observation(ledger, "a", observation(), {"units": {"weekly": 40}})
    with pytest.raises(Refused):
        publish_observation(
            ledger, "missing", observation(), {"units": {"weekly": 40}, "models": ["m"]}
        )


def test_refresh_collect_reports_per_provider_without_leaking(ledger, tmp_path):
    import json

    from inference_grid.collector import refresh_collect

    good = tmp_path / "good.json"
    good.write_text(json.dumps(observation()))
    older = tmp_path / "older.json"
    older.write_text(json.dumps(observation(-60)))  # out of order once "good" is published
    broken = tmp_path / "broken.json"
    broken.write_text("{secret: DO_NOT_SHARE")
    plan = {"units": {"weekly": 40}, "models": ["m"]}
    ledger.defer("a", "usage", time.time() + 120)
    config = {
        "providers": [
            {"alias": "alias", "provider": "opencode", "observation_path": str(good), "plan": plan},
            {"alias": "a", "provider": "codex", "observation_path": str(older), "plan": plan},
            {"alias": "a", "provider": "claude", "observation_path": str(broken), "plan": plan},
            {
                "alias": "a",
                "provider": "clinepass",
                "observation_path": str(tmp_path / "none"),
                "plan": plan,
            },
        ]
    }
    out = refresh_collect(ledger, config)
    statuses = {r["provider"]: r["status"] for r in out["providers"]}
    # The usage cooldown on the shared account wins for every alias; publication still happened.
    assert statuses == {
        "opencode": "cooldown",
        "codex": "cooldown",
        "claude": "cooldown",
        "clinepass": "cooldown",
    }
    assert all(r["next_eligible_at"] for r in out["providers"])
    assert "DO_NOT_SHARE" not in json.dumps(out) and "secret" not in json.dumps(out)
    ledger.defer("a", "usage", 0)
    with ledger.tx() as con:
        from sqlalchemy import update

        from inference_grid.ledger import cooldowns

        con.execute(update(cooldowns).values(until=0))
    statuses = {r["provider"]: r["status"] for r in refresh_collect(ledger, config)["providers"]}
    assert statuses == {
        "opencode": "ok",
        "codex": "error",
        "claude": "unknown",
        "clinepass": "unknown",
    }
    with pytest.raises(Refused):
        refresh_collect(
            ledger,
            {
                "providers": [
                    {"provider": "x", "alias": "a", "observation_path": "relative", "plan": plan}
                ]
            },
        )
    with pytest.raises(Refused):
        refresh_collect(ledger, {"providers": "x"})
