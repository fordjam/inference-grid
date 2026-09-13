"""Board task authoring: validate and write one task file with its brief; never overwrites.

Pure file work: the task is validated by board.task before anything is written, the brief
is created empty for the author to fill, and an independent_review task also gets the
review schema acceptance test beside the board. Existing task files and briefs are
refused; the schema test is shared by every review task, so an existing one is reused
untouched (never overwritten) rather than blocking the second review task on a board.
"""

import json
from pathlib import Path

from .task import validate_task

SCHEMA_TEST = '''"""Acceptance test for a review artifact: strict JSON verdict with demonstrable findings."""

import json
import unittest
from pathlib import Path


class ReviewSchemaTests(unittest.TestCase):
    def test_reply_is_a_review(self):
        text = Path("reply.txt").read_text().strip()
        if text.startswith("```"):
            body = text[3:]
            newline = body.find("\\n")
            body = body[newline + 1 :] if newline != -1 else body[body.find("{") :]
            if body.rstrip().endswith("```"):
                body = body.rstrip()[:-3]
            text = body.strip()
        review = json.loads(text)
        self.assertEqual(set(review), {"verdict", "findings", "checked"})
        self.assertIn(review["verdict"], ("approved", "rejected"))
        self.assertIsInstance(review["checked"], list)
        self.assertGreaterEqual(len(review["checked"]), 3)
        for finding in review["findings"]:
            self.assertEqual(set(finding), {"location", "input", "expected", "observed"})
            self.assertTrue(all(isinstance(v, str) and v.strip() for v in finding.values()))
        self.assertEqual(review["verdict"] == "rejected", len(review["findings"]) > 0)


if __name__ == "__main__":
    unittest.main()
'''


def new_task(board_dir, project_root, task):
    """Write one validated task file plus its empty brief; returns the created paths."""
    board_dir = Path(board_dir)
    project_root = Path(project_root)
    task = validate_task(task)
    board_file = board_dir / (task["id"] + ".json")
    brief = project_root / task["brief"]
    schema_test = board_dir.parent / "tests" / "test_review_schema.py"
    for label, path in [("task file", board_file), ("brief", brief)]:
        if path.exists():
            raise FileExistsError(f"{label} already exists: {path}")
    board_dir.mkdir(parents=True, exist_ok=True)
    brief.parent.mkdir(parents=True, exist_ok=True)
    board_file.write_text(json.dumps(task, indent=1) + "\n")
    brief.write_text("")
    created = {"task": str(board_file), "brief": str(brief)}
    if task["category"] == "independent_review":
        if not schema_test.exists():
            schema_test.parent.mkdir(parents=True, exist_ok=True)
            schema_test.write_text(SCHEMA_TEST)
        created["schema_test"] = str(schema_test)
    return created
