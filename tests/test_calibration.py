"""Reviewer calibration: corpus format, authoring, scoring, seed corpus and CLI."""

import json

import pytest

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
