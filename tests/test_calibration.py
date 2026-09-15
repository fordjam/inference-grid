"""Reviewer calibration: corpus format, authoring, scoring, seed corpus and CLI."""

import json
import sys
from pathlib import Path

import pytest

from inference_grid.board import calibration
from inference_grid.board.calibration import load_corpus


def diff_block(path, body):
    """A minimal unified-diff file block adding `path` with the given body lines."""
    lines = "".join(f"+{line}\n" for line in body.splitlines())
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(body.splitlines())} @@\n"
        f"{lines}"
    )


def write_case(root, name, files, diff, brief, answer):
    """One corpus case: packet files plus the answer key that is never staged."""
    case = root / name
    case.mkdir(parents=True)
    for relative, text in files.items():
        target = case / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (case / "diff.patch").write_text(diff)
    (case / "brief.txt").write_text(brief)
    (case / "answer.json").write_text(json.dumps(answer))
    return case


def defect(id, file, must_mention, severity="high", note="the defect"):
    return {
        "id": id,
        "file": file,
        "must_mention": must_mention,
        "severity": severity,
        "note": note,
    }


SPEC_DIFF = diff_block(
    "web/e2e/views.spec.ts",
    'import { test, expect } from "@playwright/test";\n\n'
    'test("rename keeps the view", async ({ page }) => {\n'
    '  await page.route("/api/views", (route) => route.fulfill({ body: "[]" }));\n'
    '  await page.goto("/views");\n'
    '  await expect(page.getByRole("heading")).toBeVisible();\n'
    "});\n",
)
CLIENT_DIFF = diff_block(
    "web/src/api/views.ts",
    "export async function renameView(name: string, next: string) {\n"
    '  return fetch(`/api/views/${name}`, { method: "PUT", body: next });\n'
    "}\n",
)


def seed_case(root, name="mock-path", clean=False):
    answer = (
        {"defects": [], "clean": True}
        if clean
        else {
            "defects": [
                defect(
                    "mock-path",
                    "web/e2e/views.spec.ts",
                    ["/api/views", "put"],
                    note="the mock matches /api/views exactly, so the PUT to /api/views/<name> is never intercepted",
                )
            ],
            "clean": False,
        }
    )
    return write_case(
        root,
        name,
        {"web/e2e/views.spec.ts": "spec body\n", "web/src/api/views.ts": "client body\n"},
        SPEC_DIFF + "\n" + CLIENT_DIFF + "\n",
        "Review the views rename flow: the client PUTs to /api/views/<name>.\n",
        answer,
    )


def test_a_valid_corpus_loads(tmp_path):
    seed_case(tmp_path, "defective")
    seed_case(tmp_path, "tidy", clean=True)
    cases = load_corpus(tmp_path)
    assert [c["case"] for c in cases] == ["defective", "tidy"]
    first = cases[0]
    assert first["brief"].startswith("Review the views rename flow")
    assert sorted(relative for relative, _ in first["files"]) == [
        "web/e2e/views.spec.ts",
        "web/src/api/views.ts",
    ]
    assert "diff --git a/web/e2e/views.spec.ts" in first["diff"]
    assert first["answer"]["defects"][0]["id"] == "mock-path"
    assert cases[1]["answer"] == {"defects": [], "clean": True}


def test_an_answer_naming_an_absent_file_refuses_by_name(tmp_path):
    seed_case(tmp_path)
    answer = json.loads((tmp_path / "mock-path" / "answer.json").read_text())
    answer["defects"][0]["file"] = "web/src/api/missing.ts"
    (tmp_path / "mock-path" / "answer.json").write_text(json.dumps(answer))
    with pytest.raises(ValueError, match="web/src/api/missing.ts"):
        load_corpus(tmp_path)


def test_answer_json_is_never_in_the_staged_file_list(tmp_path):
    seed_case(tmp_path)
    cases = load_corpus(tmp_path)
    staged = [relative for relative, _ in cases[0]["files"]]
    assert "answer.json" not in staged
    assert not any(relative.endswith("answer.json") for relative in staged)


def test_the_guard_refuses_credential_looking_case_files(tmp_path):
    seed_case(tmp_path)
    (tmp_path / "mock-path" / "web" / "src" / "auth.json").write_text("{}\n")
    (tmp_path / "mock-path" / "web" / "src" / "views.ts").write_text("export {};\n")
    (tmp_path / "mock-path" / "diff.patch").write_text(
        (tmp_path / "mock-path" / "diff.patch").read_text()
        + "\n"
        + diff_block("web/src/auth.json", "{}\n")
    )
    with pytest.raises(ValueError, match="credential"):
        load_corpus(tmp_path)


def test_a_clean_case_may_not_carry_defects_and_ids_must_be_distinct(tmp_path):
    seed_case(tmp_path, "tidy", clean=True)
    path = tmp_path / "tidy" / "answer.json"
    path.write_text(
        json.dumps({"defects": [defect("x", "web/e2e/views.spec.ts", ["m"])], "clean": True})
    )
    with pytest.raises(ValueError, match="clean case"):
        load_corpus(tmp_path)
    seed_case(tmp_path, "dupe")
    answer = json.loads((tmp_path / "dupe" / "answer.json").read_text())
    answer["defects"].append(defect("mock-path", "web/src/api/views.ts", ["b"]))
    (tmp_path / "dupe" / "answer.json").write_text(json.dumps(answer))
    with pytest.raises(ValueError, match="distinct"):
        load_corpus(tmp_path)


def test_an_empty_or_missing_corpus_refuses(tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        load_corpus(tmp_path / "absent")
    tmp_path.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="no cases"):
        load_corpus(tmp_path)


# --- authoring (K2) ---


def board_and_project(tmp_path):
    project = tmp_path / "project"
    board = project / "grid/board"
    project.mkdir(parents=True)
    return board, project


def corpus_of(tmp_path, cases=("mock-path",)):
    root = tmp_path / "corpus"
    root.mkdir(exist_ok=True)
    for name in cases:
        seed_case(root, name)
    return root


def test_authoring_writes_validated_tasks_briefs_and_answer_keys(tmp_path):
    from inference_grid.board.task import validate_task

    board, project = board_and_project(tmp_path)
    corpus = corpus_of(tmp_path, ["mock-path", "tidy"])
    (tmp_path / "corpus" / "tidy").joinpath("diff.patch").write_text(
        diff_block("docs/note.md", "a corrected note\n")
    )
    (tmp_path / "corpus" / "tidy" / "answer.json").write_text(
        json.dumps({"defects": [], "clean": True})
    )
    (tmp_path / "corpus" / "tidy" / "docs").mkdir()
    (tmp_path / "corpus" / "tidy" / "docs" / "note.md").write_text("a corrected note\n")
    created = calibration.author_calibration(board, project, corpus, ["go"], "seed-v1")
    assert [t["id"] for t in created["tasks"]] == [
        "calib-seed-v1-mock-path",
        "calib-seed-v1-tidy",
    ]
    for entry in created["tasks"]:
        task = validate_task(json.loads((board / (entry["id"] + ".json")).read_text()))
        assert task["category"] == "independent_review"
        assert task["author_family"] == "calibration"
        assert task["lanes"] == ["go"]
        assert task["artifacts"] == ["reply.txt"]
        brief = (project / task["brief"]).read_text()
        assert "Review the views rename flow" in brief
        assert "OUTPUT FORMAT, mandatory" in brief
        for relative in ("web/e2e/views.spec.ts",):
            assert any(relative in p for p in task["inputs"])
    answer_key = board / "calibration" / "seed-v1" / "mock-path.answer.json"
    assert json.loads(answer_key.read_text())["defects"][0]["id"] == "mock-path"
    assert (board / "calibration" / "seed-v1" / "manifest.json").is_file()


def test_the_answer_key_is_not_among_the_inputs(tmp_path):
    board, project = board_and_project(tmp_path)
    corpus = corpus_of(tmp_path)
    calibration.author_calibration(board, project, corpus, ["go"], "seed-v1")
    task = json.loads((board / "calib-seed-v1-mock-path.json").read_text())
    assert not any("answer.json" in p or "calibration" in p for p in task["inputs"])
    assert (project / "grid/board/review/calib-seed-v1-mock-path/diff.patch").is_file()


def test_two_runs_with_different_run_ids_coexist(tmp_path):
    board, project = board_and_project(tmp_path)
    corpus = corpus_of(tmp_path)
    calibration.author_calibration(board, project, corpus, ["go"], "run-a")
    calibration.author_calibration(board, project, corpus, ["go"], "run-b")
    ids = sorted(p.stem for p in board.glob("calib-*.json"))
    assert ids == [
        "calib-run-a-mock-path",
        "calib-run-b-mock-path",
    ]
    assert (board / "calibration" / "run-a" / "mock-path.answer.json").is_file()
    assert (board / "calibration" / "run-b" / "mock-path.answer.json").is_file()


def test_the_calibration_family_keeps_every_lane_eligible(tmp_path):
    from inference_grid.lanes.select import select_lane

    board, project = board_and_project(tmp_path)
    corpus = corpus_of(tmp_path)
    calibration.author_calibration(board, project, corpus, ["go", "go-kimi"], "seed-v1")
    task = json.loads((board / "calib-seed-v1-mock-path.json").read_text())
    lanes = {
        "go": {"family": "glm", "model": "glm-5.3-flash", "categories": ["independent_review"]},
        "go-kimi": {"family": "kimi", "model": "kimi-k3", "categories": ["independent_review"]},
    }
    ready = {"go": {"state": "ready"}, "go-kimi": {"state": "ready"}}
    choice = select_lane(
        {"category": "independent_review", "author_family": task["author_family"]},
        lanes,
        ready,
        [],
        0,
    )
    assert choice["lane"] in ("go", "go-kimi") and choice["reason"] == "selected"


def test_authoring_refuses_a_bad_run_id_or_lane_list(tmp_path):
    board, project = board_and_project(tmp_path)
    corpus = corpus_of(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        calibration.author_calibration(board, project, corpus, ["go"], "Run A")
    with pytest.raises(ValueError, match="lanes"):
        calibration.author_calibration(board, project, corpus, [], "run-a")


# --- scoring (K3) ---


def finding(location, text):
    return {"location": location, "input": "n/a", "expected": text, "observed": text}


def reply(verdict, findings):
    return json.dumps({"verdict": verdict, "findings": findings, "checked": ["a", "b", "c"]})


def write_packet(packets_root, task_id, body, aid=None):
    aid = aid or ("11111111-2222-3333-4444-" + str(abs(hash(task_id)) % 10**12).zfill(12))
    artifacts = packets_root / task_id / "20260914T120000" / "attempts" / aid / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "reply.txt").write_text(body)
    return aid


AID = "11111111-2222-3333-4444-555555555555"


def scoring_board(tmp_path):
    """Four cases: full recall, a miss, a clean pass, a clean case with a spurious finding."""
    board, project = board_and_project(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_case(
        corpus,
        "full-recall",
        {"web/e2e/views.spec.ts": "spec\n"},
        diff_block("web/e2e/views.spec.ts", "spec\n"),
        "brief\n",
        {
            "defects": [defect("d1", "web/e2e/views.spec.ts", ["put"], severity="high")],
            "clean": False,
        },
    )
    write_case(
        corpus,
        "a-miss",
        {"src/api/routes/views.py": "route\n"},
        diff_block("src/api/routes/views.py", "route\n"),
        "brief\n",
        {
            "defects": [defect("d2", "src/api/routes/views.py", ["delete"], severity="low")],
            "clean": False,
        },
    )
    write_case(
        corpus,
        "clean-pass",
        {"docs/note.md": "note\n"},
        diff_block("docs/note.md", "note\n"),
        "brief\n",
        {"defects": [], "clean": True},
    )
    write_case(
        corpus,
        "clean-spurious",
        {"docs/other.md": "note\n"},
        diff_block("docs/other.md", "note\n"),
        "brief\n",
        {"defects": [], "clean": True},
    )
    calibration.author_calibration(board, project, corpus, ["go"], "run1")
    packets = tmp_path / "packets"
    write_packet(
        packets,
        "calib-run1-full-recall",
        reply(
            "rejected",
            [
                finding(
                    "web/e2e/views.spec.ts (spec)",
                    "the mock intercepts /api/views but the client sends PUT /api/views/<name>",
                )
            ],
        ),
        aid=AID,
    )
    write_packet(
        packets,
        "calib-run1-a-miss",
        reply(
            "rejected",
            [
                finding("src/api/routes/views.py", "the handler returns the wrong status"),
                finding("src/api/other.py", "an unrelated concern"),
            ],
        ),
    )
    write_packet(packets, "calib-run1-clean-pass", reply("approved", []))
    write_packet(
        packets,
        "calib-run1-clean-spurious",
        reply("rejected", [finding("docs/other.md", "a made-up problem")]),
    )
    return board, packets


def test_scoring_reports_recall_false_positives_and_precision(tmp_path, capsys):
    board, packets = scoring_board(tmp_path)
    report = calibration.score_calibration(board, "run1", packets)
    lane = report["lanes"]["go"]
    assert lane["cases"] == 4
    assert lane["defects"] == 2 and lane["recalled"] == 1 and lane["recall"] == 0.5
    assert lane["false_positives"] == 3 and lane["findings"] == 4
    assert lane["precision"] == 0.25
    detail = {r["case"]: r for r in lane["cases_detail"]}
    assert detail["full-recall"]["recalled_ids"] == ["d1"] and detail["full-recall"]["accepted"]
    assert detail["a-miss"]["missed"] == ["d2"] and not detail["a-miss"]["accepted"]
    assert detail["a-miss"]["false_positives"] == 2
    assert detail["clean-pass"]["accepted"] and detail["clean-pass"]["false_positives"] == 0
    assert (
        not detail["clean-spurious"]["accepted"]
        and detail["clean-spurious"]["false_positives"] == 1
    )
    # Weighted recall: high (3) recalled, low (1) missed.
    assert lane["weighted_recall"] == 0.75
    assert (board / "calibration" / "run1" / "report.json").is_file()
    out = capsys.readouterr().out
    assert "| lane |" in out and "go" in out and "recall" in out


def test_weighted_recall_arithmetic(tmp_path):
    board, project = board_and_project(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_case(
        corpus,
        "two-defects",
        {"src/a.py": "a\n", "src/b.py": "b\n"},
        diff_block("src/a.py", "a\n") + "\n" + diff_block("src/b.py", "b\n"),
        "brief\n",
        {
            "defects": [
                defect("high-one", "src/a.py", ["alpha"], severity="high"),
                defect("low-one", "src/b.py", ["beta"], severity="low"),
            ],
            "clean": False,
        },
    )
    calibration.author_calibration(board, project, corpus, ["go"], "run1")
    packets = tmp_path / "packets"
    write_packet(
        packets,
        "calib-run1-two-defects",
        reply("rejected", [finding("src/b.py", "beta is mishandled here")]),
    )
    report = calibration.score_calibration(board, "run1", packets)
    lane = report["lanes"]["go"]
    assert lane["recalled"] == 1 and lane["defects"] == 2
    assert lane["recall"] == 0.5 and lane["weighted_recall"] == 0.25


def test_a_basename_location_still_recalls_and_clean_cases_score(tmp_path):
    board, project = board_and_project(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_case(
        corpus,
        "one-defect",
        {"src/deep/views.py": "a\n"},
        diff_block("src/deep/views.py", "a\n"),
        "brief\n",
        {"defects": [defect("d", "src/deep/views.py", ["nested"])], "clean": False},
    )
    write_case(
        corpus,
        "tidy",
        {"docs/n.md": "n\n"},
        diff_block("docs/n.md", "n\n"),
        "brief\n",
        {"defects": [], "clean": True},
    )
    calibration.author_calibration(board, project, corpus, ["go"], "run1")
    packets = tmp_path / "packets"
    write_packet(
        packets,
        "calib-run1-one-defect",
        reply("rejected", [finding("views.py (handle)", "the nested branch never runs")]),
    )
    write_packet(packets, "calib-run1-tidy", reply("approved", []))
    report = calibration.score_calibration(board, "run1", packets)
    lane = report["lanes"]["go"]
    assert lane["recalled"] == 1 and lane["false_positives"] == 0
    assert lane["recall"] == 1.0 and lane["precision"] == 1.0


def test_scoring_without_record_writes_no_ledger_rows(tmp_path):
    from inference_grid.ledger import Ledger

    board, packets = scoring_board(tmp_path)
    ledger = Ledger("sqlite:///" + str(tmp_path / "l.sqlite"))
    ledger.initialize()
    report = calibration.score_calibration(board, "run1", packets, record=False, ledger=ledger)
    assert not any(
        "record_error" in r for lane in report["lanes"].values() for r in lane["cases_detail"]
    )
    assert ledger.scorecard() == []


def test_record_true_records_calibration_outcomes(tmp_path):
    from sqlalchemy import select

    from inference_grid.ledger import Ledger, attempts as attempts_t, events, tasks as tasks_t

    board, packets = scoring_board(tmp_path)
    ledger = Ledger("sqlite:///" + str(tmp_path / "l.sqlite"))
    ledger.initialize()
    with ledger.engine.begin() as con:
        con.execute(
            tasks_t.insert().values(
                id="calib-run1-full-recall-20260914T120000-abc123", project="p", spec={}
            )
        )
        con.execute(
            attempts_t.insert().values(
                id=AID,
                task="calib-run1-full-recall-20260914T120000-abc123",
                account="a",
                generation=1,
                state="completed",
                estimate={},
                workspace="/w",
                receipt={},
                updated=0.0,
            )
        )
    report = calibration.score_calibration(board, "run1", packets, record=True, ledger=ledger)
    detail = {r["case"]: r for r in report["lanes"]["go"]["cases_detail"]}
    assert "record_error" not in detail["full-recall"]
    with ledger.engine.connect() as con:
        recorded = list(
            con.execute(select(events).where(events.c.kind == "outcome_recorded")).mappings()
        )
    assert len(recorded) == 1
    assert recorded[0]["attempt"] == AID
    assert recorded[0]["detail"]["category"] == "calibration"
    assert recorded[0]["detail"]["accepted"] is True


def test_record_true_without_a_ledger_refuses(tmp_path):
    board, packets = scoring_board(tmp_path)
    with pytest.raises(ValueError, match="ledger"):
        calibration.score_calibration(board, "run1", packets, record=True)


# --- seed corpus and CLI (K4) ---


CORPUS_V1 = Path(__file__).resolve().parents[1] / "calibration" / "example"


def test_the_example_corpus_loads_and_authors_its_tasks(tmp_path):
    from inference_grid.board.task import validate_task

    # The shipped corpus is a two-case example (one clean, one planted defect); the
    # operator's own corpus lives outside the repository and is not published.
    cases = load_corpus(CORPUS_V1)
    assert [c["case"] for c in cases] == ["clean-normalize", "sum-drops-last"]
    clean = {c["case"]: c["answer"]["clean"] for c in cases}
    assert clean == {"clean-normalize": True, "sum-drops-last": False}
    board, project = board_and_project(tmp_path)
    created = calibration.author_calibration(board, project, CORPUS_V1, ["go"], "seed-v1")
    assert [t["id"] for t in created["tasks"]] == [f"calib-seed-v1-{c['case']}" for c in cases]
    for entry in created["tasks"]:
        task = validate_task(json.loads((board / (entry["id"] + ".json")).read_text()))
        assert task["author_family"] == "calibration"
        assert all("answer" not in p for p in task["inputs"])


def run_cli(monkeypatch, tmp_path, name, payload, database):
    """Round-trip through cli.main(): the --json argument is a file path."""
    import io

    from inference_grid import cli

    argfile = tmp_path / (name + ".json")
    argfile.write_text(json.dumps(payload))
    monkeypatch.setattr(
        sys, "argv", ["inference-grid", "--database", database, name, "--json", str(argfile)]
    )
    buffer = io.StringIO()
    real = sys.stdout
    sys.stdout = buffer
    try:
        cli.main()
    finally:
        sys.stdout = real
    return buffer.getvalue()


def test_calibrate_round_trips_through_main(tmp_path, monkeypatch):
    board, project = board_and_project(tmp_path)
    out = run_cli(
        monkeypatch,
        tmp_path,
        "calibrate",
        {
            "board_dir": str(board),
            "project_root": str(project),
            "corpus_dir": str(CORPUS_V1),
            "lanes": ["go"],
            "run_id": "cli-v1",
        },
        "sqlite:///" + str(tmp_path / "board.sqlite"),
    )
    payload = json.loads(out)
    assert [t["id"] for t in payload["tasks"]] == [
        f"calib-cli-v1-{c}" for c in ("clean-normalize", "sum-drops-last")
    ]
    assert (board / "calibration" / "cli-v1" / "manifest.json").is_file()


def test_calibration_score_round_trips_through_main(tmp_path, monkeypatch):
    board, packets = scoring_board(tmp_path)
    out = run_cli(
        monkeypatch,
        tmp_path,
        "calibration-score",
        {
            "board_dir": str(board),
            "run_id": "run1",
            "packets_root": str(packets),
            "record": False,
        },
        "sqlite:///" + str(tmp_path / "score.sqlite"),
    )
    assert "| lane |" in out and "| go |" in out
    payload = json.loads(out[out.index('{\n  "run_id"') :])
    lane = payload["lanes"]["go"]
    assert lane["cases"] == 4 and lane["defects"] == 2 and lane["recalled"] == 1
    assert (board / "calibration" / "run1" / "report.json").is_file()
