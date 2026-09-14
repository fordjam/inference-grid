"""A qualification reviewer must reject the seeded-defects module with findings."""

import json
import unittest
from pathlib import Path


class QualificationFindingsTests(unittest.TestCase):
    def test_verdict_rejects_with_findings(self):
        review = json.loads(Path("reply.txt").read_text().strip())
        self.assertEqual(review["verdict"], "rejected")
        self.assertGreaterEqual(len(review["findings"]), 1)


if __name__ == "__main__":
    unittest.main()
