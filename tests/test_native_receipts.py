import unittest

from inference_grid.native_receipts import receipt_errors


def ok_receipt(**over):
    base = {"actual_model": "gpt-x", "finish_reason": "stop", "text": "hello"}
    base.update(over)
    return base


class TestReceiptErrors(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(receipt_errors(ok_receipt(), "gpt-x"), [])

    def test_wrong_model(self):
        errors = receipt_errors(ok_receipt(actual_model="gpt-y"), "gpt-x")
        self.assertEqual(len(errors), 1)
        self.assertIn("actual_model", errors[0])

    def test_non_stop(self):
        errors = receipt_errors(ok_receipt(finish_reason="length"), "gpt-x")
        self.assertEqual(errors, ["finish_reason must equal 'stop'"])

    def test_empty_text(self):
        self.assertEqual(
            receipt_errors(ok_receipt(text="   \n"), "gpt-x"),
            ["text must be non-empty after stripping"],
        )

    def test_malformed_types(self):
        for bad in (None, [], "stop", 42, (1, 2)):
            self.assertEqual(receipt_errors(bad, "gpt-x"), ["receipt must be a dict"])
        errors = receipt_errors({"actual_model": 7, "finish_reason": None, "text": 3.5}, "")
        self.assertEqual(len(errors), 4)
        self.assertEqual(
            receipt_errors(ok_receipt(), None),
            ["expected_model must be a non-empty string"],
        )


if __name__ == "__main__":
    unittest.main()
