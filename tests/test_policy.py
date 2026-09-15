"""Review policy: what the gates already proved is waived, and the waiver is recorded."""

import json

from sqlalchemy import select

from inference_grid.board import policy
from inference_grid.ledger import Ledger, attempts as attempts_t, events, tasks as tasks_t

AID = "11111111-2222-3333-4444-555555555555"
PROVEN = {
    "verified_in_lane": True,
    "gates": [{"name": "pytest", "ok": True}, {"name": "commit", "ok": True}],
}


def test_a_proven_receipt_waives_the_review_for_a_non_claude_author():
    for family in ("glm", "kimi", "deepseek", "openai"):
        needed, reason = policy.review_needed({"id": "work-1"}, PROVEN, family)
        assert needed is False
        assert "waived" in reason and "verified_in_lane" in reason


def test_a_claude_author_is_reviewed_even_with_a_proven_receipt():
    needed, reason = policy.review_needed({"id": "work-1"}, PROVEN, "claude")
    assert needed is True and "claude" in reason


def test_an_unverified_attempt_keeps_the_review():
    for receipt in (
        None,
        {},
        {"verified_in_lane": False, "gates": [{"name": "pytest", "ok": True}]},
        {"gates": [{"name": "pytest", "ok": True}]},
    ):
        needed, _ = policy.review_needed({"id": "work-1"}, receipt, "glm")
        assert needed is True


def test_a_receipt_without_gate_results_cannot_waive():
    needed, reason = policy.review_needed({"id": "work-1"}, {"verified_in_lane": True}, "glm")
    assert needed is True and "no gate results" in reason
    needed, reason = policy.review_needed(
        {"id": "work-1"}, {"verified_in_lane": True, "gates": []}, "glm"
    )
    assert needed is True and "no gate results" in reason


def test_a_failed_declared_gate_names_itself_in_the_reason():
    needed, reason = policy.review_needed(
        {"id": "work-1"},
        {"verified_in_lane": True, "gates": [{"name": "pytest", "ok": False}]},
        "glm",
    )
    assert needed is True and "pytest" in reason


def test_record_waiver_writes_the_source_tasks_review_record(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    path = policy.record_waiver(board, {"id": "work-1"}, "waived: gates green")
    assert path == board / "review" / "work-1" / "review.json"
    record = json.loads(path.read_text())
    assert record == {
        "review": {
            "waived": True,
            "reason": "waived: gates green",
            "task": "work-1",
            "attempt": None,
        }
    }


def test_record_waiver_also_writes_the_ledger_event(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    with ledger.engine.begin() as con:
        con.execute(tasks_t.insert().values(id="work-1-run", project="p", spec={}))
        con.execute(
            attempts_t.insert().values(
                id=AID,
                task="work-1-run",
                account="a",
                generation=1,
                state="completed",
                estimate={},
                workspace="/w",
                receipt={},
                updated=0.0,
            )
        )
    policy.record_waiver(board, {"id": "work-1"}, "waived: gates green", ledger=ledger, attempt=AID)
    with ledger.engine.connect() as con:
        rows = list(con.execute(select(events).where(events.c.kind == "review_waived")).mappings())
    assert len(rows) == 1
    assert rows[0]["attempt"] == AID
    assert rows[0]["detail"] == {"task": "work-1", "reason": "waived: gates green"}
