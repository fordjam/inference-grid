"""Coordinator tests for the tests_multi_file qualification packet."""

import unittest

from report import summarize
from store import Store


class StoreTests(unittest.TestCase):
    def test_put_then_get(self):
        store = Store()
        store.put("a", 1)
        self.assertEqual(store.get("a"), 1)

    def test_get_default(self):
        self.assertIsNone(Store().get("missing"))
        self.assertEqual(Store().get("missing", 7), 7)


class ReportTests(unittest.TestCase):
    def test_summarize_counts(self):
        self.assertEqual(summarize(["a", "b", "c"]), "3 items")

    def test_summarize_empty(self):
        self.assertEqual(summarize([]), "0 items")


if __name__ == "__main__":
    unittest.main()
