"""The "needs you" overlay: operator-blocked work and per-week accepted work."""

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import update

from inference_grid.capacity import clean_accepted_work, clean_operator, project
from inference_grid.ledger import Ledger, attempts as attempts_table, digest
from inference_grid.operator_queue import (
    _real_findings,
    accepted_work,
    build_overlay,
    operator_rows,
    reviewer_recall,
)


def receipt(model, manifest, verified=True):
    return {
        "status": "completed",
        "finish_reason": "stop",
        "actual_model": model,
        "manifest_sha256": manifest,
        "verified_in_lane": verified,
        "artifacts": [{"path": "out.py", "sha256": "a" * 64}],
    }


def make_spec(model="glm-5.3-flash", family="glm"):
    return {
        "authorized": True,
        "model": model,
        "family": family,
        "argv": ["/usr/bin/true"],
        "workspace": "/tmp/ws-" + uuid.uuid4().hex[:8],
        "timeout": 60,
        "output_bytes": 1000,
        "inputs": {},
        "manifest_sha256": digest({}),
    }


def make_ledger(tmp_path, account="zai"):
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    ledger.configure_account(
        account, 2, {"five_hour": 10, "weekly": 20}, time.time() + 600, ["glm-5.3-flash"]
    )
    return ledger


def held_attempt(ledger, task_id, account="zai"):
    spec = make_spec()
    ledger.submit(task_id, "project", spec)
    aid, generation = ledger.claim(task_id, account, {"five_hour": 0.01, "weekly": 0.01})
    ledger.start(aid, generation)
    ledger.hold(aid, "provider inference cooldown requires reconciliation")
    return aid


def completed_attempt(ledger, task_id, account="zai"):
    spec = make_spec()
    ledger.submit(task_id, "project", spec)
    aid, generation = ledger.claim(task_id, account, {"five_hour": 0.01, "weekly": 0.01})
    ledger.start(aid, generation)
    ledger.finish(aid, generation, receipt("glm-5.3-flash", spec["manifest_sha256"]))
    return aid


def write_board(tmp_path, tasks):
    board = tmp_path / "board"
    board.mkdir(exist_ok=True)
    for task in tasks:
        (board / (task["id"] + ".json")).write_text(json.dumps(task))
    return board


def set_updated(ledger, aid, epoch):
    with ledger.tx() as con:
        con.execute(update(attempts_table).where(attempts_table.c.id == aid).values(updated=epoch))


# --- operator: blocked board tasks naming an operator decision ---


def test_blocked_tasks_needing_operator(tmp_path):
    ledger = make_ledger(tmp_path)
    board = write_board(
        tmp_path,
        [
            {
                "id": "task-needs",
                "state": "blocked",
                "blocked_reason": "resolve with evidence: operator should re-check the claim",
            },
            {
                "id": "task-superseded",
                "state": "blocked",
                "blocked_reason": "superseded by task-b; operator already moved on",
            },
            {"id": "task-ready", "state": "ready"},
            {
                "id": "task-plain",
                "state": "blocked",
                "blocked_reason": "workspace busy; retry after the lane finishes",
            },
        ],
    )
    rows = operator_rows(ledger, board_dirs=[board])
    assert [r["id"] for r in rows if r["kind"] == "blocked_task"] == ["task-needs"]
    row = rows[0]
    assert row["kind"] == "blocked_task"
    assert row["reason"].startswith("resolve with evidence")
    assert row["since"]  # parsed back below
    datetime.fromisoformat(row["since"])


def test_held_attempt_becomes_operator_row(tmp_path):
    ledger = make_ledger(tmp_path)
    aid = held_attempt(ledger, "held-task")
    rows = operator_rows(ledger)
    assert [(r["kind"], r["id"]) for r in rows] == [("held_attempt", aid)]
    assert rows[0]["reason"] == "provider inference cooldown requires reconciliation"
    assert rows[0]["since"]
    # Once resolved (no longer held) the row disappears.
    with ledger.tx() as con:
        con.execute(
            update(attempts_table)
            .where(attempts_table.c.id == aid)
            .values(state="completed", updated=time.time())
        )
    assert operator_rows(ledger) == []


def test_alarms_from_watch_state_and_absent_file_is_quiet(tmp_path):
    ledger = make_ledger(tmp_path)
    state = tmp_path / "watch-state.json"
    state.write_text(
        json.dumps(
            {
                "alarms": [
                    {
                        "kind": "reading_stale",
                        "key": "zai",
                        "since": "2026-09-14T10:00:00+00:00",
                        "detail": "reading stale 3h",
                    },
                    {"kind": "", "key": "zai"},  # malformed alarm ignored
                ]
            }
        )
    )
    rows = operator_rows(ledger, watch_state=state)
    assert rows == [
        {
            "kind": "alarm",
            "id": "reading_stale:zai",
            "reason": "reading stale 3h",
            "since": "2026-09-14T10:00:00+00:00",
        }
    ]
    # A missing state file (packet A1 not landed) invents no operator work.
    assert operator_rows(ledger, watch_state=tmp_path / "absent.json") == []
    assert operator_rows(ledger, watch_state=None) == []


def test_owner_decision_rows(tmp_path):
    ledger = make_ledger(tmp_path)
    decisions = tmp_path / "owner-decisions.json"
    decisions.write_text(
        json.dumps(
            [
                {
                    "id": "dec-1",
                    "question": "Keep lane B on zai?",
                    "since": "2026-09-14T09:00:00+00:00",
                },
                {"question": "no id, ignored"},
            ]
        )
    )
    rows = operator_rows(ledger, owner_decisions=decisions)
    assert rows == [
        {
            "kind": "decision",
            "id": "dec-1",
            "reason": "Keep lane B on zai?",
            "since": "2026-09-14T09:00:00+00:00",
        }
    ]


# --- accepted_work: per subscription per ISO week ---


def test_accepted_work_counts_only_accepted_attempts(tmp_path):
    ledger = make_ledger(tmp_path)
    aid = completed_attempt(ledger, "reviewed-ok")
    ledger.record_outcome(aid, "independent_review", True, note="review accepted: checks out")
    external = make_spec()
    external["account"] = "zai"
    ledger.record_external(
        "external-ok",
        "project",
        external,
        receipt("glm-5.3-flash", external["manifest_sha256"]),
        "external_work",
        True,
        note="ran by hand",
    )
    # A rejected review naming a real finding is itself accepted work.
    aid = completed_attempt(ledger, "reviewed-rejected")
    ledger.record_outcome(
        aid,
        "independent_review",
        False,
        note="review rejected with 2 finding(s) (finding 2 advisory-only: rests on the"
        " brief's advisory size hint, not a defect)",
    )
    # A rejected review whose only finding is advisory-only is not accepted work.
    aid = completed_attempt(ledger, "reviewed-advisory")
    ledger.record_outcome(
        aid,
        "independent_review",
        False,
        note="review rejected with 1 finding(s) (finding 1 advisory-only: size hint only)",
    )
    # An unaccepted attempt counts as an attempt only.
    aid = completed_attempt(ledger, "unaccepted")
    ledger.record_outcome(aid, "pure_function", False)
    rows = accepted_work(ledger)
    week = datetime.fromtimestamp(time.time(), timezone.utc).isocalendar()
    expected_week = f"{week[0]}-W{week[1]:02d}"
    assert rows == [{"week": expected_week, "account": "zai", "accepted": 3, "attempts": 5}]


def test_accepted_work_buckets_by_iso_week_and_drops_old_weeks(tmp_path):
    ledger = make_ledger(tmp_path)
    now = time.time()
    for name, offset in (("this-week", 0), ("last-week", 7 * 86400), ("stale", 10 * 7 * 86400)):
        aid = completed_attempt(ledger, name)
        ledger.record_outcome(aid, "pure_function", True)
        set_updated(ledger, aid, now - offset)
    rows = accepted_work(ledger, now=now)
    assert len(rows) == 2
    assert sorted(r["attempts"] for r in rows) == [1, 1]
    assert all(r["accepted"] == 1 for r in rows)
    # Two distinct ISO weeks, and the 10-week-old attempt is outside the window.
    assert len({r["week"] for r in rows}) == 2


def test_real_findings_excludes_advisory_only():
    assert _real_findings("review rejected with 3 finding(s)") == 3
    assert (
        _real_findings(
            "review rejected with 3 finding(s) (finding 2 advisory-only: rests on the"
            " brief's advisory size hint, not a defect)"
        )
        == 2
    )
    assert _real_findings("review rejected with 1 finding(s) (finding 1 advisory-only: hint)") == 0
    assert _real_findings("review accepted: no findings") == 0
    assert _real_findings(None) == 0


def test_build_overlay_returns_both_lists(tmp_path):
    ledger = make_ledger(tmp_path)
    held_attempt(ledger, "held-task")
    board = write_board(
        tmp_path,
        [{"id": "t1", "state": "blocked", "blocked_reason": "owner must decide the boundary"}],
    )
    state = tmp_path / "watch-state.json"
    state.write_text(
        json.dumps({"alarms": [{"kind": "k", "key": "x", "since": "s", "detail": "d"}]})
    )
    overlay = build_overlay(ledger, board_dirs=[board], watch_state=state)
    assert {row["kind"] for row in overlay["operator"]} == {
        "alarm",
        "blocked_task",
        "held_attempt",
    }
    week = datetime.fromtimestamp(time.time(), timezone.utc).isocalendar()
    assert overlay["accepted_work"] == [
        {  # the still-held attempt counts as an attempt, not yet accepted
            "week": f"{week[0]}-W{week[1]:02d}",
            "account": "zai",
            "accepted": 0,
            "attempts": 1,
        }
    ]


# --- reviewer_recall: the newest calibration run per lane ---


def write_report(board, run_id, scored_at, lanes):
    run_dir = board / "calibration" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps({"run_id": run_id, "scored_at": scored_at, "lanes": lanes})
    )


def test_reviewer_recall_keeps_the_newest_run_per_lane(tmp_path):
    board = tmp_path / "board"
    write_report(board, "run-old", "2026-09-01T00:00:00+00:00", {"go": {"recall": 0.4}})
    write_report(board, "run-new", "2026-09-10T00:00:00+00:00", {"go": {"recall": 0.9}})
    # A second lane keeps its own newest run untouched by the first lane's newer one.
    write_report(board, "run-go", "2026-09-05T00:00:00+00:00", {"cline": {"recall": 0.25}})
    rows = reviewer_recall([board])
    assert rows == [
        {
            "run_id": "run-go",
            "lane": "cline",
            "recall": 0.25,
            "precision": None,
            "scored_at": "2026-09-05T00:00:00+00:00",
        },
        {
            "run_id": "run-new",
            "lane": "go",
            "recall": 0.9,
            "precision": None,
            "scored_at": "2026-09-10T00:00:00+00:00",
        },
    ]
    # A missing or unreadable board invents nothing.
    assert reviewer_recall([tmp_path / "absent"]) == []
    board2 = tmp_path / "board2"
    (board2 / "calibration" / "broken").mkdir(parents=True)
    (board2 / "calibration" / "broken" / "report.json").write_text("not json")
    assert reviewer_recall([board2]) == []


def test_build_overlay_carries_reviewer_recall(tmp_path):
    ledger = make_ledger(tmp_path)
    board = tmp_path / "board"
    write_report(
        board,
        "run-1",
        "2026-09-15T00:00:00+00:00",
        {"go": {"recall": 1.0, "precision": 0.5}},
    )
    overlay = build_overlay(ledger, board_dirs=[board])
    assert overlay["reviewer_recall"] == [
        {
            "run_id": "run-1",
            "lane": "go",
            "recall": 1.0,
            "precision": 0.5,
            "scored_at": "2026-09-15T00:00:00+00:00",
        }
    ]


# --- operator: drafted fix packets waiting for a release ---


def test_drafted_fix_packets_are_needs_you_rows(tmp_path):
    ledger = make_ledger(tmp_path)
    project = tmp_path / "project"
    board = project / "grid/board"
    briefs = project / "grid/briefs"
    board.mkdir(parents=True)
    briefs.mkdir(parents=True)
    (briefs / "packet-k1.txt").write_text(
        "## 1. Hard rules\n\n---\n\n#### K1. Retry budget on the worker\n\nbody\n"
    )
    (board / "packet-k1.json").write_text(
        json.dumps({"id": "packet-k1", "brief": "grid/briefs/packet-k1.txt"})
    )
    (board / "drafts.json").write_text(json.dumps({"drafts": ["packet-k1", "gone"]}))

    drafts = [r for r in operator_rows(ledger, board_dirs=[board]) if r["kind"] == "draft"]
    assert [(r["id"], r["reason"]) for r in drafts] == [
        ("gone", "gone"),  # an unreadable brief is the id, never an invented title
        ("packet-k1", "Retry budget on the worker"),
    ]
    assert all(r["since"] for r in drafts)
    datetime.fromisoformat(drafts[0]["since"])
    # The overlay and the dashboard sanitizers keep the row, and the shell labels it.
    assert build_overlay(ledger, board_dirs=[board])["operator"] == drafts
    assert clean_operator(drafts)[0]["kind"] == "draft"


def test_the_needs_you_label_maps_the_draft_kind():
    root = Path(__file__).resolve().parents[1]
    for rel in ("deployments/capacity/web", "src/inference_grid/capacity_web"):
        assert "draft:'Drafted fix'" in (root / rel / "app.js").read_text()


# --- the sanitizers capacity.project consumes ---


def test_clean_operator_strips_unknown_keys_and_drops_empty():
    rows = clean_operator(
        [
            {"kind": "blocked_task", "id": "t1", "reason": "r", "since": "s", "task": "private"},
            {"kind": "", "id": "t2", "reason": "r", "since": "s"},
            {"kind": "alarm", "id": "a1"},
        ]
    )
    # reason/since are optional; kind+id make a row, unknown keys are stripped.
    assert rows == [
        {"kind": "blocked_task", "id": "t1", "reason": "r", "since": "s"},
        {"kind": "alarm", "id": "a1"},
    ]
    assert clean_operator("not-a-list") == []
    assert clean_operator([None]) == []


def test_clean_accepted_work_requires_strings_and_counts():
    good = {"week": "2026-W37", "account": "zai", "accepted": 3, "attempts": 5}
    assert clean_accepted_work([good, dict(good, accepted=True)]) == [good]
    assert clean_accepted_work("junk") == []
    assert clean_accepted_work([{"week": "w", "account": "a", "accepted": 0}]) == []


def test_project_carries_all_lists_through_cleaning():
    operator = [{"kind": "held_attempt", "id": "a1", "reason": "r", "since": "s", "x": 1}]
    accepted = [{"week": "2026-W37", "account": "zai", "accepted": 1, "attempts": 2}]
    recall = [
        {
            "run_id": "run-1",
            "lane": "go",
            "recall": 0.5,
            "precision": None,
            "scored_at": "2026-09-15T00:00:00+00:00",
            "secret": "x",
        }
    ]
    out = project({}, {"operator": operator, "accepted_work": accepted, "reviewer_recall": recall})
    assert out["operator"] == [{"kind": "held_attempt", "id": "a1", "reason": "r", "since": "s"}]
    assert out["accepted_work"] == accepted
    assert out["reviewer_recall"] == [
        {
            "run_id": "run-1",
            "lane": "go",
            "recall": 0.5,
            "precision": None,
            "scored_at": "2026-09-15T00:00:00+00:00",
        }
    ]
    out = project({}, {})
    assert out["operator"] == [] and out["accepted_work"] == [] and out["reviewer_recall"] == []


def test_dashboards_reference_every_list():
    root = Path(__file__).resolve().parents[1]
    for rel in ("deployments/capacity/web", "src/inference_grid/capacity_web"):
        base = root / rel
        js = (base / "app.js").read_text()
        html = (base / "index.html").read_text()
        assert "snapshot.operator" in js
        assert "snapshot.accepted_work" in js
        assert "snapshot.reviewer_recall" in js
        for marker in (
            'id="needs"',
            'id="needsyou"',
            'id="acceptedwork"',
            'id="reviewerrecall"',
            "Needs you",
            "Accepted work",
            "Reviewer recall",
        ):
            assert marker in html, (rel, marker)
