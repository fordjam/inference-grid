"""Coordinator tests for the pure_function qualification packet (five cases)."""

import unittest

from normalize import normalize_ratio


class NormalizeRatioTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(normalize_ratio(1, 2), 0.5)

    def test_rounding_to_two_decimals(self):
        self.assertEqual(normalize_ratio(1, 3), 0.33)

    def test_zero_denominator_is_zero(self):
        self.assertEqual(normalize_ratio(5, 0), 0.0)

    def test_negative_numerator(self):
        self.assertEqual(normalize_ratio(-2, 4), -0.5)

    def test_large_numbers(self):
        self.assertEqual(normalize_ratio(10**9, 3), 333333333.33)


if __name__ == "__main__":
    unittest.main()
