import unittest
from inference_grid.goat_outcomes import classify_goat

M = "z-ai/glm-5.3-flash"


def req(model=M, effort="low", out=3):
    return {
        "type": "event",
        "event": {
            "type": "model_request_end",
            "model": model,
            "usage": {
                "inputTokens": 16579,
                "outputTokens": out,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
            },
            "stopReason": "stop",
            "effort": effort,
        },
    }


def run_end():
    return {
        "type": "event",
        "event": {
            "type": "run_end",
            "result": {"finalText": "OK", "stopReason": "end_turn", "turnCount": 1},
        },
    }


def final(text="OK", subtype="success", stop="end_turn", usage=None):
    row = {
        "type": "result",
        "subtype": subtype,
        "sessionId": "s",
        "stopReason": stop,
        "usage": {"inputTokens": 16579, "outputTokens": 3} if usage is None else usage,
        "durationMs": 2600,
        "finalText": text,
    }
    return row


OK = {"reason": "process_exited", "returncode": 0}


class GoatTests(unittest.TestCase):
    def test_complete(self):
        r = classify_goat([req(), run_end(), final()], OK, M)
        self.assertEqual((r["outcome"], r["reason"]), ("native_complete", "verified_native_shape"))
        self.assertEqual(
            r["receipt"],
            {
                "actual_model": M,
                "finish_reason": "stop",
                "text": "OK",
                "effort": "low",
                "usage": {"inputTokens": 16579, "outputTokens": 3},
            },
        )
        self.assertEqual(r["progress"], {"model_requests": 1, "output_tokens_reported": 3})

    def test_progress_counts_before_supervision(self):
        r = classify_goat(
            [req(out=3), req(out=4)], {"reason": "wall_deadline", "returncode": -15}, M
        )
        self.assertEqual(
            (r["outcome"], r["reason"], r["receipt"]), ("interrupted", "wall_deadline", None)
        )
        self.assertEqual(r["progress"], {"model_requests": 2, "output_tokens_reported": 7})

    def test_invalid_input_and_unknown_usage(self):
        self.assertEqual(classify_goat("x", OK, M)["reason"], "invalid_input")
        self.assertEqual(classify_goat([1], OK, M)["reason"], "invalid_input")
        self.assertEqual(classify_goat([req()], "nope", M)["reason"], "invalid_input")
        self.assertIsNone(classify_goat([], OK, M)["progress"]["output_tokens_reported"])
        r = classify_goat([req(), final(usage={"inputTokens": "many", "outputTokens": 3})], OK, M)
        self.assertIsNone(r["receipt"]["usage"])
        r = classify_goat(
            [
                {
                    "type": "event",
                    "event": {
                        "type": "model_request_end",
                        "model": M,
                        "usage": {"outputTokens": True},
                    },
                },
                final(),
            ],
            OK,
            M,
        )
        self.assertIsNone(r["progress"]["output_tokens_reported"])
        self.assertIsNone(r["receipt"]["effort"])

    def test_refusals(self):
        cases = [
            (
                [req(), final()],
                {"reason": "iteration_budget", "returncode": -15},
                M,
                "interrupted",
                "iteration_budget",
            ),
            (
                [req(), final()],
                {"reason": "weird", "returncode": 0},
                M,
                "unqualified",
                "supervision_unqualified",
            ),
            (
                [req(), final()],
                {"reason": "process_exited", "returncode": 1},
                M,
                "unqualified",
                "process_failed",
            ),
            (
                [req(), final()],
                {"reason": "process_exited", "returncode": False},
                M,
                "unqualified",
                "process_failed",
            ),
            ([req()], OK, M, "unqualified", "missing_or_multiple_terminals"),
            ([req(), final(), final()], OK, M, "unqualified", "missing_or_multiple_terminals"),
            ([req(), final(), run_end()], OK, M, "unqualified", "events_after_terminal"),
            ([req(), final(subtype="error")], OK, M, "unqualified", "native_not_completed"),
            ([req(), final(stop="max_turns")], OK, M, "unqualified", "native_not_completed"),
            ([final()], OK, M, "unqualified", "model_unqualified"),
            ([req(), req(model="other"), final()], OK, M, "unqualified", "model_unqualified"),
            ([req(), final()], OK, "", "unqualified", "model_unqualified"),
            ([req(), final()], OK, None, "unqualified", "model_unqualified"),
            ([req(), final(text="  ")], OK, M, "unqualified", "empty_terminal_text"),
            ([req(), final(text=7)], OK, M, "unqualified", "empty_terminal_text"),
        ]
        for events, sup, model, outcome, reason in cases:
            r = classify_goat(events, sup, model)
            self.assertEqual(
                (r["outcome"], r["reason"], r["receipt"]), (outcome, reason, None), reason
            )

    def test_mixed_effort_is_unknown_and_text_unstripped(self):
        r = classify_goat([req(effort="low"), req(effort="high"), final(text=" OK\n")], OK, M)
        self.assertIsNone(r["receipt"]["effort"])
        self.assertEqual(r["receipt"]["text"], " OK\n")


if __name__ == "__main__":
    unittest.main()
