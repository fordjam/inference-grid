"""Eval cases and scoring: the two kinds (review, packet) and their scorecard rows."""

import json
from pathlib import Path

import pytest

from inference_grid.board import calibration
from inference_grid.board.calibration import load_case, load_corpus, record_outcome, score

CORPUS = Path(__file__).resolve().parents[1] / "calibration" / "example"
PACKET_CASE = "rolling-mean-off-by-one"


# --- case loading (the two kinds) ---


def test_a_review_case_without_a_kind_marker_defaults_to_review(tmp_path):
    # The operator's private corpus predates the key; absence means review.
    case = tmp_path / "one"
    case.mkdir()
    (case / "brief.txt").write_text("brief\n")
    (case / "diff.patch").write_text("diff --git a/a.py b/a.py\n")
    (case / "answer.json").write_text(json.dumps({"defects": [], "clean": True}))
    loaded = load_case(case)
    assert loaded["kind"] == "review"
    assert loaded["case"] == "one" and loaded["answer"] == {"defects": [], "clean": True}


def test_a_review_case_may_carry_its_kind_and_case_json_is_never_staged(tmp_path):
    case = tmp_path / "marked"
    case.mkdir()
    (case / "case.json").write_text(json.dumps({"kind": "review"}))
    (case / "brief.txt").write_text("brief\n")
    (case / "diff.patch").write_text("diff --git a/a.py b/a.py\n")
    (case / "answer.json").write_text(json.dumps({"defects": [], "clean": True}))
    (case / "a.py").write_text("a = 1\n")
    loaded = load_case(case)
    assert loaded["kind"] == "review"
    assert [relative for relative, _ in loaded["files"]] == ["a.py"]


def test_an_unknown_kind_or_a_malformed_marker_refuses(tmp_path):
    case = tmp_path / "odd"
    case.mkdir()
    (case / "brief.txt").write_text("brief\n")
    (case / "diff.patch").write_text("diff --git a/a.py b/a.py\n")
    (case / "answer.json").write_text(json.dumps({"defects": [], "clean": True}))
    (case / "case.json").write_text(json.dumps({"kind": "quiz"}))
    with pytest.raises(ValueError, match="kind must be one of"):
        load_case(case)
    (case / "case.json").write_text(json.dumps({"kind": "review", "extra": 1}))
    with pytest.raises(ValueError, match="exactly kind"):
        load_case(case)
    (case / "case.json").write_text("not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_case(case)


def write_packet_case(root, name=PACKET_CASE, module="base body\n", tests=None):
    case = root / name
    base = case / "base" / "src"
    base.mkdir(parents=True)
    (base / "mod.py").write_text(module)
    reference = case / "reference"
    (reference / "reference_tests" / "tests").mkdir(parents=True)
    for test_name, body in (tests or {"tests/test_mod.py": "assert True\n"}).items():
        (reference / "reference_tests" / test_name).write_text(body)
    (reference / "reference.patch").write_text(
        "diff --git a/src/mod.py b/src/mod.py\n--- a/src/mod.py\n+++ b/src/mod.py\n"
    )
    (case / "brief.txt").write_text("brief\n")
    (case / "case.json").write_text(json.dumps({"kind": "packet"}))
    return case


def test_a_packet_case_holds_the_base_and_the_hidden_reference(tmp_path):
    loaded = load_case(write_packet_case(tmp_path))
    assert loaded["kind"] == "packet"
    assert [relative for relative, _ in loaded["base"]] == ["src/mod.py"]
    assert [relative for relative, _ in loaded["reference_tests"]] == ["tests/test_mod.py"]
    assert loaded["reference_patch"].startswith("diff --git")
    # The lane's repository is base/ only: reference material never rides in it.
    assert all(not relative.startswith("reference") for relative, _ in loaded["base"])


def test_a_packet_case_missing_its_reference_refuses(tmp_path):
    case = write_packet_case(tmp_path)
    (case / "reference" / "reference.patch").unlink()
    with pytest.raises(ValueError, match="reference.patch"):
        load_case(case)


def test_the_example_corpus_holds_both_kinds(tmp_path):
    cases = {c["case"]: c for c in load_corpus(CORPUS)}
    assert cases["clean-normalize"]["kind"] == "review"
    assert cases["sum-drops-last"]["kind"] == "review"
    assert cases[PACKET_CASE]["kind"] == "packet"
    assert len(cases[PACKET_CASE]["reference_tests"]) == 5


# --- packet scoring ---


def packet_case_and_attempt(tmp_path, body):
    case = load_case(CORPUS / PACKET_CASE)
    work = tmp_path / "attempt" / "work" / "src" / "metrics"
    work.mkdir(parents=True)
    (work / "rolling.py").write_text(body)
    return case, tmp_path / "attempt"


def base_body():
    return (CORPUS / PACKET_CASE / "base" / "src" / "metrics" / "rolling.py").read_text()


def fixed_body():
    return base_body().replace("if index > window:", "if index >= window:")


def test_a_packet_case_scores_a_correct_fix_accepted(tmp_path):
    case, attempt = packet_case_and_attempt(tmp_path, fixed_body())
    assert score(case, attempt) == {
        "kind": "packet",
        "accepted": True,
        "recalled": 5,
        "false_positives": 0,
        "repairs": 0,
        "notes": "",
    }


def test_the_unfixed_base_fails_the_reference_suite(tmp_path):
    # The off-by-one skips the eviction for index == window and for a window of one, so
    # the sliding-window and single-window cases both fail on the un-fixed base.
    case, attempt = packet_case_and_attempt(tmp_path, base_body())
    scored = score(case, attempt)
    assert scored["kind"] == "packet" and scored["accepted"] is False
    assert scored["repairs"] == 2 and scored["recalled"] == 3


def test_a_partial_fix_counts_its_repairs(tmp_path):
    partial = fixed_body().replace(
        "    means = []\n", "    if not values:\n        return [0.0]\n    means = []\n"
    )
    case, attempt = packet_case_and_attempt(tmp_path, partial)
    scored = score(case, attempt)
    assert scored["accepted"] is False and scored["repairs"] == 1 and scored["recalled"] == 4


def test_a_fix_that_edits_the_reference_tests_is_rejected(tmp_path):
    case, attempt = packet_case_and_attempt(tmp_path, fixed_body())
    tests = attempt / "work" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_rolling_window.py").write_text("assert True\n")
    scored = score(case, attempt)
    assert scored["accepted"] is False
    assert scored["false_positives"] == 1 and scored["repairs"] == 0
    assert "test_rolling_window.py" in scored["notes"]


def test_a_packet_attempt_without_a_lane_tree_is_not_accepted(tmp_path):
    case = load_case(CORPUS / PACKET_CASE)
    scored = score(case, tmp_path / "absent")
    assert scored["accepted"] is False
    assert scored["repairs"] == 5 and scored["notes"] == "no lane tree found"


def test_the_reference_test_seam_is_injected(tmp_path):
    case, attempt = packet_case_and_attempt(tmp_path, fixed_body())
    seen = []

    def run(argv, cwd=None, env=None):
        seen.append(Path(argv[-1]).name)
        return 0

    scored = score(case, attempt, run=run)
    assert scored["accepted"] is True and scored["repairs"] == 0
    assert sorted(seen) == [
        f"test_rolling_{name}.py" for name in ("empty", "exact", "short", "single", "window")
    ]


# --- the review score is the score_calibration arithmetic (a regression pin) ---


def finding(location, text):
    return {"location": location, "input": "n/a", "expected": text, "observed": text}


def reply(verdict, findings):
    return json.dumps({"verdict": verdict, "findings": findings, "checked": ["a", "b", "c"]})


def write_packet(packets_root, task_id, body):
    artifacts = packets_root / task_id / "20260916T120000" / "attempts" / "aid" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "reply.txt").write_text(body)
    return artifacts.parent


def board_and_project(tmp_path):
    project = tmp_path / "project"
    board = project / "grid/board"
    project.mkdir(parents=True)
    return board, project


def test_the_review_score_matches_score_calibration_for_the_example_cases(tmp_path):
    board, project = board_and_project(tmp_path)
    calibration.author_calibration(board, project, CORPUS, ["go"], "pin")
    packets = tmp_path / "packets"
    write_packet(
        packets,
        "calib-pin-clean-normalize",
        reply("rejected", [finding("src/text/normalize.py", "a spurious concern")]),
    )
    write_packet(
        packets,
        "calib-pin-sum-drops-last",
        reply(
            "rejected",
            [
                finding(
                    "src/metering/totals.py (total_units)",
                    "range(len(rows) - 1) skips the last row",
                )
            ],
        ),
    )
    report = calibration.score_calibration(board, "pin", packets)
    rows = {r["case"]: r for r in report["lanes"]["go"]["cases_detail"]}
    for name in ("clean-normalize", "sum-drops-last"):
        case = load_case(CORPUS / name)
        attempt = packets / f"calib-pin-{name}" / "20260916T120000" / "attempts" / "aid"
        scored = score(case, attempt)
        row = rows[name]
        assert scored["kind"] == row["kind"] == "review"
        assert scored["accepted"] == row["accepted"]
        assert scored["recalled"] == row["recalled"]
        assert scored["false_positives"] == row["false_positives"]
    # The clean case gained a spurious finding; the defective one recalled its defect.
    assert rows["clean-normalize"]["accepted"] is False
    assert rows["sum-drops-last"]["accepted"] is True


def test_a_review_score_without_a_reply_is_not_accepted(tmp_path):
    case = load_case(CORPUS / "clean-normalize")
    scored = score(case, tmp_path / "absent")
    assert scored["kind"] == "review" and scored["accepted"] is False
    assert scored["notes"] == "no settled reply found"


# --- the outcome path writes eval:<kind> scorecard rows ---


def test_record_outcome_writes_eval_categories(tmp_path):
    from sqlalchemy import select

    from inference_grid.ledger import (
        Ledger,
        attempts as attempts_t,
        events,
        tasks as tasks_t,
    )

    ledger = Ledger("sqlite:///" + str(tmp_path / "l.sqlite"))
    ledger.initialize()
    ids = {
        "review": "11111111-2222-3333-4444-555555555551",
        "packet": "11111111-2222-3333-4444-555555555552",
    }
    with ledger.engine.begin() as con:
        for kind, aid in ids.items():
            con.execute(
                tasks_t.insert().values(
                    id="t-" + kind, project="p", spec={"family": "fs", "model": "m"}
                )
            )
            con.execute(
                attempts_t.insert().values(
                    id=aid,
                    task="t-" + kind,
                    account="a",
                    generation=1,
                    state="completed",
                    estimate={},
                    workspace="/w",
                    receipt={},
                    updated=0.0,
                )
            )
    record_outcome(
        ledger,
        ids["review"],
        {
            "kind": "review",
            "accepted": True,
            "recalled": 1,
            "false_positives": 0,
            "repairs": 0,
            "notes": "",
        },
    )
    record_outcome(
        ledger,
        ids["packet"],
        {
            "kind": "packet",
            "accepted": False,
            "recalled": 3,
            "false_positives": 0,
            "repairs": 2,
            "notes": "",
        },
    )
    categories = sorted(row["category"] for row in ledger.scorecard())
    assert categories == ["eval:packet", "eval:review"]
    with ledger.engine.connect() as con:
        recorded = list(
            con.execute(select(events).where(events.c.kind == "outcome_recorded")).mappings()
        )
    assert {r["detail"]["category"] for r in recorded} == {"eval:packet", "eval:review"}
    assert {r["detail"]["repairs"] for r in recorded} == {0, 2}


