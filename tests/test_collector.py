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
    assert collect(ledger, "a", lambda **kw: R(200, observation={"secret": "token"})) == {
        "status": "response_ok_unvalidated",
        "observation": None,
    }


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
