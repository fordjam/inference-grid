import copy
import unittest
from inference_grid.cline_outcomes import classify_cline

MODEL = "cline-pass/example-model"
OK = {"reason": "process_exited", "returncode": 0}
FINAL = {
    "type": "run_result",
    "finishReason": "completed",
    "text": "Created fixture.",
    "model": {"id": MODEL, "provider": "cline"},
}


class NativeOutcomeTests(unittest.TestCase):
    def classify(self, events=None, supervisor=None):
        return classify_cline(
            [copy.deepcopy(FINAL)] if events is None else events,
            OK if supervisor is None else supervisor,
            MODEL,
        )

    def test_success_is_only_native_completion(self):
        result = self.classify()
        self.assertEqual(result["outcome"], "native_complete")
        self.assertEqual(result["receipt"]["finish_reason"], "stop")
        self.assertNotIn("accepted", result)

    def test_captured_empty_completed_pattern(self):
        result = self.classify([dict(FINAL, text="")])
        self.assertEqual(result["reason"], "empty_terminal_text")
        self.assertIsNone(result["receipt"])

    def test_supervisor_stops_override_terminal(self):
        for reason in (
            "visible_output_budget",
            "iteration_budget",
            "log_byte_budget",
            "wall_deadline",
        ):
            result = self.classify(supervisor={"reason": reason, "returncode": 0})
            self.assertEqual(result["outcome"], "interrupted")
            self.assertIsNone(result["receipt"])

    def test_exit_zero_without_terminal(self):
        self.assertEqual(self.classify([])["reason"], "missing_or_multiple_terminals")

    def test_duplicate_or_trailing_events(self):
        for rows, reason in [
            ([FINAL, FINAL], "missing_or_multiple_terminals"),
            ([FINAL, {"type": "anything"}], "events_after_terminal"),
        ]:
            self.assertEqual(self.classify(rows)["reason"], reason)

    def test_native_reasons(self):
        for reason in ("timeout", "error", "length", None):
            self.assertEqual(
                self.classify([dict(FINAL, finishReason=reason)])["reason"], "native_not_completed"
            )

    def test_model_and_provider(self):
        for model in (
            None,
            {},
            {"id": MODEL, "provider": "other"},
            {"id": "wrong", "provider": "cline"},
        ):
            self.assertEqual(
                self.classify([dict(FINAL, model=model)])["reason"], "model_unqualified"
            )

    def test_supervision_and_malformed_inputs(self):
        for supervisor in (
            {},
            {"reason": "unknown", "returncode": 0},
            {"reason": "process_exited", "returncode": False},
            {"reason": "process_exited", "returncode": -15},
        ):
            self.assertIsNone(self.classify(supervisor=supervisor)["receipt"])
        for events in (None, {}, [None], [1]):
            self.assertIsNone(classify_cline(events, OK, MODEL)["receipt"])
        self.assertIsNone(classify_cline([FINAL], [], MODEL)["receipt"])
        self.assertIsNone(classify_cline([FINAL], OK, "")["receipt"])

    def test_progress_is_sanitized_not_completion(self):
        rows = [
            {"event": {"type": "iteration_start", "text": "private"}},
            {"event": {"type": "usage", "totalOutputTokens": 4}},
            {"event": {"type": "usage", "totalOutputTokens": True}},
            {"event": {"type": "usage", "totalOutputTokens": 2}},
        ]
        result = self.classify(rows)
        self.assertEqual(
            result["progress"], {"iterations_observed": 1, "output_tokens_reported": 4}
        )
        self.assertNotIn("private", str(result))
        self.assertIsNone(result["receipt"])

    def test_no_input_mutation(self):
        rows = [copy.deepcopy(FINAL)]
        before = copy.deepcopy(rows)
        self.classify(rows)
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main()
