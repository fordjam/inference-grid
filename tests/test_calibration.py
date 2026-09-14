"""Reviewer calibration: corpus format, authoring, scoring, seed corpus and CLI."""

import json

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
    return {"id": id, "file": file, "must_mention": must_mention, "severity": severity, "note": note}


SPEC_DIFF = diff_block(
    "web/e2e/views.spec.ts",
    'import { test, expect } from "@playwright/test";\n\n'
    'test("rename keeps the view", async ({ page }) => {\n'
    '  await page.route("/api/views", (route) => route.fulfill({ body: "[]" }));\n'
    '  await page.goto("/views");\n'
    "  await expect(page.getByRole(\"heading\")).toBeVisible();\n"
    "});\n",
)
CLIENT_DIFF = diff_block(
    "web/src/api/views.ts",
    "export async function renameView(name: string, next: string) {\n"
    "  return fetch(`/api/views/${name}`, { method: \"PUT\", body: next });\n"
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
    path.write_text(json.dumps({"defects": [defect("x", "web/e2e/views.spec.ts", ["m"])], "clean": True}))
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
        assert 'OUTPUT FORMAT, mandatory' in brief
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
