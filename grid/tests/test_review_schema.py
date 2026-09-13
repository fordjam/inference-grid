"""Acceptance test for a review artifact: strict JSON verdict with demonstrable findings."""

import json
import unittest
from pathlib import Path


class ReviewSchemaTests(unittest.TestCase):
    def test_reply_is_a_review(self):
        text = Path("reply.txt").read_text().strip()
        if text.startswith("```"):
            body = text[3:]
            newline = body.find("\n")
            body = body[newline + 1 :] if newline != -1 else body[body.find("{") :]
            if body.rstrip().endswith("```"):
                body = body.rstrip()[:-3]
            text = body.strip()
        review = json.loads(text)
        self.assertEqual(set(review), {"verdict", "findings", "checked"})
        self.assertIn(review["verdict"], ("approved", "rejected"))
        self.assertIsInstance(review["checked"], list)
        self.assertGreaterEqual(len(review["checked"]), 3)
        for finding in review["findings"]:
            self.assertEqual(set(finding), {"location", "input", "expected", "observed"})
            self.assertTrue(all(isinstance(v, str) and v.strip() for v in finding.values()))
        self.assertEqual(review["verdict"] == "rejected", len(review["findings"]) > 0)


if __name__ == "__main__":
    unittest.main()
