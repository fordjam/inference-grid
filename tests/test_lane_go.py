"""go lane module against an injected send function; no network access required."""

import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from inference_grid.lanes import go


KEY = "secret-go-key-0123456789abcdef"
MODEL = "code-supernova"
CODE = "VALUE = 1\n"
GOOD_CONTENT = json.dumps({"code": CODE})
ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"


def setUpModule():
    if not Path("/private/tmp").is_dir():
        raise unittest.SkipTest("/private/tmp required")


def scratch_base():
    """A writable scratch base: /private/tmp, or this workspace when the root is sealed."""
    for candidate in (Path("/private/tmp"), Path(__file__).resolve().parent):
        if not candidate.is_dir():
            continue
        try:
            probe = tempfile.TemporaryDirectory(dir=str(candidate))
        except OSError:
            continue
        probe.cleanup()
        return candidate
    raise unittest.SkipTest("no writable scratch directory under /private/tmp")


class GoLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch_base()))
        self.root = Path(self.tmp.name)
        self.credential = self.root / "credential.json"
        self.credential.write_text(json.dumps({"opencode-go": {"type": "api", "key": KEY}}))
        self.credential.chmod(0o600)
        self.lane = {"credential_path": str(self.credential), "wall_seconds": 150}

    def tearDown(self):
        self.tmp.cleanup()

    def native(self, *, model=MODEL, finish_reason="stop", content=GOOD_CONTENT):
        return {
            "model": model,
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }

    def send(self, response):
        """An injected send that records the documented call contract."""
        recorded = {}

        def call(body, key, session, timeout):
            recorded.update(body=body, key=key, session=session, timeout=timeout)
            if isinstance(response, Exception):
                raise response
            return response

        call.recorded = recorded
        return call

    def attempt(self, expected=None, prompt="write the module"):
        n = len(list(self.root.glob("attempt*")))
        attempt = self.root / f"attempt{n}"
        (attempt / "inputs").mkdir(parents=True)
        (attempt / "artifacts").mkdir()
        (attempt / "inputs/brief.txt").write_text(prompt)
        if expected is not None:
            (attempt / "inputs/expected.json").write_text(json.dumps(expected))
        request = {
            "attempt": "a",
            "generation": 1,
            "model": MODEL,
            "manifest_sha256": "m" * 64,
            "input_directory": str(attempt / "inputs"),
            "output_directory": str(attempt / "artifacts"),
        }
        return request, attempt

    def test_success_writes_artifact_and_receipt(self):
        request, attempt = self.attempt(expected=["out.py"])
        sender = self.send(self.native())
        receipt, verdict = go.run(request, self.lane, attempt, send=sender, timeout=7)
        self.assertIsNone(verdict["refusal"])
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        data = (attempt / "artifacts/out.py").read_bytes()
        self.assertEqual(data, CODE.encode())
        self.assertEqual(receipt["artifacts"][0]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(
            (receipt["status"], receipt["finish_reason"], receipt["actual_model"]),
            ("completed", "stop", MODEL),
        )
        self.assertEqual(
            sender.recorded,
            {
                "body": {
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "write the module"}],
                    "stream": False,
                    "max_tokens": 16000,
                },
                "key": KEY,
                "session": "a",
                "timeout": 7,
            },
        )
        self.assertEqual(verdict["usage"], {"input_tokens": 11, "output_tokens": 7})
        self.assertEqual((verdict["finish_reason"], verdict["session"]), ("stop", "a"))
        self.assertTrue((attempt / "native.json").is_file())

    def test_credential_refusals(self):
        request, attempt = self.attempt(expected=["out.py"])
        lane = {"credential_path": str(self.root / "absent.json")}
        receipt, verdict = go.run(request, lane, attempt, send=self.send(self.native()))
        self.assertEqual((receipt, verdict["refusal"]), (None, "credential file missing"))
        self.assertFalse((attempt / "native.json").exists())
        request, attempt = self.attempt(expected=["out.py"])
        self.credential.chmod(0o644)
        receipt, verdict = go.run(request, self.lane, attempt, send=self.send(self.native()))
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "credential file must be mode 0o600")
        )
        request, attempt = self.attempt(expected=["out.py"])
        self.credential.chmod(0o600)
        self.credential.write_text(json.dumps({"opencode-go": {"type": "api", "key": ""}}))
        receipt, verdict = go.run(request, self.lane, attempt, send=self.send(self.native()))
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "credential opencode-go key is empty")
        )

    def test_qualification_refusals(self):
        request, attempt = self.attempt(expected=["out.py"])
        receipt, verdict = go.run(
            request, self.lane, attempt, send=self.send(self.native(model="some-other-model"))
        )
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "request served by an unexpected model")
        )
        # The native evidence is still preserved for an unqualified reply.
        self.assertTrue((attempt / "native.json").is_file())
        request, attempt = self.attempt(expected=["out.py"])
        receipt, verdict = go.run(
            request, self.lane, attempt, send=self.send(self.native(finish_reason="length"))
        )
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "final native request did not stop normally")
        )
        request, attempt = self.attempt(expected=["out.py"])
        receipt, verdict = go.run(
            request, self.lane, attempt, send=self.send(self.native(content="   "))
        )
        self.assertEqual((receipt, verdict["refusal"]), (None, "empty terminal text"))

    def test_content_schema_refusals(self):
        for content in (
            "Sure! Here is the module you asked for.",
            json.dumps({"code": CODE, "notes": "an extra key"}),
            json.dumps({"codex": CODE}),
            json.dumps({"code": 42}),
            json.dumps({"code": ""}),
            json.dumps([CODE]),
        ):
            request, attempt = self.attempt(expected=["out.py"])
            receipt, verdict = go.run(
                request, self.lane, attempt, send=self.send(self.native(content=content))
            )
            self.assertEqual(
                (receipt, verdict["refusal"]), (None, "invalid expected code schema"), content
            )
            self.assertFalse((attempt / "artifacts/out.py").exists())

    def test_code_must_be_valid_python(self):
        for code in ("def broken(:\n", "VALUE = = 1\n", "x = 1\x00\n"):
            request, attempt = self.attempt(expected=["out.py"])
            receipt, verdict = go.run(
                request,
                self.lane,
                attempt,
                send=self.send(self.native(content=json.dumps({"code": code}))),
            )
            self.assertEqual(
                (receipt, verdict["refusal"]), (None, "artifact is not valid python"), code
            )

    def test_expected_json_must_list_one_name(self):
        request, attempt = self.attempt(expected=["a.py", "b.py"])
        receipt, verdict = go.run(request, self.lane, attempt, send=self.send(self.native()))
        self.assertEqual(
            (receipt, verdict["refusal"]),
            (None, "expected.json must list exactly one artifact name"),
        )
        self.assertFalse(any((attempt / "artifacts").iterdir()))

    def test_transport_error_records_class_name(self):
        request, attempt = self.attempt(expected=["out.py"])
        receipt, verdict = go.run(
            request, self.lane, attempt, send=self.send(ConnectionResetError("boom"))
        )
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "transport_error: ConnectionResetError")
        )
        self.assertNotIn("boom", json.dumps(verdict))
        self.assertFalse((attempt / "native.json").exists())

    def test_http_error_records_status_only(self):
        request, attempt = self.attempt(expected=["out.py"])
        error = urllib.error.HTTPError(ENDPOINT, 402, "Payment Required", {}, None)
        receipt, verdict = go.run(request, self.lane, attempt, send=self.send(error))
        self.assertEqual((receipt, verdict["refusal"]), (None, "endpoint returned HTTP 402"))
        self.assertNotIn("Payment Required", json.dumps(verdict))

    def test_key_never_reaches_attempt_files(self):
        request, attempt = self.attempt(expected=["out.py"])
        receipt, verdict = go.run(request, self.lane, attempt, send=self.send(self.native()))
        (attempt / "verdict.json").write_text(json.dumps(verdict))
        (attempt / "receipt.json").write_text(json.dumps(receipt))
        self.assertNotIn(KEY, (attempt / "verdict.json").read_text())
        self.assertNotIn(KEY, (attempt / "native.json").read_text())
        self.assertNotIn(KEY, (attempt / "receipt.json").read_text())
        self.assertNotIn(KEY, (attempt / "artifacts/out.py").read_text())
        # The injected send is the only place the key is ever handed over.
        request, attempt = self.attempt(expected=["out.py"])
        _, verdict = go.run(
            request, self.lane, attempt, send=self.send(ConnectionResetError("reset"))
        )
        (attempt / "verdict.json").write_text(json.dumps(verdict))
        self.assertNotIn(KEY, (attempt / "verdict.json").read_text())

    def test_redirect_handler_refuses(self):
        with self.assertRaises(ValueError):
            go.RefusedRedirects().redirect_request(
                None, None, 302, "found", {}, "https://example.test/next"
            )


if __name__ == "__main__":
    unittest.main()



    def test_transport_timeout_follows_the_lane_budget(self):
        # Without an explicit timeout the transport waits min(wall_seconds, 600), not the old
        # fixed 150 s, and the verdict records which bound was used.
        request, attempt = self.attempt(expected=["out.py"])
        sender = self.send(self.native())
        receipt, verdict = go.run(
            request,
            {"credential_path": str(self.credential), "wall_seconds": 240},
            attempt,
            send=sender,
        )
        self.assertIsNone(verdict["refusal"])
        self.assertEqual(sender.recorded["timeout"], 240)
        self.assertEqual(verdict["transport_timeout"], 240)

        request, attempt = self.attempt(expected=["out.py"])
        sender = self.send(self.native())
        receipt, verdict = go.run(
            request,
            {"credential_path": str(self.credential), "wall_seconds": 900},
            attempt,
            send=sender,
        )
        self.assertIsNone(verdict["refusal"])
        self.assertEqual(sender.recorded["timeout"], 600)
        self.assertEqual(verdict["transport_timeout"], 600)


class ReplyModeTests(unittest.TestCase):
    def test_reply_txt_publishes_raw_content_without_code_schema(self):
        tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        try:
            root = Path(tmp.name)
            cred = root / "auth.json"
            cred.write_text(json.dumps({"opencode-go": {"type": "api", "key": "k" * 40}}))
            cred.chmod(0o600)
            attempt = root / "attempt"
            (attempt / "inputs").mkdir(parents=True)
            (attempt / "artifacts").mkdir()
            (attempt / "inputs/brief.txt").write_text("review this")
            (attempt / "inputs/expected.json").write_text(json.dumps(["reply.txt"]))
            request = {
                "attempt": "a",
                "generation": 1,
                "model": "kimi-k3",
                "manifest_sha256": "m" * 64,
                "input_directory": str(attempt / "inputs"),
                "output_directory": str(attempt / "artifacts"),
            }
            verdict_text = json.dumps({"verdict": "approved", "findings": [], "checked": ["x"]})

            def send(body, key, session, timeout):
                return {
                    "model": "kimi-k3",
                    "choices": [{"finish_reason": "stop", "message": {"content": verdict_text}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                }

            receipt, verdict = go.run(
                request, {"credential_path": str(cred), "wall_seconds": 60}, attempt, send=send
            )
            self.assertIsNotNone(receipt, verdict)
            self.assertEqual((attempt / "artifacts/reply.txt").read_text(), verdict_text)
        finally:
            tmp.cleanup()
