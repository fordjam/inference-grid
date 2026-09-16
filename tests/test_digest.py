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


def test_digest_survives_the_drafts_sidecar(tmp_path):
    """The plan node's drafts list is board-owned state, not a task file (brief J1)."""
    from pathlib import Path

    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_board_status import write_task  # reuse the fixtures

    project = tmp_path / "project"
    board = project / "grid/board"
    board.mkdir(parents=True)
    write_task(board, "waiting", "ready")
    (board / "drafts.json").write_text(json.dumps({"drafts": ["packet-k1"]}))
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    boards_dir = tmp_path / "boards"
    boards_dir.mkdir()
    (boards_dir / "project.json").write_text(
        json.dumps({"board_dir": str(board), "project_root": str(project)})
    )
    text = digest(ledger, boards_dir)
    assert "oldest ready task: waiting" in text


def test_digest_reports_the_evals_table_and_the_needs_you_lanes(tmp_path):
    """The Evals section (brief 20, M5): per lane, per kind, accepted / cases and the
    newest result's age; a lane with no eval inside the window is a needs-you row."""
    from test_board_status import write_task  # reuse the fixture

    project = tmp_path / "project"
    board = project / "grid/board"
    board.mkdir(parents=True)
    write_task(board, "waiting", "ready")
    lanes_path = tmp_path / "lanes.json"
    lanes_path.write_text(
        json.dumps(
            {
                "lanes": {
                    "go": {
                        "provider": "opencode",
                        "family": "glm",
                        "model": "glm-5.3-flash",
                        "kind": "go_http",
                        "credential_path": None,
                        "executable": None,
                        "plan_units": {},
                        "window": None,
                        "max_concurrency": 1,
                        "wall_seconds": 60,
                        "categories": ["independent_review"],
                    },
                    "kimi": {
                        "provider": "opencode",
                        "family": "kimi",
                        "model": "kimi-k3",
                        "kind": "go_http",
                        "credential_path": None,
                        "executable": None,
                        "plan_units": {},
                        "window": None,
                        "max_concurrency": 1,
                        "wall_seconds": 60,
                        "categories": ["independent_review"],
                    },
                }
            }
        )
    )
    boards_dir = tmp_path / "boards"
    boards_dir.mkdir()
    (boards_dir / "project.json").write_text(
        json.dumps(
            {
                "board_dir": str(board),
                "project_root": str(project),
                "lanes_path": str(lanes_path),
            }
        )
    )
    now = time.time()
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    _seed_eval(
        ledger, "11111111-2222-3333-4444-eeeeeeeeeeee", "glm", "glm-5.3-flash", "review", now
    )

    text = digest(ledger, boards_dir, now=now)
    assert "## Evals" in text
    assert "| go | review | 1 / 1 | 0.0 d |" in text
    assert "| go | packet | 0 / 0 | never |" in text
    assert "| kimi | review | 0 / 0 | never |" in text
    assert "## Needs you" in text
    assert "eval coverage: `kimi` — no eval ever recorded" in text
    assert "eval coverage: `go`" not in text


def _seed_eval(ledger, aid, family, model, kind, at):
    from sqlalchemy import update

    from inference_grid.ledger import attempts as attempts_t, events, tasks as tasks_t

    with ledger.engine.begin() as con:
        con.execute(
            tasks_t.insert().values(
                id="t-" + aid, project="p", spec={"family": family, "model": model}
            )
        )
        con.execute(
            attempts_t.insert().values(
                id=aid,
                task="t-" + aid,
                account="a",
                generation=1,
                state="completed",
                estimate={},
                workspace="/w",
                receipt={},
                updated=0.0,
            )
        )
    ledger.record_outcome(aid, "eval:" + kind, True, note="eval")
    with ledger.engine.begin() as con:
        con.execute(
            update(events)
            .where(events.c.attempt == aid, events.c.kind == "outcome_recorded")
            .values(at=at)
        )


def uuid_hex():
    import uuid

    return uuid.uuid4().hex[:8]
