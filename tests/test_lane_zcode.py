"""zcode lane module against a fake CLI and a fake session database; macOS sandbox required."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from inference_grid.lanes import zcode


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec") or not Path("/private/tmp").is_dir():
        raise unittest.SkipTest("macOS sandbox-exec and /private/tmp required")


FAKE_CLI = r"""
import json, os, sys
args = sys.argv[1:]
prompt = args[args.index("--prompt") + 1]
cwd = args[args.index("--cwd") + 1]
sys.stdout.write("AI SDK Warning: something\n")
if "write" in prompt:
    open(os.path.join(cwd, "out.py"), "w").write("VALUE = 1\n")
if "fail" in prompt:
    sys.exit(3)
json.dump({"sessionId": "sess_test", "response": "OK done", "usage": {"inputTokens": 5, "outputTokens": 2}}, sys.stdout)
"""


class ZcodeLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        root = Path(self.tmp.name)
        self.home = root / "home"
        (self.home / ".zcode/cli/db").mkdir(parents=True)
        self.db = self.home / ".zcode/cli/db/db.sqlite"
        con = sqlite3.connect(self.db)
        con.execute(
            "create table model_usage(session_id text, provider_id text, model_id text, variant text, status text, "
            "finish_reason text, input_tokens int, output_tokens int, reasoning_tokens int, cache_read_input_tokens int, "
            "error_type text, started_at int)"
        )
        con.commit()
        con.close()
        self.cli = root / "fake_zcode.py"
        self.cli.write_text(FAKE_CLI)
        self.lane = {"executable": sys.executable, "wall_seconds": 30}

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, *finish):
        con = sqlite3.connect(self.db)
        for i, f in enumerate(finish):
            con.execute(
                "insert into model_usage values('sess_test','builtin:zai-coding-plan','GLM-5.3-Flash','max','completed',?,10,3,0,0,NULL,?)",
                (f, i),
            )
        con.commit()
        con.close()

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
        self.rows("tool-calls", "stop")
        request, attempt = self.attempt("reply please")
        receipt, verdict = zcode.run(
            request, self.lane, attempt, cli=str(self.cli), db_path=self.db, home=self.home
        )
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["reply.txt"])
        self.assertEqual((attempt / "artifacts/reply.txt").read_text(), "OK done")
        self.assertEqual(verdict["session"], "sess_test")
        request, attempt = self.attempt("write out.py", expected=["out.py"])
        receipt, _ = zcode.run(
            request, self.lane, attempt, cli=str(self.cli), db_path=self.db, home=self.home
        )
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual((attempt / "artifacts/out.py").read_text(), "VALUE = 1\n")

    def test_refusals(self):
        request, attempt = self.attempt("reply please")
        receipt, verdict = zcode.run(
            request, self.lane, attempt, cli=str(self.cli), db_path=self.db, home=self.home
        )
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "no native model requests recorded")
        self.rows("tool-calls")
        request, attempt = self.attempt("reply please")
        receipt, verdict = zcode.run(
            request, self.lane, attempt, cli=str(self.cli), db_path=self.db, home=self.home
        )
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "final native request did not stop normally")
        )
        self.rows("stop")
        request, attempt = self.attempt("write then fail")
        receipt, verdict = zcode.run(
            request, self.lane, attempt, cli=str(self.cli), db_path=self.db, home=self.home
        )
        self.assertEqual((receipt, verdict["refusal"]), (None, "no qualified native terminal"))
        request, attempt = self.attempt("reply please", expected=["missing.py"])
        receipt, verdict = zcode.run(
            request, self.lane, attempt, cli=str(self.cli), db_path=self.db, home=self.home
        )
        self.assertEqual(verdict["refusal"], "expected artifacts missing: missing.py")
        request, attempt = self.attempt("reply please")
        receipt, verdict = zcode.run(
            request,
            self.lane,
            attempt,
            cli=str(self.cli),
            db_path=self.db,
            home=self.home,
            mode="plan",
        )
        self.assertIsNotNone(receipt)
        request, attempt = self.attempt("reply please")
        receipt, verdict = zcode.run(
            dict(request, model="glm-5.3"),
            self.lane,
            attempt,
            cli=str(self.cli),
            db_path=self.db,
            home=self.home,
        )
        self.assertEqual(verdict["refusal"], "request served by an unexpected model")

    def test_artifact_names_cannot_escape(self):
        # Found by an independent Z.ai review: "../native.out" and absolute names must be refused.
        self.rows("stop")
        for name in ("../native.out", "/tmp/escape.txt", "sub/../../x"):
            request, attempt = self.attempt("reply please", expected=[name])
            receipt, verdict = zcode.run(
                request, self.lane, attempt, cli=str(self.cli), db_path=self.db, home=self.home
            )
            self.assertIsNone(receipt, name)
            self.assertEqual(
                verdict["refusal"], "expected.json must list safe relative artifact names", name
            )
            self.assertFalse((attempt / "native.out.copy").exists())


if __name__ == "__main__":
    unittest.main()
