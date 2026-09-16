"""A lane canary: the artifact must be the literal OK the brief demanded."""

import unittest
from pathlib import Path


class CanaryReplyTests(unittest.TestCase):
    def test_reply_is_exactly_ok(self):
        self.assertEqual(Path("reply.txt").read_text().strip(), "OK")


if __name__ == "__main__":
    unittest.main()
