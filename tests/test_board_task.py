import copy
import unittest

from inference_grid.board.task import validate_task


def task(**kw):
    base = dict(
        id="lanes-go-py",
        category="tests_multi_file",
        brief="brief.txt",
        inputs=["brief.txt", "reference_zcode.py"],
        tests=["test_go.py"],
        artifacts=["go.py", "test_go.py"],
        lanes=["zcode", "goat"],
        author_family=None,
        budget={"wall_seconds": 900, "output_bytes": 2000000, "thinking_tokens": None},
        state="ready",
        blocked_reason=None,
    )
    base.update(kw)
    return base


class TaskTests(unittest.TestCase):
    def test_valid_and_copied(self):
        raw = task()
        snap = copy.deepcopy(raw)
        out = validate_task(raw)
        self.assertEqual(out, raw)
        self.assertEqual(raw, snap)
        self.assertIsNot(out["inputs"], raw["inputs"])
        self.assertIsNot(out["budget"], raw["budget"])
        self.assertEqual(
            validate_task(
                task(
                    state="blocked",
                    blocked_reason="weekly reset",
                    author_family="glm",
                    budget={"wall_seconds": 30, "output_bytes": 1, "thinking_tokens": 0},
                )
            )["state"],
            "blocked",
        )

    def test_refusals(self):
        bad = [
            ("task", "x"),
            ("task", dict(task(), extra=1)),
            ("task", {k: v for k, v in task().items() if k != "tests"}),
            ("id", task(id="Lanes")),
            ("id", task(id="")),
            ("category", task(category="review")),
            ("brief", task(brief="brief.md")),
            ("inputs", task(inputs=["brief.txt", "../x"])),
            ("inputs", task(inputs=["brief.txt", "/abs"])),
            ("inputs", task(inputs=["brief.txt", "brief.txt"])),
            ("inputs", task(inputs=["reference.py"])),
            ("tests", task(tests=["a\\b.py"])),
            ("artifacts", task(artifacts=[])),
            ("artifacts", task(artifacts=["a.py", "a.py"])),
            ("lanes", task(lanes=[])),
            ("lanes", task(lanes=["ZCode"])),
            ("author_family", task(author_family="")),
            ("budget", task(budget={"wall_seconds": 900})),
            (
                "budget",
                task(budget={"wall_seconds": 10, "output_bytes": 1, "thinking_tokens": None}),
            ),
            (
                "budget",
                task(budget={"wall_seconds": True, "output_bytes": 1, "thinking_tokens": None}),
            ),
            (
                "budget",
                task(budget={"wall_seconds": 900, "output_bytes": 1, "thinking_tokens": -1}),
            ),
            ("state", task(state="queued")),
            ("blocked_reason", task(state="blocked")),
            ("blocked_reason", task(blocked_reason="why")),
            ("blocked_reason", task(state="blocked", blocked_reason="x" * 301)),
        ]
        for key, raw in bad:
            with self.assertRaises(ValueError, msg=str(raw)[:100]) as ctx:
                validate_task(raw)
            self.assertTrue(str(ctx.exception).startswith(key + ":"), (key, str(ctx.exception)))


if __name__ == "__main__":
    unittest.main()
