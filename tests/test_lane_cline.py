"""cline lane module against a fake ClinePass CLI; macOS sandbox required."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from inference_grid.lanes import cline


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec"):
        raise unittest.SkipTest("macOS sandbox-exec required")


def temp_root():
    """Scratch space under a base the sandbox profile allows writes in.

    /private/tmp is the reference base; when this process may not write there (a
    sandboxed checkout), the packet directory itself lies under ~/.grid-workspaces,
    the other allowed base, so it serves.
    """
    for base in (Path("/private/tmp"), Path(__file__).resolve().parent):
        try:
            return tempfile.TemporaryDirectory(dir=base)
        except OSError:
            continue
    raise unittest.SkipTest("no writable sandbox base for test scratch space")


FAKE_CLINE = r"""
import json, os, sys, time
args = sys.argv[1:]
model = args[args.index("--model") + 1]
cwd = args[args.index("--cwd") + 1]
prompt = args[-1]

def emit(row):
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()

emit({"event": {"type": "iteration_start", "iteration": 1}})
emit({"event": {"type": "usage", "totalOutputTokens": 12}})
target = os.path.join(cwd, "out.py")
with open(target, "w") as stream:
    stream.write("VALUE = 1\n")
emit({"event": {"type": "iteration_end", "iteration": 1}})
if "snapshot" in prompt:
    # Yield only once the controller-side snapshot holds the file, then delete the
    # original so the artifact can be served from the snapshot alone.
    served = os.path.join(cwd, os.pardir, "snapshots", "iter-1", "out.py")
    deadline = time.time() + 10
    while not os.path.exists(served) and time.time() < deadline:
        time.sleep(0.05)
    os.unlink(target)
if "removelater" in prompt:
    # Snapshot iteration 1 holds the file, then a later iteration deletes it: the
    # controller must restore the last complete snapshot, not lose the artifact.
    served = os.path.join(cwd, os.pardir, "snapshots", "iter-1", "out.py")
    deadline = time.time() + 10
    while not os.path.exists(served) and time.time() < deadline:
        time.sleep(0.05)
    os.unlink(target)
    emit({"event": {"type": "iteration_start", "iteration": 2}})
    emit({"event": {"type": "iteration_end", "iteration": 2}})
if "wrongmodel" in prompt:
    model = "wrong-model"
if "noterminal" not in prompt:
    emit({"type": "run_result", "finishReason": "completed", "model": {"provider": "cline", "id": model}, "text": "done"})
"""


class ClineLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_root()
        root = Path(self.tmp.name)
        self.saved_home = os.environ.get("HOME")
        self.key = "sk-cline-test-0000000000000000"
        self.credential = root / "credential.json"
        self.write_credential(0o600)
        self.home = root / "home"
        self.home.mkdir()
        self.fake = root / "fake_cline.py"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_CLINE)
        self.fake.chmod(0o755)
        self.lane = {
            "executable": str(self.fake),
            "wall_seconds": 30,
            "credential_path": str(self.credential),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def write_credential(self, mode):
        self.credential.write_text(json.dumps({"api_key": self.key}))
        os.chmod(self.credential, mode)

    def attempt(self, prompt, expected=("out.py",)):
        root = Path(self.tmp.name)
        n = len(list(root.glob("attempt*")))
        attempt = root / f"attempt{n}"
        (attempt / "inputs").mkdir(parents=True)
        (attempt / "artifacts").mkdir()
        (attempt / "inputs/brief.txt").write_text(prompt)
        if expected is not None:
            (attempt / "inputs/expected.json").write_text(json.dumps(list(expected)))
        request = {
            "attempt": "a",
            "generation": 1,
            "model": "glm-5.3-flash",
            "manifest_sha256": "m" * 64,
            "input_directory": str(attempt / "inputs"),
            "output_directory": str(attempt / "artifacts"),
        }
        return request, attempt

    def run_lane(self, request, attempt, **kwargs):
        return cline.run(request, self.lane, attempt, home=self.home, **kwargs)

    def test_success_from_work(self):
        request, attempt = self.attempt("write out.py please")
        receipt, verdict = self.run_lane(request, attempt)
        self.assertIsNone(verdict["refusal"])
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual((attempt / "artifacts/out.py").read_text(), "VALUE = 1\n")
        self.assertEqual(receipt["actual_model"], "glm-5.3-flash")
        self.assertEqual(receipt["finish_reason"], "stop")
        self.assertEqual(verdict["outcome"], "native_complete")
        self.assertEqual(verdict["supervisor"]["reason"], "process_exited")
        self.assertEqual(verdict["progress"]["iterations_observed"], 1)
        self.assertEqual(verdict["progress"]["output_tokens_reported"], 12)
        self.assertEqual(verdict["artifact_sources"], {"out.py": "work"})

    def test_deleted_after_a_complete_snapshot_is_restored(self):
        # A passing artifact destroyed in a later iteration cannot erase the evidence:
        # the newest snapshot that held everything is restored into the workspace and
        # the verdict records which iteration served.
        request, attempt = self.attempt("write out.py, then removelater")
        receipt, verdict = self.run_lane(request, attempt)
        self.assertIsNone(verdict["refusal"], verdict)
        self.assertEqual(verdict["restored_from_snapshot"], 1)
        self.assertTrue((attempt / "work/out.py").is_file())
        self.assertEqual((attempt / "artifacts/out.py").read_text(), "VALUE = 1\n")
        self.assertEqual(verdict["artifact_sources"], {"out.py": "work"})

    def test_success_served_from_snapshot(self):
        request, attempt = self.attempt("write out.py, then snapshot it")
        receipt, verdict = self.run_lane(request, attempt)
        self.assertIsNone(verdict["refusal"])
        # The deletion guard restores the last complete snapshot into the workspace, so
        # the published bytes always have a workspace-side provenance too.
        self.assertTrue((attempt / "work/out.py").is_file())
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual((attempt / "artifacts/out.py").read_text(), "VALUE = 1\n")
        self.assertEqual(verdict["artifact_sources"], {"out.py": "work"})
        self.assertEqual(verdict["restored_from_snapshot"], 1)

    def test_missing_credential(self):
        request, attempt = self.attempt("write out.py please")
        lane = dict(self.lane, credential_path=str(Path(self.tmp.name) / "absent.json"))
        receipt, verdict = cline.run(request, lane, attempt, home=self.home)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "credential file missing")

    def test_wrong_credential_mode(self):
        self.write_credential(0o644)
        request, attempt = self.attempt("write out.py please")
        receipt, verdict = self.run_lane(request, attempt)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "credential file must be mode 0o600")

    def test_missing_expected_json(self):
        request, attempt = self.attempt("write out.py please", expected=None)
        receipt, verdict = self.run_lane(request, attempt)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "expected.json is required for this lane")

    def test_model_mismatch(self):
        request, attempt = self.attempt("write out.py, wrongmodel please")
        receipt, verdict = self.run_lane(request, attempt)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "cline unqualified: model_unqualified")

    def test_missing_terminal_record(self):
        request, attempt = self.attempt("write out.py, noterminal please")
        receipt, verdict = self.run_lane(request, attempt)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "cline unqualified: missing_or_multiple_terminals")

    def test_key_never_written_under_attempt(self):
        request, attempt = self.attempt("write out.py, then snapshot it")
        receipt, verdict = self.run_lane(request, attempt)
        self.assertIsNotNone(receipt)
        needle = self.key.encode()
        for path in attempt.rglob("*"):
            if path.is_file():
                self.assertNotIn(needle, path.read_bytes(), str(path))
        self.assertNotIn("CLINE_API_KEY", os.environ)
        self.assertEqual(os.environ.get("HOME"), self.saved_home)

    def test_unsafe_artifact_names_refused(self):
        for name in ("../native.jsonl", "/tmp/escape.txt", "sub/../../x"):
            request, attempt = self.attempt("write out.py please", expected=[name])
            receipt, verdict = self.run_lane(request, attempt)
            self.assertIsNone(receipt, name)
            self.assertEqual(
                verdict["refusal"],
                "expected.json must list safe relative artifact names",
                name,
            )


if __name__ == "__main__":
    unittest.main()
