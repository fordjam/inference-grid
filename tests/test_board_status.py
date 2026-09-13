"""Board status rows: state, review resolution and named attempts, without touching anything."""

import json
import time
import uuid

from inference_grid.board.status import board_status
from inference_grid.ledger import Ledger, digest


def write_task(board, tid, state, reason=None):
    task = dict(
        id=tid,
        category="pure_function",
        brief=f"grid/briefs/{tid}.txt",
        inputs=[f"grid/briefs/{tid}.txt"],
        tests=[],
        artifacts=["out.py"],
        lanes=["go"],
        author_family=None,
        budget={"wall_seconds": 60, "output_bytes": 1000, "thinking_tokens": None},
        state=state,
        blocked_reason=reason,
    )
    (board / f"{tid}.json").write_text(json.dumps(task))
    return task


def seed_attempt(ledger, alias, task_id, workspace):
    spec = {
        "authorized": True,
        "model": "glm-5.3-flash",
        "family": "glm",
        "argv": ["/usr/bin/true"],
        "workspace": str(workspace),
        "timeout": 60,
        "output_bytes": 1000,
        "inputs": {},
        "manifest_sha256": digest({}),
    }
    ledger.submit(task_id, "project", spec)
    return ledger.claim(task_id, alias, {"five_hour": 0.01, "weekly": 0.01})


def test_status_rows_resolve_reviews_and_attempts(tmp_path):
    board = tmp_path / "grid/board"
    board.mkdir(parents=True)
    write_task(board, "fresh", "ready")
    write_task(board, "waited-on", "review_pending")
    write_task(
        board,
        "stuck",
        "blocked",
        reason=f"attempt {uuid.uuid4()}" + " held; resolve with evidence",
    )
    write_task(board, "orphan", "review_pending")
    write_task(board, "review-waited-on", "ready")
    # A retry link: the live review of waited-on is named in its source link.
    stage = board / "review/waited-on"
    stage.mkdir(parents=True)
    (stage / "source.json").write_text(
        json.dumps({"task": "waited-on", "review_task": "review-waited-on-2"})
    )
    write_task(board, "review-waited-on-2", "dispatched")

    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    account = "go-" + uuid.uuid4().hex[:8]
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    aid, generation = seed_attempt(ledger, account, "seed-1", tmp_path / "ws")
    ledger.start(aid, generation)  # queued -> dispatching; only then can it be held
    ledger.hold(aid, "Refused: timeout: provider acceptance may be ambiguous")
    stuck = json.loads((board / "stuck.json").read_text())
    stuck["blocked_reason"] = f"attempt {aid} held; resolve with evidence"
    (board / "stuck.json").write_text(json.dumps(stuck))

    before = {p.name: p.read_bytes() for p in board.glob("*.json")}
    rows = {r["id"]: r for r in board_status(ledger, board)}
    assert rows["fresh"]["state"] == "ready" and "review" not in rows["fresh"]
    assert rows["waited-on"]["review"] == {"task": "review-waited-on-2", "state": "dispatched"}
    assert rows["orphan"]["review"] == {"task": "review-orphan", "state": "missing"}
    assert rows["stuck"]["attempt"]["id"] == aid
    assert rows["stuck"]["attempt"]["state"] == "held"
    assert rows["stuck"]["attempt"]["refusal"] is None  # no verdict beside this attempt
    assert rows["stuck"]["attempt"]["artifacts_present"] is False
    assert rows["fresh"]["blocked_reason"] is None
    # Read-only: every board file is byte-identical afterwards.
    assert {p.name: p.read_bytes() for p in board.glob("*.json")} == before


def test_reasons_are_bounded_and_a_dead_ledger_yields_unknown(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    write_task(
        board,
        "stuck",
        "blocked",
        reason="attempt 00000000-0000-0000-0000-000000000000 " + "x" * 200,
    )
    ledger = Ledger("sqlite:///" + str(tmp_path / "empty.sqlite"))
    rows = board_status(ledger, board)
    assert rows[0]["blocked_reason"].startswith("attempt 00000000")
    assert len(rows[0]["blocked_reason"]) == 80
    assert rows[0]["attempt"]["state"] == "unknown"
    assert rows[0]["attempt"]["refusal"] is None
    assert rows[0]["attempt"]["transport_timeout"] is None
    assert rows[0]["attempt"]["artifacts_present"] is False


def test_attempt_detail_reads_the_verdict_and_artifacts(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    write_task(
        board, "stuck", "blocked", reason="attempt 00000000-0000-0000-0000-000000000000 held"
    )
    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    account = "go-" + uuid.uuid4().hex[:8]
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    aid, generation = seed_attempt(ledger, account, "seed-1", tmp_path / "ws")
    ledger.start(aid, generation)
    ledger.hold(aid, "Refused: adapter exited without a successful terminal receipt")
    stuck = json.loads((board / "stuck.json").read_text())
    stuck["blocked_reason"] = f"attempt {aid} held; resolve with evidence"
    (board / "stuck.json").write_text(json.dumps(stuck))
    # A transport-timeout verdict and one artifact beside the attempt; the attempt
    # directory is the worker's creation, so the test stages it here.
    attempt_dir = tmp_path / "ws" / aid
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "verdict.json").write_text(
        json.dumps(
            {
                "refusal": "transport_error: TimeoutError",
                "transport_timeout": 400,
                "task_wall_seconds": 600,
                "lane_wall_seconds": 400,
            }
        )
    )
    (attempt_dir / "artifacts").mkdir()
    (attempt_dir / "artifacts" / "reply.txt").write_text("partial")
    before = (attempt_dir / "verdict.json").read_bytes()
    rows = board_status(ledger, board)
    attempt = rows[0]["attempt"]
    assert attempt == {
        "id": aid,
        "state": "held",
        "refusal": "transport_error: TimeoutError",
        "transport_timeout": 400,
        "artifacts_present": True,
    }
    assert (attempt_dir / "verdict.json").read_bytes() == before  # read-only


def test_suggest_prefills_the_retry_for_transport_dead_tasks(tmp_path):
    # A transport-dead blocked task (refusal transport_error, no artifacts) carries the
    # exact board-new JSON that would retry it: wall_seconds doubled to the 900 s cap,
    # the change pre-written from the refusal. Print only — nothing is authored.
    board = tmp_path / "grid/board"
    board.mkdir(parents=True)
    task = write_task(board, "slow", "blocked")
    task["budget"] = {"wall_seconds": 600, "output_bytes": 2000000, "thinking_tokens": 6000}
    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    account = "go-" + uuid.uuid4().hex[:8]
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    aid, generation = seed_attempt(ledger, account, "seed-1", tmp_path / "ws")
    ledger.start(aid, generation)
    ledger.hold(aid, "Refused: transport dead")
    task["blocked_reason"] = f"attempt {aid} held; resolve with evidence"
    (board / "slow.json").write_text(json.dumps(task))
    attempt_dir = tmp_path / "ws" / aid
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "verdict.json").write_text(
        json.dumps({"refusal": "transport_error: TimeoutError", "transport_timeout": 400})
    )
    before = {p.name: p.read_bytes() for p in board.glob("*.json")}
    report = board_status(ledger, board, suggest=True)
    assert report["skipped"] == []
    rows = {r["id"]: r for r in report["rows"]}
    assert rows["slow"]["suggest"] == {
        "board_dir": str(board),
        "project_root": str(tmp_path),
        "retry": "slow",
        "change": "wall_seconds 600 -> 900 after transport_error: TimeoutError",
        "budget": {"wall_seconds": 900, "output_bytes": 2000000, "thinking_tokens": 6000},
    }
    assert {p.name: p.read_bytes() for p in board.glob("*.json")} == before
    # A small budget doubles without reaching the cap.
    task["budget"] = {"wall_seconds": 300, "output_bytes": 1000, "thinking_tokens": None}
    (board / "slow.json").write_text(json.dumps(task))
    report = board_status(ledger, board, suggest=True)
    rows = {r["id"]: r for r in report["rows"]}
    assert rows["slow"]["suggest"]["change"] == (
        "wall_seconds 300 -> 600 after transport_error: TimeoutError"
    )
    assert rows["slow"]["suggest"]["budget"] == {
        "wall_seconds": 600,
        "output_bytes": 1000,
        "thinking_tokens": None,
    }


def test_suggest_skips_holds_that_are_not_transport_dead(tmp_path):
    board = tmp_path / "grid/board"
    board.mkdir(parents=True)
    write_task(board, "http", "blocked")
    write_task(board, "partial", "blocked")
    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    account = "go-" + uuid.uuid4().hex[:8]
    ledger.configure_account(
        account, 2, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    aid1, gen1 = seed_attempt(ledger, account, "s1", tmp_path / "ws1")
    ledger.start(aid1, gen1)
    ledger.hold(aid1, "Refused: http status")
    (tmp_path / "ws1" / aid1).mkdir(parents=True)
    (tmp_path / "ws1" / aid1 / "verdict.json").write_text(
        json.dumps({"refusal": "endpoint returned HTTP 402"})
    )
    aid2, gen2 = seed_attempt(ledger, account, "s2", tmp_path / "ws2")
    ledger.start(aid2, gen2)
    ledger.hold(aid2, "Refused: transport with files")
    delivered = tmp_path / "ws2" / aid2
    delivered.mkdir(parents=True)
    (delivered / "verdict.json").write_text(
        json.dumps({"refusal": "transport_error: TimeoutError"})
    )
    (delivered / "artifacts").mkdir()
    (delivered / "artifacts" / "out.py").write_text("VALUE = 1\n")
    for name, aid in (("http", aid1), ("partial", aid2)):
        task = json.loads((board / f"{name}.json").read_text())
        task["blocked_reason"] = f"attempt {aid} held; resolve with evidence"
        (board / f"{name}.json").write_text(json.dumps(task))
    report = board_status(ledger, board, suggest=True)
    rows = {r["id"]: r for r in report["rows"]}
    assert "suggest" not in rows["http"]  # not a transport refusal
    assert "suggest" not in rows["partial"]  # transport refusal, but artifacts exist
    assert report["skipped"] == []
    rows = {r["id"]: r for r in board_status(ledger, board)}
    assert all("suggest" not in r for r in rows)  # off by default


def test_suggest_skips_tasks_whose_chain_already_moved_on(tmp_path):
    # review-lane-cline was offered a retry although review-lane-cline-2 existed: the
    # operator's hand-written reason predated the superseded: convention. The id-shape
    # rule decides — a blocked task with an <id>-<n> successor is never offered one.
    board = tmp_path / "grid/board"
    board.mkdir(parents=True)
    write_task(board, "review-lane-cline", "blocked", reason="held at the wall deadline")
    write_task(board, "review-lane-cline-2", "ready")
    write_task(board, "unrelated", "blocked", reason="held; resolve with evidence")
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    report = board_status(ledger, board, suggest=True)
    assert report["skipped"] == [
        {"task": "review-lane-cline", "successor": "review-lane-cline-2"}
    ]
    rows = {r["id"]: r for r in report["rows"]}
    assert "suggest" not in rows["review-lane-cline"]
    assert "suggest" not in rows["unrelated"]
    # Without suggest the shape stays the plain row list.
    assert isinstance(board_status(ledger, board), list)
