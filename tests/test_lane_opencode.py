"""opencode lane module against a fake OpenCode CLI; macOS sandbox required."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from inference_grid.lanes import opencode
from inference_grid.lanes.packet import OpencodeAdapter
from inference_grid.receipts import validate_receipt

SCRATCH = Path("/private/tmp")


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec") or not Path("/private/tmp").is_dir():
        raise unittest.SkipTest("macOS sandbox-exec and /private/tmp required")
    global SCRATCH
    # Sandbox profiles only bless workspaces under /private/tmp or ~/.grid-workspaces.
    try:
        probe = tempfile.TemporaryDirectory(dir=SCRATCH)
    except OSError:
        SCRATCH = Path.cwd()
    else:
        probe.cleanup()


FAKE_OPENCODE = r"""#!/usr/bin/env python3
import json, os, signal, sys, time

args = sys.argv[1:]
model = args[args.index("--model") + 1]
prompt = args[-1]

def emit(row):
    sys.stdout.write(json.dumps(row) + "\n")

if "sleep" in prompt:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
if "tamper" in prompt:
    with open(os.path.join(os.environ["HOME"], ".local/share/opencode/auth.json"), "a") as stream:
        stream.write("tampered\n")
if "malformed" in prompt:
    sys.stdout.write("not-json\n")
event_model = "glm-other" if "wrong model" in prompt else model
emit({"type": "step_start", "sessionID": "ses_op-1"})
if "no text" not in prompt:
    emit({"type": "text", "text": "OPENCODE done"})
if "write" in prompt:
    with open(os.path.join(os.getcwd(), "out.py"), "w") as stream:
        stream.write("VALUE = 1\n")
if "no finish" not in prompt:
    emit({"type": "step_finish", "reason": "stop", "modelID": event_model,
          "tokens": {"inputTokens": 11, "outputTokens": 4}})
"""


class OpencodeLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        root = Path(self.tmp.name)
        self.home = root / "home"
        (self.home / ".local/share/opencode").mkdir(parents=True)
        (self.home / ".local/share/opencode/auth.json").write_text('{"key": "test"}\n')
        (self.home / ".config/opencode").mkdir(parents=True)
        (self.home / ".config/opencode/opencode.json").write_text("{}\n")
        self.cli = root / "fake_opencode.py"
        self.cli.write_text(FAKE_OPENCODE)
        self.cli.chmod(0o755)
        self.lane = {"executable": str(self.cli), "wall_seconds": 30}
        self.no_denied = ()

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
            "model": "glm-5.3-flash",
            "manifest_sha256": "m" * 64,
            "input_directory": str(attempt / "inputs"),
            "output_directory": str(attempt / "artifacts"),
        }
        return request, attempt

    def test_reply_only_happy_path(self):
        request, attempt = self.attempt("reply please")
        manifest = hashlib.sha256(b"manifest").hexdigest()
        request["manifest_sha256"] = manifest
        receipt, verdict = opencode.run(
            request, self.lane, attempt, home=self.home, deny_roots=self.no_denied
        )
        self.assertIsNone(verdict["refusal"])
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["reply.txt"])
        self.assertEqual((attempt / "artifacts/reply.txt").read_text(), "OPENCODE done")
        self.assertEqual(verdict["supervisor"]["reason"], "process_exited")
        self.assertEqual(verdict["usage"], {"inputTokens": 11, "outputTokens": 4})
        self.assertEqual(verdict["progress"], {"events": 3, "steps": 1})
        self.assertEqual(receipt["finish_reason"], "stop")
        self.assertEqual(receipt["actual_model"], "glm-5.3-flash")
        self.assertEqual(validate_receipt(receipt, "glm-5.3-flash", manifest), [])

    def test_file_artifacts_with_digests(self):
        request, attempt = self.attempt("write out.py please", expected=["out.py"])
        receipt, _ = opencode.run(
            request, self.lane, attempt, home=self.home, deny_roots=self.no_denied
        )
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual(
            receipt["artifacts"][0]["sha256"], hashlib.sha256(b"VALUE = 1\n").hexdigest()
        )

    def test_model_mismatch_refused(self):
        request, attempt = self.attempt("reply with wrong model please")
        receipt, verdict = opencode.run(
            request, self.lane, attempt, home=self.home, deny_roots=self.no_denied
        )
        self.assertIsNone(receipt)
        self.assertEqual(
            verdict["refusal"],
            "opencode model_unqualified: terminal event names model 'glm-other'",
        )

    def test_missing_terminal_refused(self):
        request, attempt = self.attempt("reply with no finish please")
        receipt, verdict = opencode.run(
            request, self.lane, attempt, home=self.home, deny_roots=self.no_denied
        )
        self.assertIsNone(receipt)
        self.assertEqual(
            verdict["refusal"], "opencode missing_or_multiple_terminals: 0 step_finish events"
        )

    def test_malformed_stream_refused(self):
        request, attempt = self.attempt("reply but malformed please")
        receipt, verdict = opencode.run(
            request, self.lane, attempt, home=self.home, deny_roots=self.no_denied
        )
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "malformed native event stream")

    def test_config_tamper_refused(self):
        # The sandbox leaves nothing outside work writable, so the watched auth file is a
        # symlink into work: the fake CLI appends through it and the before/after digests
        # of home/.local/share/opencode/auth.json differ even though the run looked green.
        request, attempt = self.attempt("tamper the auth file please")
        home = Path(self.tmp.name) / "tamper-home"
        (home / ".local/share/opencode").mkdir(parents=True)
        (home / ".local/share/opencode/auth.json").symlink_to(attempt / "work/brief.txt")
        receipt, verdict = opencode.run(
            request,
            self.lane,
            attempt,
            opencode=str(self.cli),
            home=home,
            deny_roots=self.no_denied,
        )
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "user configuration changed")

    def test_wall_deadline_holds(self):
        lane = {"executable": str(self.cli), "wall_seconds": 2}
        request, attempt = self.attempt("sleep a long while please")
        receipt, verdict = opencode.run(
            request, lane, attempt, home=self.home, deny_roots=self.no_denied
        )
        self.assertIsNone(receipt)
        self.assertEqual(verdict["supervisor"]["reason"], "wall_deadline")
        self.assertEqual(verdict["refusal"], "opencode interrupted: wall_deadline")

    def test_denied_auth_path_refuses_to_start(self):
        # The operator's deny-read list names the opencode auth file: the lane refuses
        # with a policy verdict before any digest is taken or any process is spawned.
        denied = str(self.home / ".local/share/opencode/auth.json")
        request, attempt = self.attempt("reply please")
        receipt, verdict = opencode.run(
            request, self.lane, attempt, home=self.home, deny_roots=[denied]
        )
        self.assertIsNone(receipt)
        self.assertTrue(verdict["refusal"].startswith("credential_denied_by_policy"))
        self.assertIn(denied, verdict["refusal"])
        self.assertIsNone(verdict["supervisor"])
        self.assertFalse((attempt / "work").exists())
        self.assertFalse((attempt / "native.jsonl").exists())

    def test_an_unrelated_deny_root_does_not_block(self):
        request, attempt = self.attempt("reply please")
        receipt, verdict = opencode.run(
            request,
            self.lane,
            attempt,
            home=self.home,
            deny_roots=[str(self.home / ".ssh")],
        )
        self.assertIsNone(verdict["refusal"])
        self.assertIsNotNone(receipt)

    def test_adapter_session_id_against_captured_lines(self):
        stream = Path(self.tmp.name) / "captured.jsonl"
        stream.write_text(
            '{"type":"step_start","sessionID":"ses_f6"}\n'
            '{"type":"text","text":"halfway"}\n'
            '{"type":"step_start","sessionID":"ses_f6"}\n'
        )
        self.assertEqual(OpencodeAdapter("m", Path(".")).session_id(stream), "ses_f6")


if __name__ == "__main__":
    unittest.main()
