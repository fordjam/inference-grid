"""Codex lane module against a fake CLI; macOS sandbox required (as in production)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from inference_grid.lanes import codex


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec") or not Path("/private/tmp").is_dir():
        raise unittest.SkipTest("macOS sandbox-exec and /private/tmp required")


FAKE_CLI = r"""
import json, os, sys, time
args = sys.argv[1:]
prompt = args[args.index("exec") + 2]
cwd = os.getcwd()
model = os.environ.get("FAKE_MODEL", "codex-5")
if "slow" in prompt:
    time.sleep(30)
if "crash" in prompt:
    sys.stdout.write(json.dumps({"type": "thread.started", "model": model}) + "\n")
    sys.stderr.write("boom\n")
    sys.exit(2)
events = [{"type": "thread.started", "model": model}]
if "write" in prompt:
    open(os.path.join(cwd, "out.py"), "w").write("VALUE = 1\n")
    events.append({"type": "item.completed", "item": {"item_type": "file_change", "path": "out.py"}})
text = "OK done"
events.append({"type": "item.completed", "item": {"item_type": "agent_message", "text": text}})
events.append({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}})
for event in events:
    sys.stdout.write(json.dumps(event) + "\n")
"""


class CodexLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        root = Path(self.tmp.name)
        self.home = root / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.cli = root / "fake_codex"
        self.cli.write_text("#!/usr/bin/env python3\n" + FAKE_CLI)
        self.cli.chmod(0o755)
        self.lane = {"executable": str(self.cli), "wall_seconds": 30}

    def tearDown(self):
        self.tmp.cleanup()

    def attempt(self, prompt, expected=None):
        root = Path(self.tmp.name)
        n = len(list(root.glob("attempt*")))
        attempt = root / f"attempt{n}"
        (attempt / "inputs").mkdir(parents=True)
        (attempt / "artifacts").mkdir()
        (attempt / "inputs/brief.txt").write_text(prompt)
        if expected is not None:
            (attempt / "inputs/expected.json").write_text(json.dumps(expected))
        request = {
            "attempt": "a",
            "generation": 1,
            "model": "codex-5",
            "manifest_sha256": "m" * 64,
            "input_directory": str(attempt / "inputs"),
            "output_directory": str(attempt / "artifacts"),
        }
        return request, attempt

    def test_happy_path_publishes_reply_and_artifacts(self):
        request, attempt = self.attempt("write out.py then say done", expected=["out.py"])
        receipt, verdict = codex.run(request, self.lane, attempt, home=self.home)
        self.assertIsNone(verdict["refusal"], verdict)
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual((attempt / "artifacts/out.py").read_text(), "VALUE = 1\n")
        self.assertEqual(receipt["actual_model"], "codex-5")
        self.assertEqual(verdict["classify"], {"outcome": "native_complete", "reason": "verified_native_shape"})
        self.assertEqual(verdict["usage"], {"input_tokens": 7, "output_tokens": 3})
        self.assertTrue((attempt / "native.jsonl").is_file())

    def test_reply_only_task_publishes_the_terminal_text(self):
        request, attempt = self.attempt("just say done")
        receipt, verdict = codex.run(request, self.lane, attempt, home=self.home)
        self.assertIsNone(verdict["refusal"], verdict)
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["reply.txt"])
        self.assertEqual((attempt / "artifacts/reply.txt").read_text(), "OK done")

    def test_wall_deadline_holds_with_the_supervisor_reason(self):
        request, attempt = self.attempt("slow")
        lane = dict(self.lane, wall_seconds=1)
        receipt, verdict = codex.run(request, lane, attempt, home=self.home)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["supervisor"]["reason"], "wall_deadline")
        self.assertEqual(verdict["refusal"], "codex interrupted: wall_deadline")

    def test_unexpected_model_is_refused(self):
        request, attempt = self.attempt("say done")
        previous = os.environ.get("FAKE_MODEL")
        os.environ["FAKE_MODEL"] = "codex-other"
        try:
            receipt, verdict = codex.run(request, self.lane, attempt, home=self.home)
        finally:
            if previous is None:
                os.environ.pop("FAKE_MODEL", None)
            else:
                os.environ["FAKE_MODEL"] = previous
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "codex unqualified: model_unqualified")

    def test_crashing_cli_is_a_process_failure(self):
        request, attempt = self.attempt("crash")
        receipt, verdict = codex.run(request, self.lane, attempt, home=self.home)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "codex unqualified: process_failed")


class ClassifyCodexTests(unittest.TestCase):
    def test_missing_or_multiple_terminals(self):
        events = [
            {"type": "thread.started", "model": "codex-5"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
        result = codex.classify_codex(events, {"reason": "process_exited", "returncode": 0}, "codex-5")
        self.assertEqual(result["reason"], "missing_or_multiple_terminals")

    def test_events_after_terminal_are_refused(self):
        events = [
            {"type": "thread.started", "model": "codex-5"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"type": "item.completed", "item": {"item_type": "agent_message", "text": "late"}},
        ]
        result = codex.classify_codex(events, {"reason": "process_exited", "returncode": 0}, "codex-5")
        self.assertEqual(result["reason"], "missing_or_multiple_terminals")

    def test_wrong_model_is_refused(self):
        events = [
            {"type": "thread.started", "model": "codex-mini"},
            {"type": "item.completed", "item": {"item_type": "agent_message", "text": "hi"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
        result = codex.classify_codex(events, {"reason": "process_exited", "returncode": 0}, "codex-5")
        self.assertEqual(result["reason"], "model_unqualified")

    def test_empty_terminal_text_is_refused(self):
        events = [
            {"type": "thread.started", "model": "codex-5"},
            {"type": "item.completed", "item": {"item_type": "agent_message", "text": "  "}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
        result = codex.classify_codex(events, {"reason": "process_exited", "returncode": 0}, "codex-5")
        self.assertEqual(result["reason"], "empty_terminal_text")


if __name__ == "__main__":
    unittest.main()
