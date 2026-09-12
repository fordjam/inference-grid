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
    ledger.configure_account(account, 2, {"five_hour": 10, "weekly": 20},
                             time.time() + 300, ["synthetic"])
    first = claim(grid, submit(grid, "capacity-first"))
    second = claim(grid, submit(grid, "capacity-second"))
    if already_running:
        assert ledger.start(*first) is not None
    ledger.configure_account(account, 1, {"five_hour": 10, "weekly": 20},
                             time.time() + 300, ["synthetic"])
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
