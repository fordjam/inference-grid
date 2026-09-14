"""digest: the one-page operator view over the boards."""

import json
import time

from inference_grid.digest import digest
from inference_grid.ledger import Ledger


def test_digest_reports_boards_suggestions_and_lanes(tmp_path):
    from pathlib import Path

    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_board_status import seed_attempt, write_task  # reuse the fixtures

    project = tmp_path / "insta-saved"
    board = project / "grid/board"
    board.mkdir(parents=True)
    write_task(board, "done", "accepted")
    write_task(board, "waiting", "ready")
    write_task(board, "stuck", "blocked")
    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    account = "go-" + uuid_hex()
    ledger.configure_account(
        account, 1, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    aid, generation = seed_attempt(ledger, account, "seed-1", tmp_path / "ws")
    ledger.start(aid, generation)
    ledger.hold(aid, "Refused: transport dead")
    attempt_dir = tmp_path / "ws" / aid
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "verdict.json").write_text(
        json.dumps({"refusal": "transport_error: TimeoutError", "transport_timeout": 400})
    )
    stuck = json.loads((board / "stuck.json").read_text())
    stuck["blocked_reason"] = f"attempt {aid} held; resolve with evidence"
    (board / "stuck.json").write_text(json.dumps(stuck))
    (project / "grid/board").mkdir(parents=True, exist_ok=True)
    boards_dir = tmp_path / "boards"
    boards_dir.mkdir()
    (boards_dir / "insta-saved.json").write_text(
        json.dumps({"board_dir": str(board), "project_root": str(project)})
    )
    text = digest(ledger, boards_dir)
    assert "## Board insta-saved" in text
    assert "accepted 1, passed 0, blocked 1, ready 1" in text
    assert "oldest ready task: waiting" in text
    assert "## Suggested retries" in text
    assert "`stuck`" in text
    assert "## Inbox landings awaiting inbox-integrate" in text
    assert "| go-" + account[-8:] + " |" in text or f"| {account} |" in text


def uuid_hex():
    import uuid

    return uuid.uuid4().hex[:8]
