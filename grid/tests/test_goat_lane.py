"""Coordinator acceptance tests for a staged goat.py artifact (board task lane-goat).

Runs in the packet's flat scratch directory beside the artifact and the reference lane
modules. Scenarios are written from the lane-goat brief: a reply-only task publishes
the native final text, declared artifacts arrive with a verifiable receipt and the
effort module is never one of them, anything but effort "low" on every model request
refuses, a modified Command Code login refuses, and a wall overrun refuses as an
interrupted run.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

import goat  # the artifact under test, staged flat beside this file
import sandbox  # the reference sandbox module staged with the packet


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec"):
        raise unittest.SkipTest("macOS sandbox-exec required")


def temp_base():
    for base in ("/private/tmp", str(Path(__file__).resolve().parent)):
        try:
            return tempfile.mkdtemp(prefix="glane-coord-", dir=base)
        except OSError:
            continue
    raise unittest.SkipTest("no writable sandbox base")


FAKE_CMD = r"""#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
model = args[args.index("--model") + 1]
prompt = args[args.index("--print") + 1]

def emit(row):
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()

effort = "high" if "efforthigh" in prompt else "low"
emit({"type": "event", "event": {"type": "model_request_end", "model": model, "effort": effort, "usage": {"inputTokens": 500, "outputTokens": 20}}})
if "wrongmodel" in prompt:
    emit({"type": "event", "event": {"type": "model_request_end", "model": "not-the-model", "effort": "low", "usage": {"inputTokens": 1, "outputTokens": 1}}})
if "tamper" in prompt:
    config = os.path.join(os.environ["HOME"], ".commandcode", "config.json")
    with open(config, "a") as stream:
        stream.write('{"tampered": true}\n')
if "wall" in prompt:
    time.sleep(30)
    sys.exit(0)
if "artifact" in prompt:
    with open(os.path.join(os.getcwd(), "out.py"), "w") as stream:
        stream.write("VALUE = 9\n")
emit({"type": "result", "subtype": "success", "stopReason": "end_turn", "finalText": "GOAT says hello", "usage": {"inputTokens": 500, "outputTokens": 20}})
"""


class GoatLaneTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(temp_base())
        self.fake = self.base / "fake-cmd"
        self.fake.write_text(FAKE_CMD)
        os.chmod(self.fake, 0o755)

    def attempt(self, name, expected=None, prompt="plain"):
        root = self.base / name
        input_dir = root / "input"
        output_dir = root / "output"
        attempt_dir = root / "attempt"
        input_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        attempt_dir.mkdir(parents=True)
        (input_dir / "brief.txt").write_text(prompt + "\n")
        if expected is not None:
            (input_dir / "expected.json").write_text(json.dumps(expected))
        return attempt_dir, input_dir, output_dir

    def request(self, input_dir, output_dir, model="glm-5.3-flash"):
        return {
            "model": model,
            "input_directory": str(input_dir),
            "output_directory": str(output_dir),
            "manifest_sha256": "1" * 64,
        }

    def lane(self, wall=25):
        return {"executable": str(self.fake), "wall_seconds": wall}

    def home_with_login(self, root):
        home = root / "home"
        (home / ".commandcode").mkdir(parents=True)
        (home / ".commandcode" / "config.json").write_text('{"theme": "dark"}\n')
        (home / ".commandcode" / "auth.json").write_text('{"token": "x"}\n')
        return home

    def test_reply_only_run_publishes_the_final_text(self):
        attempt_dir, input_dir, output_dir = self.attempt("reply")
        home = self.home_with_login(self.base / "reply")
        receipt, verdict = goat.run(
            self.request(input_dir, output_dir), self.lane(), attempt_dir, home=home
        )
        self.assertIsNotNone(receipt, verdict)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["actual_model"], "glm-5.3-flash")
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["reply.txt"])
        self.assertEqual((output_dir / "reply.txt").read_text(), "GOAT says hello")
        self.assertEqual(verdict["effort"], "low")

    def test_declared_artifact_arrives_and_the_effort_module_is_not_one(self):
        attempt_dir, input_dir, output_dir = self.attempt("files", expected=["out.py"], prompt="artifact")
        home = self.home_with_login(self.base / "files")
        receipt, verdict = goat.run(
            self.request(input_dir, output_dir), self.lane(), attempt_dir, home=home
        )
        self.assertIsNotNone(receipt, verdict)
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual((output_dir / "out.py").read_text(), "VALUE = 9\n")
        self.assertFalse((output_dir / "grid-effort.mjs").exists())

    def test_any_high_effort_request_refuses(self):
        attempt_dir, input_dir, output_dir = self.attempt("high", prompt="efforthigh")
        home = self.home_with_login(self.base / "high")
        receipt, verdict = goat.run(
            self.request(input_dir, output_dir), self.lane(), attempt_dir, home=home
        )
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "effort evidence missing")

    def test_model_mismatch_refuses(self):
        attempt_dir, input_dir, output_dir = self.attempt("model", prompt="wrongmodel")
        home = self.home_with_login(self.base / "model")
        receipt, verdict = goat.run(
            self.request(input_dir, output_dir), self.lane(), attempt_dir, home=home
        )
        self.assertIsNone(receipt)
        self.assertIn("model_unqualified", verdict["refusal"])

    def test_modified_command_code_login_refuses(self):
        # The write sandbox only permits writes under the workspace, so the login being
        # guarded is staged inside it through the input directory: the fake appends to
        # $HOME/.commandcode/config.json there between the module's before/after digests.
        attempt_dir, input_dir, output_dir = self.attempt("tamper", prompt="tamper")
        login = input_dir / ".commandcode"
        login.mkdir(parents=True)
        (login / "config.json").write_text('{"theme": "dark"}\n')
        receipt, verdict = goat.run(
            self.request(input_dir, output_dir),
            self.lane(),
            attempt_dir,
            home=str(attempt_dir / "work"),
        )
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "user configuration changed")

    def test_wall_overrun_refuses_as_interrupted(self):
        attempt_dir, input_dir, output_dir = self.attempt("wall", prompt="wall")
        home = self.home_with_login(self.base / "wall")
        receipt, verdict = goat.run(
            self.request(input_dir, output_dir), self.lane(wall=2), attempt_dir, home=home
        )
        self.assertIsNone(receipt)
        self.assertEqual(verdict["supervisor"]["reason"], "wall_deadline")
        self.assertIn("interrupted", verdict["refusal"])


if __name__ == "__main__":
    unittest.main()
