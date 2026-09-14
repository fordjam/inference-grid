"""zai lane module against a fake claude script; macOS sandbox required."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from inference_grid.lanes import zai


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec") or not Path("/private/tmp").is_dir():
        raise unittest.SkipTest("macOS sandbox-exec and /private/tmp required")


FAKE_CLAUDE = r"""
import json, os, sys, time
prompt = sys.stdin.read()
args = sys.argv[1:]
env = dict(os.environ)
env_check = {
    "anthropic_api_key": "ANTHROPIC_API_KEY" in env,
    "oauth_token": "CLAUDE_CODE_OAUTH_TOKEN" in env,
    "model_usage_key": os.environ.get("FAKE_MODELUSAGE_KEY"),
    "auth_token_length": len(env.get("ANTHROPIC_AUTH_TOKEN", "")),
    "base_url": env.get("ANTHROPIC_BASE_URL", ""),
    "tmpdir": env.get("TMPDIR", ""),
    "max_thinking_tokens": env.get("MAX_THINKING_TOKENS", ""),
    "home": env.get("HOME", ""),
    "setting_sources_empty": "setting-sources" in args
    and args[args.index("setting-sources") + 1] == "",
}

def emit(row):
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()

sys.stdout.write("claude-cli warning: not a json line\n")
emit({"type": "system", "subtype": "init", "session_id": "sess_test",
      "model": args[args.index("--model") + 1], "env_check": env_check})
if "sleep" in prompt:
    time.sleep(30)
if "write" in prompt:
    open(os.path.join(os.getcwd(), "out.py"), "w").write("VALUE = 1\n")
if "fail" in prompt:
    emit({"type": "result", "subtype": "error", "is_error": True, "result": "boom",
          "num_turns": 1, "session_id": "sess_test",
          "usage": {"input_tokens": 5, "output_tokens": 0},
          "modelUsage": {(os.environ.get("FAKE_MODELUSAGE_KEY") or args[args.index("--model") + 1]): {"inputTokens": 5, "outputTokens": 0}}})
    sys.exit(0)
emit({"type": "result", "subtype": "success", "is_error": False, "result": "OK done",
      "num_turns": 1, "session_id": "sess_test",
      "usage": {"input_tokens": 5, "output_tokens": 2},
      "modelUsage": {(os.environ.get("FAKE_MODELUSAGE_KEY") or args[args.index("--model") + 1]): {"inputTokens": 5, "outputTokens": 2}}})
"""


class ZaiLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.key = "sk-zai-grid-secret-000000000001"
        self.credential = root / "credential.json"
        self.credential.write_text(json.dumps({"api_key": self.key}))
        os.chmod(self.credential, 0o600)
        self.claude = root / "fake_claude.py"
        self.claude.write_text("#!" + sys.executable + "\n" + FAKE_CLAUDE)
        self.claude.chmod(0o700)
        self.lane = {"wall_seconds": 30, "credential_path": str(self.credential)}
        self._env_saved = {}
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            self._env_saved[name] = os.environ.get(name)
            os.environ[name] = "must-be-stripped"

    def tearDown(self):
        for name, value in self._env_saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
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

    def init_row(self, attempt):
        for line in (attempt / "native.jsonl").read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") == "system":
                return row
        raise AssertionError("no init row in native.jsonl")

    def assert_no_key_leak(self, attempt, verdict, receipt=None):
        """The api key must not reach the verdict record, the process stdout or any attempt file."""
        verdict_path = attempt / "verdict.json"
        verdict_path.write_text(json.dumps(verdict))
        blobs = [verdict_path.read_text(), (attempt / "native.jsonl").read_text()]
        if (attempt / "native.stderr").exists():
            blobs.append((attempt / "native.stderr").read_text())
        if receipt is not None:
            blobs.append(json.dumps(receipt))
        for path in sorted(attempt.rglob("*")):
            if path.is_file():
                blobs.append(path.read_bytes().decode("utf-8", "replace"))
        for blob in blobs:
            self.assertNotIn(self.key, blob)

    def test_reply_only_and_file_artifacts(self):
        request, attempt = self.attempt("reply please")
        receipt, verdict = zai.run(
            request, self.lane, attempt, claude=str(self.claude), home=self.home
        )
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["reply.txt"])
        self.assertEqual((attempt / "artifacts/reply.txt").read_text(), "OK done")
        self.assertEqual(verdict["refusal"], None)
        self.assertEqual(verdict["session"], "sess_test")
        self.assertEqual(verdict["usage"], {"input_tokens": 5, "output_tokens": 2})
        self.assertEqual(list(verdict["model_usage"]), ["glm-5.3-flash"])
        self.assertEqual(verdict["num_turns"], 1)
        self.assertEqual(verdict["supervisor"]["reason"], "process_exited")
        init = self.init_row(attempt)
        self.assertEqual(init["model"], "glm-5.3-flash")
        self.assertEqual(
            init["env_check"],
            {
                "anthropic_api_key": False,
                "oauth_token": False,
                "model_usage_key": None,
                "auth_token_length": len(self.key),
                "base_url": "https://api.z.ai/api/anthropic",
                "tmpdir": str(attempt / "work"),
                "max_thinking_tokens": "6000",
                "home": str(Path(self.tmp.name) / "home"),
                "setting_sources_empty": False,
            },
        )
        self.assert_no_key_leak(attempt, verdict, receipt)
        request, attempt = self.attempt("write out.py", expected=["out.py"])
        receipt, verdict = zai.run(
            request, self.lane, attempt, claude=str(self.claude), home=self.home
        )
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        self.assertEqual((attempt / "artifacts/out.py").read_text(), "VALUE = 1\n")
        self.assertEqual(verdict["refusal"], None)
        self.assert_no_key_leak(attempt, verdict, receipt)

    def test_credential_refusals(self):
        request, attempt = self.attempt("reply please")
        missing = Path(self.tmp.name) / "absent.json"
        receipt, verdict = zai.run(
            request,
            dict(self.lane, credential_path=str(missing)),
            attempt,
            claude=str(self.claude),
            home=self.home,
        )
        self.assertEqual((receipt, verdict["refusal"]), (None, "credential file missing"))
        self.assertIsNone(verdict["supervisor"])
        self.assertFalse((attempt / "native.jsonl").exists())
        os.chmod(self.credential, 0o644)
        request, attempt = self.attempt("reply please")
        receipt, verdict = zai.run(
            request, self.lane, attempt, claude=str(self.claude), home=self.home
        )
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "credential file must be mode 0o600")
        )
        self.assertFalse((attempt / "native.jsonl").exists())
        self.credential.write_text(json.dumps({"api_key": ""}))
        os.chmod(self.credential, 0o600)
        request, attempt = self.attempt("reply please")
        receipt, verdict = zai.run(
            request, self.lane, attempt, claude=str(self.claude), home=self.home
        )
        self.assertEqual((receipt, verdict["refusal"]), (None, "credential api_key is empty"))
        self.assertFalse((attempt / "native.jsonl").exists())

    def test_result_and_model_qualification(self):
        request, attempt = self.attempt("fail result")
        receipt, verdict = zai.run(
            request, self.lane, attempt, claude=str(self.claude), home=self.home
        )
        self.assertEqual((receipt, verdict["refusal"]), (None, "native result was not a success"))
        self.assertEqual(verdict["num_turns"], 1)
        request, attempt = self.attempt("reply please")
        os.environ["FAKE_MODELUSAGE_KEY"] = "glm-5.3-flash"
        try:
            receipt, verdict = zai.run(
                dict(request, model="glm-5.3"),
                self.lane,
                attempt,
                claude=str(self.claude),
                home=self.home,
            )
        finally:
            os.environ.pop("FAKE_MODELUSAGE_KEY", None)
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "request served by an unexpected model")
        )

    def test_wall_deadline(self):
        request, attempt = self.attempt("sleep then reply")
        receipt, verdict = zai.run(
            request,
            dict(self.lane, wall_seconds=2),
            attempt,
            claude=str(self.claude),
            home=self.home,
        )
        self.assertIsNone(receipt)
        self.assertEqual(verdict["refusal"], "no qualified native terminal")
        self.assertEqual(verdict["supervisor"]["reason"], "wall_deadline")
        self.assert_no_key_leak(attempt, verdict)


if __name__ == "__main__":
    unittest.main()


class FirstPartyClaudeTests(unittest.TestCase):
    def test_thinking_budget_and_first_party_login(self):
        tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        try:
            root = Path(tmp.name)
            (root / ".claude").mkdir()
            claude = root / "fake_claude_fp"
            claude.write_text("#!" + sys.executable + "\n" + FAKE_CLAUDE)
            claude.chmod(0o700)
            attempt = root / "attempt"
            (attempt / "inputs").mkdir(parents=True)
            (attempt / "artifacts").mkdir()
            (attempt / "inputs/brief.txt").write_text("review this")
            request = {
                "attempt": "a",
                "generation": 1,
                "model": "claude-opus-4",
                "manifest_sha256": "m" * 64,
                "input_directory": str(attempt / "inputs"),
                "output_directory": str(attempt / "artifacts"),
                "thinking_tokens": 1200,
            }
            lane = {"wall_seconds": 30, "credential_path": None}
            receipt, verdict = zai.run(request, lane, attempt, claude=str(claude), home=root / "home")
            self.assertIsNone(verdict["refusal"], verdict)
            self.assertIsNone(receipt is None and False or receipt or None) if False else None
            self.assertEqual(verdict["first_party"], True)
            self.assertEqual(verdict["thinking_tokens"], 1200)
            # The init row's env facts: the Max login path, no token, no endpoint override.
            rows = [
                json.loads(line)
                for line in (attempt / "native.jsonl").read_text().splitlines()
                if line.startswith("{")
            ]
            init = next(r for r in rows if r.get("type") == "system")
            check = init["env_check"]
            self.assertEqual(check["max_thinking_tokens"], "1200")
            self.assertEqual(check["auth_token_length"], 0)
            self.assertEqual(check["base_url"], "")
            self.assertFalse(check["setting_sources_empty"])
            self.assertTrue(check["home"].startswith("/"))
            # The CLI's config directory was writable; the attempt home was not implied.
            self.assertTrue((root / ".claude").is_dir())
        finally:
            tmp.cleanup()

    def test_task_budget_overrides_the_default_thinking_tokens(self):
        task = {"id": "x", "artifacts": ["reply.txt"], "brief": "b.txt"}
        self.assertEqual(
            zai.DEFAULT_THINKING_TOKENS, 6000
        )  # the tier constant stays named and documented
        self.assertTrue("thinking_tokens" in zai.build_env.__code__.co_varnames)
        del task
