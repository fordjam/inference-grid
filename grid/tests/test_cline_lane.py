"""Coordinator acceptance tests for a staged cline.py artifact (board task lane-cline).

Runs in the packet's flat scratch directory beside the artifact and the reference lane
modules. Scenarios are written from the lane-cline brief, not from the author's tests:
a completed run must publish only expected artifacts with a verifiable receipt, an
artifact the agent deleted must be served from a controller-side snapshot, credential
and terminal-record problems must refuse, and the API key must never reach any file
under the attempt directory.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# The packet is a flat directory: register a minimal inference_grid.lanes package whose
# __path__ is this directory so cline.py's relative imports resolve to the flat files.
for _name, _path in (("inference_grid", []), ("inference_grid.lanes", [str(_HERE)])):
    if _name not in sys.modules:
        _package = types.ModuleType(_name)
        _package.__path__ = _path
        sys.modules[_name] = _package

from inference_grid.lanes import cline  # noqa: E402


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec"):
        raise unittest.SkipTest("macOS sandbox-exec required")


def temp_base():
    """Scratch under a base the sandbox profile allows writes in."""
    for base in ("/private/tmp", str(_HERE)):
        try:
            return tempfile.mkdtemp(prefix="clane-coord-", dir=base)
        except OSError:
            continue
    raise unittest.SkipTest("no writable sandbox base")


FAKE_CLINE = r"""#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
model = args[args.index("--model") + 1]
cwd = args[args.index("--cwd") + 1]
prompt = args[-1]

def emit(row):
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()

emit({"event": {"type": "iteration_start", "iteration": 1}})
emit({"event": {"type": "usage", "totalOutputTokens": 40}})
out = os.path.join(cwd, "out.py")
with open(out, "w") as stream:
    stream.write("VALUE = 7\n")
emit({"event": {"type": "iteration_end", "iteration": 1}})
if "servefromsnapshot" in prompt:
    served = os.path.join(cwd, os.pardir, "snapshots", "iter-1", "out.py")
    deadline = time.time() + 15
    while not os.path.exists(served) and time.time() < deadline:
        time.sleep(0.05)
    os.unlink(out)
if "wrongmodel" in prompt:
    model = "some-other-model"
if "noterminal" in prompt:
    sys.exit(0)
emit({"type": "run_result", "finishReason": "completed", "model": {"provider": "cline", "id": model}, "text": "wrote out.py"})
"""


class ClineLaneTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(temp_base())
        self.fake = self.base / "fake-cline"
        self.fake.write_text(FAKE_CLINE)
        os.chmod(self.fake, 0o755)
        self.cred = self.base / "cline-cred.json"
        self.cred.write_text(json.dumps({"api_key": "coord-key-1"}))
        os.chmod(self.cred, 0o600)
        self.key = "coord-key-1"

    def attempt(self, name):
        attempt_dir = self.base / name / "attempt"
        input_dir = self.base / name / "input"
        output_dir = self.base / name / "output"
        input_dir.mkdir(parents=True)
        attempt_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        return attempt_dir, input_dir, output_dir

    def stage(self, input_dir, prompt, expected=("out.py",)):
        (input_dir / "brief.txt").write_text(prompt + "\n")
        (input_dir / "expected.json").write_text(json.dumps(list(expected)))

    def request(self, input_dir, output_dir, model="cl-pass-1"):
        return {
            "model": model,
            "input_directory": str(input_dir),
            "output_directory": str(output_dir),
            "manifest_sha256": "0" * 64,
        }

    def lane(self, wall=25):
        return {
            "credential_path": str(self.cred),
            "executable": str(self.fake),
            "wall_seconds": wall,
        }

    def test_completed_run_publishes_expected_artifact_with_receipt(self):
        attempt_dir, input_dir, output_dir = self.attempt("ok")
        self.stage(input_dir, "write out.py")
        receipt, verdict = cline.run(
            self.request(input_dir, output_dir), self.lane(), attempt_dir,
            home=str(self.base / "ok" / "home"),
        )
        self.assertIsNotNone(receipt, verdict)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["actual_model"], "cl-pass-1")
        self.assertEqual(receipt["manifest_sha256"], "0" * 64)
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual((output_dir / "out.py").read_text(), "VALUE = 7\n")
        self.assertEqual(verdict["outcome"], "native_complete")
        self.assertEqual(verdict["artifact_sources"], {"out.py": "work"})

    def test_deleted_artifact_is_restored_from_the_complete_snapshot(self):
        attempt_dir, input_dir, output_dir = self.attempt("snap")
        self.stage(input_dir, "servefromsnapshot")
        receipt, verdict = cline.run(
            self.request(input_dir, output_dir), self.lane(), attempt_dir,
            home=str(self.base / "snap" / "home"),
        )
        self.assertIsNotNone(receipt, verdict)
        # The deletion guard restores the last complete snapshot into the workspace.
        self.assertTrue((attempt_dir / "work" / "out.py").is_file())
        self.assertEqual((output_dir / "out.py").read_text(), "VALUE = 7\n")
        self.assertEqual(verdict["artifact_sources"], {"out.py": "work"})
        self.assertEqual(verdict["restored_from_snapshot"], 1)

    def test_missing_or_wrong_mode_credential_refuses_without_running(self):
        attempt_dir, input_dir, output_dir = self.attempt("nocred")
        self.stage(input_dir, "write out.py")
        lane = self.lane()
        lane["credential_path"] = str(self.base / "absent.json")
        receipt, verdict = cline.run(self.request(input_dir, output_dir), lane, attempt_dir)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "credential file missing")
        self.assertFalse((attempt_dir / "native.jsonl").exists())

        os.chmod(self.cred, 0o644)
        attempt_dir2, input_dir2, output_dir2 = self.attempt("badmode")
        self.stage(input_dir2, "write out.py")
        receipt2, verdict2 = cline.run(self.request(input_dir2, output_dir2), self.lane(), attempt_dir2)
        self.assertIsNone(receipt2)
        self.assertEqual(verdict2["refusal"], "credential file must be mode 0o600")

    def test_terminal_model_mismatch_refuses(self):
        attempt_dir, input_dir, output_dir = self.attempt("wrongmodel")
        self.stage(input_dir, "wrongmodel")
        receipt, verdict = cline.run(self.request(input_dir, output_dir), self.lane(), attempt_dir)
        self.assertIsNone(receipt)
        self.assertIn("model_unqualified", verdict["refusal"])
        self.assertFalse((output_dir / "out.py").exists())

    def test_missing_terminal_record_refuses(self):
        attempt_dir, input_dir, output_dir = self.attempt("noterminal")
        self.stage(input_dir, "noterminal")
        receipt, verdict = cline.run(self.request(input_dir, output_dir), self.lane(), attempt_dir)
        self.assertIsNone(receipt)
        self.assertIn("missing_or_multiple_terminals", verdict["refusal"])

    def test_api_key_never_appears_under_the_attempt_directory(self):
        attempt_dir, input_dir, output_dir = self.attempt("leak")
        self.stage(input_dir, "servefromsnapshot")
        cline.run(self.request(input_dir, output_dir), self.lane(), attempt_dir)
        scanned = 0
        for path in attempt_dir.rglob("*"):
            if path.is_file():
                scanned += 1
                self.assertNotIn(self.key.encode(), path.read_bytes(), str(path))
        self.assertGreater(scanned, 3)  # profile, native log, snapshots and artifacts exist


if __name__ == "__main__":
    unittest.main()
