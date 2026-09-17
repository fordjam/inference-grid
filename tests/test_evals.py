"""Eval freshness reporting (board/evals.py), the calibration_run task and its runner step.

B4 deleted the authoring side (`inference-grid evals`, `due_evals`/`author_evals`): no
calibration_run task is ever authored automatically anymore. What is left — reading
recorded eval outcomes for the digest, and the runner's own execution of a calibration_run
task if one already exists on a board — is still covered here.

Offline throughout: the ledger is a temp sqlite, the corpus is a temp directory, and the
packet case's build is a fake dispatch that writes the lane's tree — no lane, no network.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import update

from inference_grid.board import calibration, evals
from inference_grid.board.runner import step_calibration_run
from inference_grid.ledger import Ledger, attempts as attempts_t, events, tasks as tasks_t

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "calibration" / "example"
PACKET_CASE = "rolling-mean-off-by-one"
NOW = 1_800_000_000.0
DAY = 86400


# --- fixtures -------------------------------------------------------------------


def write_review_case(root, name, clean=True):
    case = root / name
    case.mkdir(parents=True)
    (case / "case.json").write_text(json.dumps({"kind": "review"}))
    (case / "brief.txt").write_text("Review the staged change.\n")
    (case / "diff.patch").write_text("diff --git a/src/a.py b/src/a.py\n")
    (case / "answer.json").write_text(json.dumps({"defects": [], "clean": clean}))
    return case


def write_packet_case(root, name=PACKET_CASE):
    case = root / name
    base = case / "base" / "src"
    base.mkdir(parents=True)
    (base / "mod.py").write_text("value = 1\n")
    reference = case / "reference"
    (reference / "reference_tests" / "tests").mkdir(parents=True)
    (reference / "reference_tests" / "tests" / "test_mod.py").write_text("assert True\n")
    (reference / "reference.patch").write_text(
        "diff --git a/src/mod.py b/src/mod.py\n--- a/src/mod.py\n+++ b/src/mod.py\n"
    )
    (case / "brief.txt").write_text("Make the failing case pass.\n")
    (case / "case.json").write_text(json.dumps({"kind": "packet"}))
    return case


def corpus(tmp_path, names=("review-a",)):
    root = tmp_path / "corpus"
    root.mkdir(exist_ok=True)
    for name in names:
        if name == PACKET_CASE:
            write_packet_case(root)
        else:
            write_review_case(root, name)
    return root


def ladder(tmp_path, corpus_dir):
    """A ledger, a board and a project root the authoring can stage under."""
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    project = tmp_path / "grid-project"
    board = project / "grid" / "board"
    schema = project / "grid" / "tests" / "test_review_schema.py"
    board.mkdir(parents=True)
    schema.parent.mkdir(parents=True)
    schema.write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n    def test(self):\n        pass\n"
    )
    return ledger, board, project


def lanes(lane_id="go", family="glm", model="glm-5.3-flash"):
    return {lane_id: {"family": family, "model": model, "categories": ["independent_review"]}}


def seed_outcome(ledger, aid, family, model, kind, accepted=True, at=NOW):
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
    ledger.record_outcome(aid, "eval:" + kind, accepted, note="eval")
    with ledger.engine.begin() as con:
        con.execute(
            update(events)
            .where(events.c.attempt == aid, events.c.kind == "outcome_recorded")
            .values(at=at)
        )
    return aid


def add_attempt(ledger, task_id, aid, state="completed"):
    with ledger.engine.begin() as con:
        con.execute(
            tasks_t.insert().values(id=task_id, project="p", spec={"family": "glm", "model": "m"})
        )
        con.execute(
            attempts_t.insert().values(
                id=aid,
                task=task_id,
                account="a",
                generation=1,
                state=state,
                estimate={},
                workspace="/w",
                receipt={},
                updated=0.0,
            )
        )


# --- the ledger-side eval rows ----------------------------------------------------


def test_eval_rows_aggregate_accepted_and_cases(tmp_path):
    ledger, _board, _project = ladder(tmp_path, None)
    seed_outcome(ledger, "c1", "glm", "glm-5.3-flash", "review", accepted=True, at=NOW - DAY)
    seed_outcome(ledger, "c2", "glm", "glm-5.3-flash", "review", accepted=False, at=NOW)
    rows = evals.eval_rows(ledger)
    row = rows[("glm", "glm-5.3-flash", "review")]
    assert row["cases"] == 2 and row["accepted"] == 1 and row["newest_at"] == NOW
    # A non-eval outcome never counts toward the eval rows.
    aid = "c3"
    add_attempt(ledger, "t-c3", aid)
    ledger.record_outcome(aid, "pure_function", True, note="not an eval")
    assert set(evals.eval_rows(ledger)) == {("glm", "glm-5.3-flash", "review")}


def test_an_unknown_eval_kind_is_not_an_eval_row(tmp_path):
    ledger, _board, _project = ladder(tmp_path, None)
    aid = "d1"
    add_attempt(ledger, "t-d1", aid)
    ledger.record_outcome(aid, "eval:quiz", True, note="unknown kind")
    assert evals.eval_rows(ledger) == {}


# --- the digest summary -----------------------------------------------------------


def test_eval_summary_per_lane_kind_and_needs_you(tmp_path):
    ledger, _board, _project = ladder(tmp_path, None)
    seed_outcome(ledger, "e1", "glm", "glm-5.3-flash", "review", accepted=True, at=NOW - DAY)
    both = lanes() | {"kimi": {"family": "kimi", "model": "kimi-k3", "categories": []}}
    summary = evals.eval_summary(ledger, both, now=NOW)
    by_lane = {row["lane"]: row for row in summary["lanes"]}
    glm = {kind["kind"]: kind for kind in by_lane["go"]["kinds"]}
    assert glm["review"] == {"kind": "review", "accepted": 1, "cases": 1, "age_days": 1.0}
    assert glm["packet"] == {"kind": "packet", "accepted": 0, "cases": 0, "age_days": None}
    assert by_lane["go"]["newest_age_days"] == 1.0
    assert [row["lane"] for row in summary["needs_you"]] == ["kimi"]
    assert summary["needs_you"][0]["reason"] == "no eval ever recorded"
    # A result older than the window is needs-you with its age named.
    seed_outcome(ledger, "e2", "kimi", "kimi-k3", "review", at=NOW - 9 * DAY)
    summary = evals.eval_summary(ledger, both, now=NOW)
    stale = {row["lane"]: row["reason"] for row in summary["needs_you"]}
    assert stale == {"kimi": "no eval in 7 days"}
    assert "| go | review | 1 / 1 | 1.0 d |" in evals.eval_markdown(summary)


def test_case_task_id_stays_legal_and_bounded(tmp_path):
    assert (
        calibration.case_task_id("e-20260916-abcdef12", "small") == "eval-e-20260916-abcdef12-small"
    )
    long_id = calibration.case_task_id("e-20260916-abcdef12", "x" * 80)
    assert len(long_id) <= 60
    assert calibration.NAME.fullmatch(long_id)


def test_a_case_run_must_name_exactly_one_lane(tmp_path):
    _ledger, board, _project = ladder(tmp_path, None)
    with pytest.raises(ValueError, match="exactly one lane"):
        calibration.calibration_task(board, "/corpus", ["go", "kimi"], "e-20260916-abc", case="one")
    # A whole-corpus run still takes the plural list.
    created = calibration.calibration_task(board, "/corpus", ["go", "kimi"], "e-20260916-abc")
    task = json.loads(Path(created["task"]).read_text())
    assert task["spec"] == {
        "corpus_dir": "/corpus",
        "lanes": ["go", "kimi"],
        "run_id": "e-20260916-abc",
    }
    assert (board / "calibration-e-20260916-abc.json").is_file()


# --- the runner step: review ------------------------------------------------------


def review_run(tmp_path, corpus_root):
    ledger, board, project = ladder(tmp_path, corpus_root)
    created = calibration.calibration_task(
        board, str(corpus_root), ["go"], "e-20260916-abc12345", case="review-a"
    )
    path = Path(created["task"])
    return ledger, board, project, path, json.loads(path.read_text())


def step(path, board, project, ledger, packets, **kw):
    """One tick pass over the run task, re-read from disk exactly as the tick re-reads it."""
    return step_calibration_run(
        Path(path),
        json.loads(Path(path).read_text()),
        board,
        project,
        ledger,
        packets,
        lanes=kw.get("lanes", lanes()),
        lanes_path=None,
        accounts_by_lane=kw.get("accounts_by_lane", {}),
        dispatch_fn=kw.get("dispatch_fn"),
    )


def test_a_review_case_run_authors_waits_and_records_an_eval_outcome(tmp_path):
    root = corpus(tmp_path, ["review-a"])
    ledger, board, project, path, task = review_run(tmp_path, root)
    packets = tmp_path / "packets"

    first = step(path, board, project, ledger, packets)
    assert first["result"] == "authored" and first["lane"] is None
    assert json.loads(path.read_text())["state"] == "review_pending"
    case_task = board / "eval-e-20260916-abc12345-review-a.json"
    assert case_task.is_file()
    staged = json.loads(case_task.read_text())
    assert staged["category"] == "independent_review" and staged["lanes"] == ["go"]
    assert staged["author_family"] == "calibration"

    # The lane's attempt settles with an approving reply.
    aid = "11111111-2222-3333-4444-aaaaaaaaaaaa"
    stamp = "20260916T120000"
    reply = packets / case_task.stem / stamp / "attempts" / aid / "artifacts" / "reply.txt"
    reply.parent.mkdir(parents=True)
    reply.write_text(json.dumps({"verdict": "approved", "findings": [], "checked": ["a"]}))
    add_attempt(ledger, case_task.stem + "-" + stamp + "-abc123", aid)
    case_task.write_text(json.dumps(dict(staged, state="passed")))

    second = step(path, board, project, ledger, packets)
    assert second["result"] == "passed" and second["attempt"] == aid
    assert second["accepted"] is True
    assert json.loads(path.read_text())["state"] == "passed"
    with ledger.engine.connect() as con:
        from sqlalchemy import select

        recorded = list(
            con.execute(select(events).where(events.c.kind == "outcome_recorded")).mappings()
        )
    assert [r["detail"]["category"] for r in recorded] == ["eval:review"]
    assert recorded[0]["detail"]["accepted"] is True


def test_a_review_case_run_waits_while_its_case_task_is_open(tmp_path):
    root = corpus(tmp_path, ["review-a"])
    ledger, board, project, path, task = review_run(tmp_path, root)
    step(path, board, project, ledger, tmp_path / "packets")
    waiting = step(path, board, project, ledger, tmp_path / "packets")
    assert waiting["result"] == "waiting on review-a"
    assert json.loads(path.read_text())["state"] == "review_pending"


# --- the runner step: packet ------------------------------------------------------


def test_a_packet_case_run_builds_in_a_base_repo_and_records_the_scored_outcome(tmp_path):

    ledger, board, project = ladder(tmp_path, None)
    created = calibration.calibration_task(
        board, str(EXAMPLE), ["go"], "e-20260916-pkt12345", case=PACKET_CASE
    )
    path = Path(created["task"])

    packets = tmp_path / "packets"
    seen = {}

    def fake_dispatch(
        ledger, lanes_spec, lanes_path, lane_id, packet_task, repo, packet_dir, alias
    ):
        seen["task"] = packet_task
        seen["repo"] = Path(repo)
        seen["lane"] = lane_id
        aid = "22222222-3333-4444-5555-bbbbbbbbbbbb"
        attempt_dir = Path(packet_dir) / "attempts" / aid
        work = attempt_dir / "work"
        # The lane's solution: the base tree with the off-by-one fixed.
        body = (Path(repo) / "src" / "metrics" / "rolling.py").read_text()
        (work / "src" / "metrics").mkdir(parents=True)
        (work / "src" / "metrics" / "rolling.py").write_text(
            body.replace("if index > window:", "if index >= window:")
        )
        add_attempt(ledger, packet_task["id"], aid)
        return aid, "completed", attempt_dir

    result = step(
        path,
        board,
        project,
        ledger,
        packets,
        dispatch_fn=fake_dispatch,
        accounts_by_lane={"go": "go-alias"},
    )
    assert result["result"] == "passed" and result["accepted"] is True
    assert result["repairs"] == 0
    assert json.loads(path.read_text())["state"] == "passed"

    # The build ran on a repository of the case's base only (plus the brief), never the
    # hidden reference suite.
    repo = seen["repo"]
    assert (repo / ".git").is_dir()
    assert (repo / "src" / "metrics" / "rolling.py").is_file()
    assert not any("reference" in part for part in str(repo).split("/"))
    assert not (repo / "reference").exists()
    assert seen["lane"] == "go"
    # The in-memory packet task is a valid packet task: one gate, the case's brief, no tests.
    packet_task = seen["task"]
    assert packet_task["category"] == "packet"
    assert packet_task["spec"]["base"] == "HEAD"
    assert packet_task["spec"]["packet_id"] == "E1"
    assert packet_task["tests"] == []
    assert packet_task["brief"] in packet_task["inputs"]
    # The eval outcome is the scorecard row M4's design names.
    with ledger.engine.connect() as con:
        from sqlalchemy import select

        recorded = list(
            con.execute(select(events).where(events.c.kind == "outcome_recorded")).mappings()
        )
    assert [r["detail"]["category"] for r in recorded] == ["eval:packet"]
    assert recorded[0]["detail"]["accepted"] is True


def test_a_packet_case_run_blocks_when_the_attempt_is_held(tmp_path):
    ledger, board, project = ladder(tmp_path, None)
    created = calibration.calibration_task(
        board, str(EXAMPLE), ["go"], "e-20260916-held1234", case=PACKET_CASE
    )
    path = Path(created["task"])

    def fake_dispatch(
        ledger, lanes_spec, lanes_path, lane_id, packet_task, repo, packet_dir, alias
    ):
        aid = "33333333-4444-5555-6666-cccccccccccc"
        add_attempt(ledger, packet_task["id"], aid, state="held")
        return aid, "held", Path(packet_dir) / "attempts" / aid

    result = step(
        path,
        board,
        project,
        ledger,
        tmp_path / "packets",
        dispatch_fn=fake_dispatch,
        accounts_by_lane={"go": "go-alias"},
    )
    assert result["result"] == "blocked"
    assert "held" in json.loads(path.read_text())["blocked_reason"]


# --- admission refusal (main, 8e26e4c) ---------------------------------------------
#
# B4 deleted the `inference-grid evals` CLI command and its authoring entry points, so
# main's own `test_evals_round_trips_through_main` (a CLI round-trip through that
# command) does not apply on this branch and is not carried over by this merge; the
# runner-level admission-refusal behavior it was added alongside is independent of the
# CLI and is kept below.


def test_a_packet_case_run_stays_ready_when_admission_is_refused(tmp_path):
    from inference_grid.ledger import Refused

    ledger, board, project = ladder(tmp_path, None)
    created = calibration.calibration_task(
        board, str(EXAMPLE), ["go"], "e-20260916-busy1234", case=PACKET_CASE
    )
    path = Path(created["task"])

    def fake_dispatch(*args, **kwargs):
        raise Refused("account busy")

    result = step(
        path,
        board,
        project,
        ledger,
        tmp_path / "packets",
        dispatch_fn=fake_dispatch,
        accounts_by_lane={"go": "go-alias"},
    )
    assert result["result"] == "refused: account busy" and result["lane"] == "go"
    saved = json.loads(path.read_text())
    assert saved["state"] == "ready" and saved.get("blocked_reason") is None
