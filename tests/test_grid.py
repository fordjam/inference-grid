import hashlib
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import select, update

from inference_grid.ledger import Ledger, Refused, accounts, digest, outbox
from inference_grid.queue import hold_abandoned, publish
from inference_grid.receipts import validate_receipt
from inference_grid.worker import execute


@pytest.fixture
def grid(tmp_path):
    # Same tests can be run against a disposable PostgreSQL database in CI.
    url = os.environ.get("GRID_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger = Ledger(url)
    ledger.initialize()
    prefix = uuid.uuid4().hex
    ledger.configure_account(
        prefix,
        1,
        {"five_hour": 10, "weekly": 20},
        time.time() + 300,
        ["synthetic"],
        [prefix + "-alias"],
    )
    return ledger, prefix, tmp_path


def submit(grid, suffix="one", **changes):
    ledger, account, tmp = grid
    spec = dict(
        authorized=True,
        model="synthetic",
        family="example",
        argv=[
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "examples/synthetic_provider.py"),
        ],
        workspace=str(tmp / suffix),
        timeout=3,
        output_bytes=10000,
        inputs={},
        manifest_sha256=digest({}),
    )
    spec.update(changes)
    task = account + suffix
    ledger.submit(task, "public-example", spec)
    return task


def claim(grid, task, alias=False):
    ledger, account, _ = grid
    return ledger.claim(task, account + ("-alias" if alias else ""), {"five_hour": 2, "weekly": 2})


def receipt():
    return dict(
        status="completed",
        finish_reason="stop",
        actual_model="synthetic",
        manifest_sha256=digest({}),
        artifacts=[{"path": "a.txt", "sha256": "a" * 64}],
    )


def test_aliases_share_concurrency(grid):
    a, b = submit(grid), submit(grid, "two")
    claim(grid, a)
    with pytest.raises(Refused, match="account busy"):
        claim(grid, b, True)


def test_competing_claims(grid):
    a, b = submit(grid), submit(grid, "two")

    def run(task):
        try:
            return claim(grid, task)
        except Refused:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [a, b]))
    assert sum(r is not None for r in results) == 1


def test_duplicate_start_and_stale_generation(grid):
    aid, gen = claim(grid, submit(grid))
    ledger = grid[0]
    assert ledger.start(aid, gen + 1) is None
    assert ledger.start(aid, gen)
    assert ledger.start(aid, gen) is None
    with pytest.raises(Refused):
        ledger.finish(aid, gen + 1, receipt())
    ledger.hold(aid, "ambiguous timeout")
    with pytest.raises(Refused):
        ledger.finish(aid, gen, receipt())
    with pytest.raises(Refused):
        claim(grid, submit(grid, "two"))


def test_all_windows_and_budget(grid):
    task = submit(grid)
    with pytest.raises(Refused):
        grid[0].claim(task, grid[1], {"weekly": 1})
    with pytest.raises(Refused):
        grid[0].claim(task, grid[1], {"five_hour": 11, "weekly": 1})


def test_expired_before_start(grid):
    aid, gen = claim(grid, submit(grid))
    with grid[0].tx() as con:
        con.execute(update(accounts).where(accounts.c.id == grid[1]).values(expires=0))
    assert grid[0].start(aid, gen) is None
    assert next(r for r in grid[0].status() if r["id"] == aid)["state"] == "held"


def test_outbox_survives_publish_failure_and_duplicate_delivery(grid):
    aid, gen = claim(grid, submit(grid))

    def fail(*args):
        raise ConnectionError("broker unavailable")

    with pytest.raises(ConnectionError):
        publish(grid[0], fail)
    with grid[0].engine.connect() as con:
        assert con.execute(select(outbox.c.sent).where(outbox.c.attempt == aid)).scalar() is None
    delivered = []
    publish(grid[0], lambda a, g: delivered.append((a, g)))
    assert (aid, gen) in delivered
    assert execute(grid[0], aid, gen) == "completed"
    assert execute(grid[0], aid, gen) == "duplicate_or_stale"


def test_complete_is_not_accepted(grid):
    aid, gen = claim(grid, submit(grid))
    assert execute(grid[0], aid, gen) == "completed"
    row = next(r for r in grid[0].status() if r["id"] == aid)
    with pytest.raises(Refused):
        grid[0].accept(aid, digest(row["receipt"]), "example", "approved")
    with pytest.raises(Refused):
        grid[0].accept(aid, "changed", "independent", "approved")
    grid[0].accept(aid, digest(row["receipt"]), "operator-attested-independent", "approved")
    assert next(r for r in grid[0].status() if r["id"] == aid)["state"] == "accepted"


@pytest.mark.parametrize(
    "change",
    [
        {"finish_reason": "length"},
        {"actual_model": "fallback"},
        {"status": "running"},
        {"manifest_sha256": "bad"},
        {"artifacts": []},
        {"artifacts": [{"path": "../secret", "sha256": "a" * 64}]},
        {"artifacts": [{"path": "/secret", "sha256": "a" * 64}]},
        {
            "artifacts": [
                {"path": "a", "sha256": "a" * 64},
                {"path": "a", "sha256": "a" * 64},
            ]
        },
    ],
)
def test_receipt_rejections(change):
    value = receipt()
    value.update(change)
    assert validate_receipt(value, "synthetic", digest({}))


def test_changed_input_is_held(grid):
    source = grid[2] / "source"
    source.mkdir()
    (source / "a").write_text("changed")
    manifest = {"a": hashlib.sha256(b"original").hexdigest()}
    aid, gen = claim(
        grid,
        submit(
            grid,
            inputs=manifest,
            manifest_sha256=digest(manifest),
            input_root=str(source),
        ),
    )
    assert execute(grid[0], aid, gen) == "held"


@pytest.mark.parametrize(
    "code",
    [
        "import time; time.sleep(5)",
        'print("x"*20000)',
        "raise SystemExit(1)",
        'print("not json")',
    ],
)
def test_bounded_failures_hold(grid, code):
    aid, gen = claim(grid, submit(grid, argv=[sys.executable, "-c", code], timeout=1))
    assert execute(grid[0], aid, gen) == "held"
    with pytest.raises(Refused):
        claim(grid, submit(grid, "two"))


def test_recovery_never_blindly_restarts(grid):
    aid, gen = claim(grid, submit(grid))
    grid[0].start(aid, gen)
    assert hold_abandoned(grid[0], time.time() + 1) >= 1
    assert grid[0].start(aid, gen) is None


def test_workspace_exclusion_across_accounts(grid):
    ledger, account, tmp = grid
    task = submit(grid)
    claim(grid, task)
    ledger.configure_account(
        account + "other",
        1,
        {"five_hour": 10, "weekly": 20},
        time.time() + 300,
        ["synthetic"],
    )
    second = submit(grid, "two", workspace=str(tmp / "one"))
    with pytest.raises(Refused, match="workspace busy"):
        ledger.claim(second, account + "other", {"five_hour": 2, "weekly": 2})


def test_scheduler_respects_priority_and_does_not_retry(grid):
    from inference_grid.scheduler import tick

    candidate = [{"account": grid[1], "estimate": {"five_hour": 2, "weekly": 2}}]
    low = submit(grid, "low", priority=100, candidates=candidate)
    high = submit(grid, "high", priority=1, candidates=candidate)
    results = tick(grid[0])
    mine = {r["task"]: r for r in results if r["task"] in (low, high)}
    assert mine[high]["state"] == "queued"
    assert mine[low]["state"] == "blocked"
    assert not any(r["task"] == high for r in tick(grid[0]))


@pytest.mark.parametrize("mode", ["symlink", "wronghash"])
def test_artifact_validation(grid, mode):
    code = """import json,sys,pathlib
r=json.load(sys.stdin)
p=pathlib.Path(r['output_directory'])/'result'
MODE
print(json.dumps(dict(status='completed',finish_reason='stop',actual_model=r['model'],manifest_sha256=r['manifest_sha256'],artifacts=[dict(path='result',sha256='a'*64)])))
"""
    code = code.replace(
        "MODE",
        "p.symlink_to('/etc/hosts')" if mode == "symlink" else "p.write_text('wrong')",
    )
    aid, gen = claim(grid, submit(grid, argv=[sys.executable, "-c", code]))
    assert execute(grid[0], aid, gen) == "held"


def test_finished_quota_remains_debited(grid):
    aid, gen = claim(grid, submit(grid))
    assert execute(grid[0], aid, gen) == "completed"
    with grid[0].engine.connect() as con:
        remaining = con.execute(select(accounts.c.windows).where(accounts.c.id == grid[1])).scalar()
    assert remaining == {"five_hour": 8, "weekly": 18}


def test_immutable_spec_and_input_paths(grid):
    submit(grid)
    with pytest.raises(Refused, match="immutable"):
        submit(grid, timeout=2)
    manifest = {"../outside": "a" * 64}
    with pytest.raises(Refused, match="manifest"):
        submit(grid, "invalid", inputs=manifest, manifest_sha256=digest(manifest))


def test_non_packet_timeout_is_bounded_at_an_hour(grid):
    with pytest.raises(Refused, match="bounded timeout"):
        submit(grid, timeout=3601)


def test_packet_spec_timeout_is_bounded_at_four_hours(grid):
    # A packet's admission timeout covers the gates and the re-entry rounds on top of
    # the agent's own budget; the bound grows only for packet-loop argv.
    argv = ["packet-loop:goat_cli", "A1", "task"]
    submit(grid, timeout=4 * 3600, argv=argv)
    with pytest.raises(Refused, match="bounded timeout"):
        submit(grid, "two", timeout=4 * 3600 + 1, argv=argv)


def test_quota_observation_replay_cannot_restore_spent_units(grid):
    ledger, account, _ = grid
    stamp = time.time()
    expires = stamp + 300
    ledger.configure_account(
        account,
        1,
        {"five_hour": 10, "weekly": 20},
        expires,
        ["synthetic"],
        observed_at=stamp,
    )
    aid, gen = claim(grid, submit(grid))
    assert execute(ledger, aid, gen) == "completed"
    ledger.configure_account(
        account,
        1,
        {"five_hour": 10, "weekly": 20},
        expires,
        ["synthetic"],
        observed_at=stamp,
    )
    with ledger.engine.connect() as con:
        assert (
            con.execute(select(accounts.c.windows).where(accounts.c.id == account)).scalar()[
                "weekly"
            ]
            == 18
        )
    with pytest.raises(Refused, match="out-of-order"):
        ledger.configure_account(
            account,
            1,
            {"five_hour": 10, "weekly": 20},
            expires,
            ["synthetic"],
            observed_at=stamp - 1,
        )
    with pytest.raises(Refused, match="identity"):
        ledger.configure_account(
            account,
            1,
            {"five_hour": 11, "weekly": 20},
            expires,
            ["synthetic"],
            observed_at=stamp,
        )


@pytest.mark.parametrize("path", ["C:/outside", "D:relative", "nul\x00name"])
def test_portable_artifact_paths_reject_drive_and_nul(path):
    r = receipt()
    r["artifacts"][0]["path"] = path
    assert validate_receipt(r, "synthetic", digest({}))


def test_delayed_new_observation_cannot_erase_later_completion(grid):
    ledger, account, _ = grid
    sampled = time.time()
    expires = sampled + 300
    aid, gen = claim(grid, submit(grid))
    assert execute(ledger, aid, gen) == "completed"
    ledger.configure_account(
        account,
        1,
        {"five_hour": 10, "weekly": 20},
        expires,
        ["synthetic"],
        observed_at=sampled,
    )
    with ledger.engine.connect() as con:
        assert (
            con.execute(select(accounts.c.windows).where(accounts.c.id == account)).scalar()[
                "weekly"
            ]
            == 18
        )


@pytest.mark.parametrize("already_running", [False, True])
def test_refreshed_capacity_holds_queued_without_disturbing_running(grid, already_running):
    ledger, account, _ = grid
    ledger.configure_account(
        account, 2, {"five_hour": 10, "weekly": 20}, time.time() + 300, ["synthetic"]
    )
    first = claim(grid, submit(grid, "capacity-first"))
    second = claim(grid, submit(grid, "capacity-second"))
    if already_running:
        assert ledger.start(*first) is not None
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 300, ["synthetic"]
    )
    assert ledger.start(*second) is None
    if not already_running:
        assert ledger.start(*first) is None
    rows = {row["id"]: row for row in ledger.status()}
    assert rows[second[0]]["state"] == "held"
    assert "capacity changed" in rows[second[0]]["reason"]
    assert rows[first[0]]["state"] == ("dispatching" if already_running else "held")
    # Holds retain the reservations; a refresh must not silently release them.
    with pytest.raises(Refused, match="account busy"):
        claim(grid, submit(grid, "capacity-third"))


def test_cooldown_max_survives_alias_refresh_and_restart(grid):
    ledger, account, _ = grid
    deadline = time.time() + 200
    assert ledger.defer(account + "-alias", "inference", deadline)["until"] == deadline
    assert ledger.defer(account, "inference", deadline - 100)["until"] == deadline
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 300, ["synthetic"]
    )
    other = Ledger(ledger.engine.url)
    assert other.cooldown_status(account, "inference")["until"] == deadline
    assert other.cooldown_status(account + "-alias", "inference")["blocked"]
    with pytest.raises(Refused, match="cooldown"):
        claim(grid, submit(grid), alias=True)


def test_usage_cooldown_does_not_pause_inference(grid):
    ledger, account, _ = grid
    ledger.defer(account, "usage", time.time() + 200)
    aid, gen = claim(grid, submit(grid))
    assert ledger.start(aid, gen) is not None
    assert ledger.cooldown_status(account, "usage")["blocked"]
    assert not ledger.cooldown_status(account, "inference")["blocked"]


def test_new_cooldown_holds_queued_and_preserves_reservation(grid):
    ledger, account, _ = grid
    aid, gen = claim(grid, submit(grid))
    ledger.defer(account, "inference", time.time() + 200)
    assert ledger.start(aid, gen) is None
    row = next(r for r in ledger.status() if r["id"] == aid)
    assert row["state"] == "held"
    assert "cooldown" in row["reason"]
    assert ledger.start(aid, gen) is None


def test_cooldown_does_not_cancel_started_work(grid):
    ledger, account, _ = grid
    aid, gen = claim(grid, submit(grid))
    assert ledger.start(aid, gen) is not None
    ledger.defer(account, "inference", time.time() + 200)
    assert next(r for r in ledger.status() if r["id"] == aid)["state"] == "dispatching"


def test_expired_cooldown_allows_new_work_without_releasing_held(grid, monkeypatch):
    ledger, account, _ = grid
    now = time.time()
    aid, gen = claim(grid, submit(grid))
    ledger.defer(account, "inference", now + 10)
    assert ledger.start(aid, gen) is None
    monkeypatch.setattr("inference_grid.ledger.time.time", lambda: now + 20)
    assert not ledger.cooldown_status(account, "inference")["blocked"]
    with pytest.raises(Refused, match="account busy"):
        claim(grid, submit(grid, "another"))
    assert next(r for r in ledger.status() if r["id"] == aid)["state"] == "held"


def test_concurrent_cooldowns_keep_longest_deadline(grid):
    ledger, account, _ = grid
    now = time.time()

    def record(delay):
        return Ledger(ledger.engine.url).defer(account + "-alias", "inference", now + delay)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, [100, 300, 150, 250]))
    assert ledger.cooldown_status(account, "inference")["until"] == now + 300


@pytest.mark.parametrize("until", [True, -1, None, float("nan"), float("inf"), 10**400])
def test_invalid_cooldown_deadlines_refused(grid, until):
    ledger, account, _ = grid
    with pytest.raises(Refused):
        ledger.defer(account, "inference", until)
    assert ledger.cooldown_status(account, "inference")["until"] is None


def test_unknown_cooldown_account_and_endpoint_refused(grid):
    ledger, account, _ = grid
    with pytest.raises(Refused, match="unknown account"):
        ledger.defer(account + "-unknown", "inference", time.time() + 1)
    with pytest.raises(Refused, match="endpoint"):
        ledger.defer(account, "typo", time.time() + 1)


@pytest.mark.parametrize("first_operation", ["cooldown", "start"])
def test_cooldown_and_start_serialize_under_account_lock(grid, monkeypatch, first_operation):
    import threading

    ledger, account, _ = grid
    aid, gen = claim(grid, submit(grid))
    entered, release = threading.Event(), threading.Event()
    local = threading.local()
    original = ledger.lock

    def lock(con, keys):
        original(con, keys)
        if getattr(local, "first", False):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization timed out")

    monkeypatch.setattr(ledger, "lock", lock)

    def operation(name, first):
        local.first = first
        return (
            ledger.defer(account, "inference", time.time() + 100)
            if name == "cooldown"
            else ledger.start(aid, gen)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(operation, first_operation, True)
        try:
            assert entered.wait(5)
            second = pool.submit(
                operation, "start" if first_operation == "cooldown" else "cooldown", False
            )
        finally:
            release.set()
        first_result, second_result = first.result(timeout=10), second.result(timeout=10)
    started = first_result if first_operation == "start" else second_result
    assert (started is not None) == (first_operation == "start")
    state = next(r for r in ledger.status() if r["id"] == aid)["state"]
    assert state == ("dispatching" if first_operation == "start" else "held")


def test_resolve_released_frees_slot_without_debit(grid):
    ledger, account, tmp = grid
    task = submit(grid, argv=[sys.executable, "-c", "raise SystemExit(1)"], timeout=1)
    aid, gen = claim(grid, task)
    assert execute(ledger, aid, gen) == "held"
    with pytest.raises(Refused, match="account busy"):
        claim(grid, submit(grid, "two"))
    with pytest.raises(Refused, match="outcome"):
        ledger.resolve(aid, "retry", "please", "operator")
    with pytest.raises(Refused, match="reason"):
        ledger.resolve(aid, "released", "  ", "operator")
    with pytest.raises(Refused, match="operator"):
        ledger.resolve(aid, "released", "adapter exited before any provider call", "")
    result = ledger.resolve(
        aid,
        "released",
        "adapter exited before any provider call",
        "operator",
        evidence={"stderr_digest": "b" * 64},
    )
    assert result == {"attempt": aid, "state": "abandoned", "outcome": "released"}
    with ledger.engine.connect() as con:
        windows = con.execute(
            select(accounts.c.windows).where(accounts.c.id == account)
        ).scalar_one()
    assert windows == {"five_hour": 10, "weekly": 20}
    # The freed workspace and account slot admit new work; the resolved attempt stays terminal.
    claim(grid, submit(grid, "three", workspace=str(tmp / "one")))
    with pytest.raises(Refused, match="only held"):
        ledger.resolve(aid, "released", "again", "operator")


def test_resolve_consumed_debits_reservation(grid):
    ledger, account, _ = grid
    aid, gen = claim(
        grid, submit(grid, argv=[sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    )
    assert execute(ledger, aid, gen) == "held"
    assert (
        ledger.resolve(aid, "consumed", "native log shows a model request", "operator")["state"]
        == "failed"
    )
    with ledger.engine.connect() as con:
        windows = con.execute(
            select(accounts.c.windows).where(accounts.c.id == account)
        ).scalar_one()
    assert windows == {"five_hour": 8, "weekly": 18}
    claim(grid, submit(grid, "two"))


def test_resolve_refuses_non_held_states(grid):
    ledger, _, _ = grid
    aid, gen = claim(grid, submit(grid))
    with pytest.raises(Refused, match="only held"):
        ledger.resolve(aid, "released", "queued is not held", "operator")
    ledger.start(aid, gen)
    with pytest.raises(Refused, match="only held"):
        ledger.resolve(aid, "released", "dispatching is not held", "operator")
    with pytest.raises(Refused, match="unknown attempt"):
        ledger.resolve("missing", "released", "x", "operator")


def test_outcome_records_feed_scorecard_without_inventing_usage(grid):
    ledger, account, _ = grid
    aid, gen = claim(grid, submit(grid))
    with pytest.raises(Refused, match="terminal"):
        ledger.record_outcome(aid, "extraction", True)
    assert execute(ledger, aid, gen) == "completed"
    with pytest.raises(Refused):
        ledger.record_outcome(aid, "extraction", "yes")
    with pytest.raises(Refused):
        ledger.record_outcome(aid, "extraction", True, usage={"output": float("nan")})
    ledger.record_outcome(aid, "extraction", True, usage={"input": 300, "output": 1200}, repairs=0)
    held, gen2 = claim(
        grid, submit(grid, "two", argv=[sys.executable, "-c", "raise SystemExit(1)"], timeout=1)
    )
    assert execute(ledger, held, gen2) == "held"
    with pytest.raises(Refused, match="terminal"):
        ledger.record_outcome(held, "extraction", False)
    ledger.resolve(held, "released", "no provider call", "operator")
    with pytest.raises(Refused, match="completed work"):
        ledger.record_outcome(held, "extraction", True)
    ledger.record_outcome(held, "extraction", False, note="adapter exited early")
    card = ledger.scorecard(account=account)
    assert [
        (e["model"], e["category"], e["attempts"], e["completed"], e["accepted"], e["resolved"])
        for e in card
    ] == [("synthetic", "extraction", 2, 1, 1, 1)]
    assert card[0]["usage"] == {"input": 300, "output": 1200} and card[0]["usage_reported"] == 1
