"""Real process boundaries; SQLite locally or disposable PostgreSQL in CI."""

import multiprocessing as mp
import os
import time
import uuid

from inference_grid.ledger import Ledger, Refused, digest, hold_abandoned


def competing_claim(url, task, account, ready, start, results):
    ledger = Ledger(url)
    ready.put(True)
    start.wait(10)
    try:
        ledger.claim(task, account, {"weekly": 1})
        results.put("admitted")
    except Refused:
        results.put("refused")


def crash_after_intent(url, aid, generation):
    ledger = Ledger(url)
    assert ledger.start(aid, generation)
    os._exit(23)


def setup(tmp_path):
    url = os.environ.get("GRID_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "process.sqlite"))
    ledger = Ledger(url)
    ledger.initialize()
    account = uuid.uuid4().hex
    ledger.configure_account(
        account,
        1,
        {"weekly": 10},
        time.time() + 120,
        ["synthetic"],
        [account + "alias"],
    )
    for name in ("a", "b"):
        ledger.submit(
            account + name,
            "process-test",
            dict(
                authorized=True,
                model="synthetic",
                family="example",
                argv=["unused"],
                workspace=str(tmp_path / name),
                timeout=1,
                output_bytes=1000,
                inputs={},
                manifest_sha256=digest({}),
            ),
        )
    return ledger, url, account


def test_two_processes_share_alias_limit(tmp_path):
    ledger, url, account = setup(tmp_path)
    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    results = ctx.Queue()
    start = ctx.Event()
    processes = [
        ctx.Process(
            target=competing_claim,
            args=(url, account + name, alias, ready, start, results),
        )
        for name, alias in [("a", account), ("b", account + "alias")]
    ]
    try:
        for p in processes:
            p.start()
        for _ in processes:
            assert ready.get(timeout=10)
        start.set()
        assert sorted(results.get(timeout=10) for _ in processes) == [
            "admitted",
            "refused",
        ]
        for p in processes:
            p.join(10)
            assert p.exitcode == 0
    finally:
        for p in processes:
            if p.is_alive():
                p.kill()
                p.join()


def test_process_death_after_dispatch_intent_never_restarts(tmp_path):
    ledger, url, account = setup(tmp_path)
    aid, generation = ledger.claim(account + "a", account, {"weekly": 1})
    p = mp.get_context("spawn").Process(target=crash_after_intent, args=(url, aid, generation))
    p.start()
    p.join(10)
    try:
        assert p.exitcode == 23
        assert Ledger(url).start(aid, generation) is None
        hold_abandoned(ledger, time.time() + 1)
        row = next(r for r in ledger.status() if r["id"] == aid)
        assert row["state"] == "held"
        try:
            ledger.claim(account + "b", account, {"weekly": 1})
        except Refused:
            pass
        else:
            raise AssertionError("ambiguous reservation was released")
    finally:
        if p.is_alive():
            p.kill()
            p.join()
