"""Board task authoring: validate and write one task file with its brief.

Pure file work: the task is validated by board.task before anything is written, the brief
is created empty for the author to fill, and an independent_review task also gets the
review schema acceptance test beside the board. Existing task files and briefs are
refused; the schema test is shared by every review task, so an existing one is reused
untouched (never overwritten) rather than blocking the second review task on a board.

retry_task is the authorised-retry path (BOARD.md: retries only as new tasks with a
recorded change): it writes a new task file and a copied brief, and rewrites the
predecessor's blocked_reason to `superseded: <new id> — <change>` — the one edit
board-new ever makes to an existing file.
"""

import json
import re
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


def stored_task(board_dir, task_id):
    """The task dict stored for task_id, or None when the file is missing or unreadable."""
    try:
        return json.loads((Path(board_dir) / (task_id + ".json")).read_text())
    except (OSError, ValueError):
        return None


def next_retry_id(board_dir, task_id):
    """The id a retry of task_id takes; a chain of retries reads as a sequence.

    When task_id already ends in `-<n>` and that task is itself a retry — its
    blocked_reason starts with `superseded:`, or its own predecessor (the same id with
    one lower suffix, or the plain id for `-2`) sits on the board — the numeric suffix
    increments (`x-2` → `x-3`), skipping ids already taken. A first retry of a plain id
    still gets `-2`. Existing files are never renamed, so a stacked legacy id such as
    `x-3-2` keeps its shape and its retry is `x-3-3`.
    """
    board_dir = Path(board_dir)
    match = re.fullmatch(r"(.+)-(\d+)", task_id)
    if match:
        base, n = match.group(1), int(match.group(2))
        predecessor = base if n == 2 else f"{base}-{n - 1}"
        task = stored_task(board_dir, task_id)
        continues_chain = (
            str((task or {}).get("blocked_reason") or "").startswith("superseded:")
            or (board_dir / (predecessor + ".json")).exists()
        )
        if continues_chain:
            n += 1
            while (board_dir / f"{base}-{n}.json").exists():
                n += 1
            return f"{base}-{n}"
    n = 2
    while (board_dir / f"{task_id}-{n}.json").exists():
        n += 1
    return f"{task_id}-{n}"


def retry_task(board_dir, project_root, retry, change, budget=None, lanes=None, author_family=None):
    """Authorise a retry of a board task: a new task and copied brief; the predecessor is superseded.

    The recorded change lives only in the predecessor's `superseded:` reason; the new task
    starts clean with any overrides applied. The predecessor may be ready, dispatched or
    blocked — never accepted, passed or review_pending.
    """
    if not isinstance(change, str) or not change.strip():
        raise ValueError("a recorded change is required for a retry")
    board_dir = Path(board_dir)
    project_root = Path(project_root)
    source_path = board_dir / (retry + ".json")
    predecessor = validate_task(json.loads(source_path.read_text()))
    if predecessor["state"] in ("accepted", "passed", "review_pending"):
        raise ValueError(
            f"predecessor {retry} is {predecessor['state']}; only a ready, dispatched or "
            "blocked task can be superseded"
        )
    new_id = next_retry_id(board_dir, retry)
    old_brief = Path(predecessor["brief"])
    new_brief_rel = str(old_brief.with_name(new_id + old_brief.suffix))
    task = dict(predecessor)
    task["id"] = new_id
    task["brief"] = new_brief_rel
    task["inputs"] = [
        new_brief_rel if p == predecessor["brief"] else p for p in predecessor["inputs"]
    ]
    if budget is not None:
        task["budget"] = budget
    if lanes is not None:
        task["lanes"] = lanes
    if author_family is not None:
        task["author_family"] = author_family
    task["state"] = "ready"
    task["blocked_reason"] = None
    task = validate_task(task)
    new_file = board_dir / (new_id + ".json")
    new_brief = project_root / new_brief_rel
    for label, path in [("task file", new_file), ("brief", new_brief)]:
        if path.exists():
            raise FileExistsError(f"{label} already exists: {path}")
    superseded = validate_task(
        dict(predecessor, state="blocked", blocked_reason=f"superseded: {new_id} — {change}"[:300])
    )
    board_dir.mkdir(parents=True, exist_ok=True)
    new_brief.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_text(json.dumps(task, indent=1) + "\n")
    new_brief.write_text((project_root / predecessor["brief"]).read_text())
    source_path.write_text(json.dumps(superseded, indent=1) + "\n")
    return {
        "id": new_id,
        "task": str(new_file),
        "brief": str(new_brief),
        "superseded": str(source_path),
    }
