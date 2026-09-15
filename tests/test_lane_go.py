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
        # 402 carries its own plan refusal now; the bare-status form is any other error.
        error = urllib.error.HTTPError(ENDPOINT, 500, "Internal Server Error", {}, None)
        receipt, verdict = go.run(request, self.lane, attempt, send=self.send(error))
        self.assertEqual((receipt, verdict["refusal"]), (None, "endpoint returned HTTP 500"))
        self.assertNotIn("Internal Server Error", json.dumps(verdict))

    def test_402_names_a_model_outside_the_subscription_plan(self):
        import io

        request, attempt = self.attempt(expected=["out.py"])
        error = urllib.error.HTTPError(
            ENDPOINT,
            402,
            "Payment Required",
            {},
            io.BytesIO(b'{"error": {"message": "insufficient_credits"}}'),
        )
        receipt, verdict = go.run(request, self.lane, attempt, send=self.send(error))
        self.assertIsNone(receipt)
        # ClinePass bills uncovered models (kimi-k3, deepseek-v4-flash) to pay-as-you-go
        # credits; the refusal names the plan gap so the driver never retries it.
        self.assertEqual(verdict["refusal"], "model_not_in_plan")
        self.assertNotIn("insufficient_credits", json.dumps(verdict))
        self.assertFalse((attempt / "native.json").exists())

    def test_clinepass_wrapped_response_is_unwrapped_and_provider_recorded(self):
        request, attempt = self.attempt(expected=["out.py"])
        wrapped = {"provider": "clinepass", "data": self.native()}
        receipt, verdict = go.run(
            request, dict(self.lane, provider="clinepass"), attempt, send=self.send(wrapped)
        )
        self.assertIsNone(verdict["refusal"], verdict)
        self.assertEqual([a["path"] for a in receipt["artifacts"]], ["out.py"])
        # The response's provider field reaches the verdict; usage and finish reason are
        # read from the unwrapped chat-completions document.
        self.assertEqual(verdict["provider"], "clinepass")
        self.assertEqual(verdict["usage"], {"input_tokens": 11, "output_tokens": 7})
        self.assertEqual(verdict["finish_reason"], "stop")
        # native.json keeps the endpoint's own document, wrapper included.
        native = json.loads((attempt / "native.json").read_text())
        self.assertEqual(native["provider"], "clinepass")
        self.assertEqual(native["data"]["model"], MODEL)

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

    def test_task_budget_bounds_the_transport_timeout(self):
        # The attempt request carries the task's budget.wall_seconds; the transport waits
        # for the tighter of task and lane bounds, never longer than the lane allows.
        request, attempt = self.attempt(expected=["out.py"])
        sender = self.send(self.native())
        receipt, verdict = go.run(
            dict(request, wall_seconds=600),
            {"credential_path": str(self.credential), "wall_seconds": 400},
            attempt,
            send=sender,
        )
        self.assertIsNone(verdict["refusal"], verdict)
        self.assertEqual(sender.recorded["timeout"], 400)
        self.assertEqual(verdict["transport_timeout"], 400)
        self.assertEqual((verdict["task_wall_seconds"], verdict["lane_wall_seconds"]), (600, 400))

        request, attempt = self.attempt(expected=["out.py"])
        sender = self.send(self.native())
        receipt, verdict = go.run(
            dict(request, wall_seconds=300),
            {"credential_path": str(self.credential), "wall_seconds": 900},
            attempt,
            send=sender,
        )
        self.assertIsNone(verdict["refusal"], verdict)
        self.assertEqual(sender.recorded["timeout"], 300)
        self.assertEqual(verdict["transport_timeout"], 300)

        # Without a task budget the lane bound alone applies (back-compat).
        request, attempt = self.attempt(expected=["out.py"])
        sender = self.send(self.native())
        receipt, verdict = go.run(
            request,
            {"credential_path": str(self.credential), "wall_seconds": 400},
            attempt,
            send=sender,
        )
        self.assertIsNone(verdict["refusal"], verdict)
        self.assertEqual(sender.recorded["timeout"], 400)
        self.assertIsNone(verdict["task_wall_seconds"])

    def test_thinking_budget_rides_on_max_tokens(self):
        # The endpoint's documented request schema honours no token-count reasoning field
        # for the model, so the task's thinking budget only sizes max_tokens (three times the
        # budget plus a content allowance: the cap guards spend, it does not bound thinking)
        # and the verdict records that the budget itself could not be forwarded: no effort
        # field in the body, reasoning_effort unsupported.
        request, attempt = self.attempt(expected=["out.py"])
        sender = self.send(self.native())
        receipt, verdict = go.run(
            dict(request, thinking_tokens=6000), self.lane, attempt, send=sender
        )
        self.assertIsNone(verdict["refusal"], verdict)
        self.assertEqual(sender.recorded["body"]["max_tokens"], 22000)
        self.assertNotIn("reasoning_effort", sender.recorded["body"])
        self.assertNotIn("thinking_tokens", sender.recorded["body"])
        self.assertEqual(verdict["reasoning_effort"], "unsupported")
        self.assertEqual(verdict["reasoning_budget"], "unsupported")
        self.assertEqual((verdict["thinking_tokens"], verdict["max_tokens"]), (6000, 22000))
        self.assertTrue((attempt / "artifacts/out.py").is_file())

    def test_deepseek_v4_flash_gets_the_tier_policy(self):
        # deepseek-v4-flash is in the capability map like kimi-k3: the policy tier rides
        # on reasoning_effort, whatever the model.
        for thinking, effort in ((None, "low"), (6000, "high"), (13000, "max")):
            request, attempt = self.attempt(expected=["out.py"])
            sender = self.send(self.native(model="deepseek-v4-flash"))
            payload = dict(request, model="deepseek-v4-flash")
            if thinking is not None:
                payload["thinking_tokens"] = thinking
            receipt, verdict = go.run(payload, self.lane, attempt, send=sender)
            self.assertIsNone(verdict["refusal"], verdict)
            self.assertEqual(sender.recorded["body"]["reasoning_effort"], effort, thinking)
            self.assertEqual(verdict["reasoning_effort"], effort, thinking)
            self.assertEqual(receipt["actual_model"], "deepseek-v4-flash")

    def test_deepseek_reasoning_overrun_is_classified(self):
        request, attempt = self.attempt(expected=["out.py"])
        response = self.native(model="deepseek-v4-flash", finish_reason="length", content="")
        response["usage"] = {"completion_tokens": 9000, "reasoning_tokens": 9000}
        receipt, verdict = go.run(
            dict(request, model="deepseek-v4-flash", thinking_tokens=6000),
            self.lane,
            attempt,
            send=self.send(response),
        )
        self.assertIsNone(receipt)
        self.assertEqual(
            verdict["refusal"],
            "reasoning_overrun: finish_reason length with no content"
            " (reasoning_tokens 9000 of max_tokens 22000)",
        )

    def test_deepseek_served_by_an_unexpected_model_is_refused(self):
        request, attempt = self.attempt(expected=["out.py"])
        receipt, verdict = go.run(
            dict(request, model="deepseek-v4-flash"),
            self.lane,
            attempt,
            send=self.send(self.native(model="glm-5.3-flash")),
        )
        self.assertEqual(
            (receipt, verdict["refusal"]), (None, "request served by an unexpected model")
        )

    def test_a_region_optin_403_is_its_own_refusal(self):
        import io

        request, attempt = self.attempt(expected=["out.py"])
        error = urllib.error.HTTPError(
            ENDPOINT,
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"error": {"message": "China hosting opt-in disabled"}}'),
        )
        receipt, verdict = go.run(request, self.lane, attempt, send=self.send(error))
        self.assertIsNone(receipt)
        self.assertEqual(
            verdict["refusal"],
            "region_optin_required: enable China hosting in the Go console",
        )
        # The body itself is never recorded; only the fixed classification is.
        self.assertNotIn("disabled", json.dumps(verdict))
        self.assertFalse((attempt / "native.json").exists())
        # A 403 about something else keeps the bare status refusal.
        request, attempt = self.attempt(expected=["out.py"])
        error = urllib.error.HTTPError(
            ENDPOINT, 403, "Forbidden", {}, io.BytesIO(b'{"error": "quota exhausted"}')
        )
        _, verdict = go.run(request, self.lane, attempt, send=self.send(error))
        self.assertEqual(verdict["refusal"], "endpoint returned HTTP 403")

    def test_reasoning_effort_follows_the_tier_policy(self):
        # kimi-k3 is in the capability map, so the tier the policy picks for the task's
        # thinking budget is sent as reasoning_effort: absent or <=4000 low, <=12000
        # high, beyond max. Board review tasks (6000) ask for high, not the endpoint's
        # default max that thought 16000 tokens away.
        for thinking, effort in (
            (None, "low"),
            (0, "low"),
            (4000, "low"),
            (4001, "high"),
            (6000, "high"),
            (12000, "high"),
            (12001, "max"),
        ):
            request, attempt = self.attempt(expected=["out.py"])
            sender = self.send(self.native(model="kimi-k3"))
            payload = dict(request, model="kimi-k3")
            if thinking is not None:
                payload["thinking_tokens"] = thinking
            receipt, verdict = go.run(payload, self.lane, attempt, send=sender)
            self.assertIsNone(verdict["refusal"], verdict)
            self.assertEqual(sender.recorded["body"]["reasoning_effort"], effort, thinking)
            self.assertEqual(verdict["reasoning_effort"], effort, thinking)
            self.assertNotIn("reasoning_budget", verdict)
            if thinking is not None and thinking > 0:
                self.assertEqual(sender.recorded["body"]["max_tokens"], 3 * thinking + 4000)
            else:
                self.assertEqual(sender.recorded["body"]["max_tokens"], 16000)
            self.assertTrue((attempt / "artifacts/out.py").is_file())

    def test_reasoning_overrun_is_its_own_refusal(self):
        # kimi-k3 answering with finish_reason length, all reasoning tokens and no content
        # must be named as an overrun with both counts, not a generic did-not-stop.
        request, attempt = self.attempt(expected=["out.py"])
        response = self.native(finish_reason="length", content="")
        response["usage"] = {"completion_tokens": 16000, "reasoning_tokens": 16000}
        receipt, verdict = go.run(
            dict(request, thinking_tokens=6000), self.lane, attempt, send=self.send(response)
        )
        self.assertIsNone(receipt)
        self.assertEqual(
            verdict["refusal"],
            "reasoning_overrun: finish_reason length with no content"
            " (reasoning_tokens 16000 of max_tokens 22000)",
        )
        self.assertEqual(verdict["finish_reason"], "length")
        self.assertTrue((attempt / "native.json").is_file())
        self.assertFalse((attempt / "artifacts/out.py").exists())
        # The nested completion_tokens_details form is read too, falling back to
        # completion_tokens when no reasoning count is reported at all.
        request, attempt = self.attempt(expected=["out.py"])
        response = self.native(finish_reason="length", content=None)
        response["usage"] = {
            "completion_tokens": 9000,
            "completion_tokens_details": {"reasoning_tokens": 9000},
        }
        _, verdict = go.run(request, self.lane, attempt, send=self.send(response))
        self.assertEqual(
            verdict["refusal"],
            "reasoning_overrun: finish_reason length with no content"
            " (reasoning_tokens 9000 of max_tokens 16000)",
        )
        request, attempt = self.attempt(expected=["out.py"])
        response = self.native(finish_reason="length", content=None)
        response["usage"] = {"completion_tokens": 8000}
        _, verdict = go.run(request, self.lane, attempt, send=self.send(response))
        self.assertIn("reasoning_tokens 8000 of max_tokens 16000", verdict["refusal"])
        # A length stop that did produce text stays a generic truncation refusal.
        request, attempt = self.attempt(expected=["out.py"])
        _, verdict = go.run(
            request,
            self.lane,
            attempt,
            send=self.send(self.native(finish_reason="length")),
        )
        self.assertEqual(verdict["refusal"], "final native request did not stop normally")


if __name__ == "__main__":
    unittest.main()


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


class ToolMarkupTests(unittest.TestCase):
    def test_a_tool_call_transcript_is_its_own_refusal(self):
        import tempfile

        # review-ff-glm-r6-c8e5c2c came back as a tool-calling transcript; the schema
        # test failed it correctly but only as a bare failed_tests. The lane names it.
        transcript = '<|open|>tools<|sep|><|open|>call tool="bash" ls -la'
        for expected in (["out.py"], ["reply.txt"]):
            tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
            try:
                root = Path(tmp.name)
                cred = root / "auth.json"
                cred.write_text(json.dumps({"opencode-go": {"type": "api", "key": KEY}}))
                cred.chmod(0o600)
                attempt = root / "attempt"
                (attempt / "inputs").mkdir(parents=True)
                (attempt / "artifacts").mkdir()
                (attempt / "inputs/brief.txt").write_text("review this")
                (attempt / "inputs/expected.json").write_text(json.dumps(expected))
                request = {
                    "attempt": "a",
                    "generation": 1,
                    "model": MODEL,
                    "manifest_sha256": "m" * 64,
                    "input_directory": str(attempt / "inputs"),
                    "output_directory": str(attempt / "artifacts"),
                }
                receipt, verdict = go.run(
                    request,
                    {"credential_path": str(cred), "wall_seconds": 60},
                    attempt,
                    send=self_response_sender(transcript),
                )
                self.assertIsNone(receipt)
                self.assertTrue(verdict["refusal"].startswith("tool_markup:"), verdict)
                self.assertIn('call tool="bash"', verdict["refusal"])
                self.assertLessEqual(len(verdict["refusal"]), 200)
                self.assertTrue((attempt / "native.json").is_file())
            finally:
                tmp.cleanup()


def self_response_sender(content):
    """An injected send answering with a stop reply whose text is `content`."""

    def send(body, key, session, timeout):
        return {
            "model": body["model"],
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"completion_tokens": 10},
        }

    return send


class ProviderEndpointTests(unittest.TestCase):
    def test_endpoint_follows_the_lane_provider(self):
        self.assertEqual(
            go.endpoint_for("opencode"), "https://opencode.ai/zen/go/v1/chat/completions"
        )
        self.assertEqual(
            go.endpoint_for("clinepass"), "https://api.cline.bot/api/v1/chat/completions"
        )
        # A lane without a provider key keeps the Go endpoint.
        self.assertEqual(go.ENDPOINT, go.endpoint_for("opencode"))

    def test_an_unknown_provider_refuses_before_any_request_or_credential(self):
        tmp = tempfile.TemporaryDirectory(dir=str(scratch_base()))
        try:
            root = Path(tmp.name)
            attempt = root / "attempt"
            (attempt / "inputs").mkdir(parents=True)
            (attempt / "inputs/brief.txt").write_text("write the module")
            request = {
                "attempt": "a",
                "generation": 1,
                "model": "m",
                "manifest_sha256": "m" * 64,
                "input_directory": str(attempt / "inputs"),
                "output_directory": str(attempt / "artifacts"),
            }
            # No credential file exists at all: the provider is resolved first.
            lane = {
                "credential_path": str(root / "absent.json"),
                "provider": "goat",
                "wall_seconds": 30,
            }

            def send(*args):
                raise AssertionError("no request may be sent for an unknown provider")

            receipt, verdict = go.run(request, lane, attempt, send=send)
            self.assertIsNone(receipt)
            self.assertEqual(verdict["refusal"], "unknown provider: goat")
        finally:
            tmp.cleanup()
