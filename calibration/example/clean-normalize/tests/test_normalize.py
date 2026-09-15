import unittest

from src.text.normalize import normalize_heading


class NormalizeHeadingTests(unittest.TestCase):
    def test_trims_and_drops_colon(self):
        self.assertEqual(normalize_heading("  Site A:  "), "Site A")

    def test_keeps_interior_colon(self):
        self.assertEqual(normalize_heading("Site: A"), "Site: A")

    def test_plain_heading_is_unchanged(self):
        self.assertEqual(normalize_heading("Site A"), "Site A")


if __name__ == "__main__":
    unittest.main()
