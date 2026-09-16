"""goat lane module against a fake Command Code CLI; macOS sandbox required."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from inference_grid.lanes import goat

SCRATCH = Path("/private/tmp")


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec") or not Path("/private/tmp").is_dir():
        raise unittest.SkipTest("macOS sandbox-exec and /private/tmp required")
    global SCRATCH
    # Sandbox profiles only bless workspaces under /private/tmp or ~/.grid-workspaces; prefer
    # /private/tmp and fall back to the workspace itself when it is not writable.
    try:
        probe = tempfile.TemporaryDirectory(dir=SCRATCH)
    except OSError:
        SCRATCH = Path.cwd()
    else:
        probe.cleanup()


FAKE_CMD = r"""#!/usr/bin/env python3
import json, os, signal, sys, time

args = sys.argv[1:]
prompt = args[args.index("--print") + 1]
model = args[args.index("--model") + 1]

def emit(row):
    sys.stdout.write(json.dumps(row) + "\n")

if "sleep" in prompt:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
if "tamper" in prompt:
    with open(os.path.join(os.environ["HOME"], ".commandcode", "config.json"), "a") as stream:
        stream.write("tampered\n")
event_model = "glm-other" if "wrong model" in prompt else model
if "vendor casing" in prompt:
    event_model = model.upper()  # moonshotai/Kimi-K3, Qwen/Qwen3.8-Flash: the vendor's casing
effort = "high" if "effort high" in prompt else "low"
for _ in range(2):
    emit(
        {
            "type": "event",
            "event": {
                "type": "model_request_end",
                "model": event_model,
                "effort": effort,
                "usage": {"outputTokens": 7},
            },
        }
    )
if "write" in prompt:
    with open(os.path.join(os.getcwd(), "out.py"), "w") as stream:
        stream.write("VALUE = 1\n")
if "no result" not in prompt:
    emit(
        {
            "type": "result",
            "subtype": "success",
            "stopReason": "end_turn",
            "finalText": "GOAT done",
            "usage": {"inputTokens": 11, "outputTokens": 4},
        }
    )
"""


class GoatLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        root = Path(self.tmp.name)
        self.home = root / "home"
        (self.home / ".commandcode").mkdir(parents=True)
        (self.home / ".commandcode/config.json").write_text('{"login": "test"}\n')
        self.cmd = root / "fake_goat.py"
        self.cmd.write_text(FAKE_CMD)
        self.cmd.chmod(0o755)
        self.lane = {"executable": str(self.cmd), "wall_seconds": 30}

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

    def test_reply_only_and_file_artifacts(self):
        request, attempt = self.attempt("reply please")
        receipt, verdict = goat.run(request, self.lane, attempt, home=self.home)
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["reply.txt"])
        self.assertEqual((attempt / "artifacts/reply.txt").read_text(), "GOAT done")
        self.assertEqual(
            (attempt / "work/grid-effort.mjs").read_text(),
            "export default function (cmd) { cmd.on('session_start', () => "
            "{ cmd.setEffort('low'); }); }",
        )
        self.assertEqual(verdict["supervisor"]["reason"], "process_exited")
        self.assertEqual(verdict["effort"], "low")
        self.assertEqual(verdict["usage"], {"inputTokens": 11, "outputTokens": 4})
        self.assertEqual(verdict["progress"], {"model_requests": 2, "output_tokens_reported": 14})
        request, attempt = self.attempt("write out.py", expected=["out.py"])
        receipt, _ = goat.run(request, self.lane, attempt, cmd=str(self.cmd), home=self.home)
        self.assertEqual(receipt["actual_model"], "glm-5.3-flash")
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual((attempt / "artifacts/out.py").read_text(), "VALUE = 1\n")
        self.assertEqual(
            receipt["artifacts"][0]["sha256"],
            hashlib.sha256(b"VALUE = 1\n").hexdigest(),
        )

    def test_expected_artifact_names_cannot_escape(self):
        for name in ("../native.jsonl", "/tmp/escape.txt", "sub/../../x"):
            request, attempt = self.attempt("reply please", expected=[name])
            receipt, verdict = goat.run(request, self.lane, attempt, home=self.home)
            self.assertIsNone(receipt, name)
            self.assertEqual(
                verdict["refusal"],
                "expected.json must list safe relative artifact names",
                name,
            )

    def test_effort_high_refused(self):
        request, attempt = self.attempt("work with effort high please")
        receipt, verdict = goat.run(request, self.lane, attempt, home=self.home)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "effort evidence missing")

    def test_vendor_casing_of_the_asked_model_is_the_same_model(self):
        # Command Code echoed `moonshotai/Kimi-K3` for the lane's `moonshotai/kimi-k3` and
        # the canary refused model_unqualified on a served reply (2026-09-16).
        request, attempt = self.attempt("reply with vendor casing please")
        receipt, verdict = goat.run(request, self.lane, attempt, home=self.home)
        self.assertIsNotNone(receipt)
        self.assertIsNone(verdict["refusal"])
        self.assertEqual(verdict["served_model_ids"], ["GLM-5.3-FLASH"])

    def test_model_mismatch_refused(self):
        request, attempt = self.attempt("reply with wrong model please")
        receipt, verdict = goat.run(request, self.lane, attempt, home=self.home)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "goat unqualified: model_unqualified")

    def test_result_missing_refused(self):
        request, attempt = self.attempt("reply with no result row please")
        receipt, verdict = goat.run(request, self.lane, attempt, home=self.home)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "goat unqualified: missing_or_multiple_terminals")

    def test_config_tamper_refused(self):
        # The sandbox leaves nothing outside work writable, so the watched config file is a
        # symlink into work: the fake cmd appends through it, and the before/after digests
        # of home/.commandcode/config.json differ even though the run looked native-complete.
        request, attempt = self.attempt("tamper the config please")
        home = Path(self.tmp.name) / "tamper-home"
        (home / ".commandcode").mkdir(parents=True)
        (home / ".commandcode/config.json").symlink_to(attempt / "work/brief.txt")
        receipt, verdict = goat.run(request, self.lane, attempt, cmd=str(self.cmd), home=home)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "user configuration changed")

    def test_timeout_refused(self):
        lane = {"executable": str(self.cmd), "wall_seconds": 2}
        request, attempt = self.attempt("sleep a long while please")
        receipt, verdict = goat.run(request, lane, attempt, home=self.home)
        self.assertIsNone(receipt)
        self.assertEqual(verdict["supervisor"]["reason"], "wall_deadline")
        self.assertEqual(verdict["refusal"], "goat interrupted: wall_deadline")


if __name__ == "__main__":
    unittest.main()
