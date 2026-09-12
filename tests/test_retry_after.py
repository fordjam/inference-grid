import unittest
from inference_grid.retry_after import retry_deadline, cooldown_key


class RetryTests(unittest.TestCase):
    def test_seconds_not_early(self):
        self.assertEqual(retry_deadline("120", 1000), 1120)
        self.assertLess(1119.999, retry_deadline("120", 1000))
        self.assertEqual(retry_deadline("0", 1000), 1000)

    def test_long_delay_not_capped(self):
        self.assertEqual(retry_deadline("1000000", 1000), 1001000)

    def test_date(self):
        self.assertEqual(retry_deadline("Thu, 01 Jan 1970 00:20:00 GMT", 1000), 1200)
        self.assertEqual(retry_deadline("Thu, 01 Jan 1970 00:01:00 GMT", 1000), 1000)

    def test_missing_invalid(self):
        for v in [None, "", "garbage", "-1", "1.5", "NaN", "Infinity", "١٢", [], True, 120]:
            with self.subTest(value=v):
                self.assertEqual(retry_deadline(v, 1000), 1060)

    def test_bad_numbers(self):
        for n in [True, None, float("inf"), float("nan"), "1"]:
            with self.subTest(n=n), self.assertRaises(ValueError):
                retry_deadline("1", n)
        for f in [True, 0, -1, None, float("inf"), float("nan"), "1"]:
            with self.subTest(f=f), self.assertRaises(ValueError):
                retry_deadline("1", 1000, f)

    def test_overflow_never_shortens(self):
        with self.assertRaises(ValueError):
            retry_deadline("9" * 400, 1000)

    def test_fractional_date(self):
        self.assertEqual(retry_deadline("Thu, 01 Jan 1970 00:20:00 GMT", 1000.75), 1200)

    def test_large_now(self):
        self.assertGreater(retry_deadline("1", 1e16), 1e16)
        from fractions import Fraction

        self.assertGreaterEqual(Fraction(retry_deadline("5", 1e16)), Fraction(1e16) + 5)
        with self.assertRaises(ValueError):
            retry_deadline("1", 10**400)

    def test_endpoint_partition(self):
        self.assertNotEqual(cooldown_key("a", "usage"), cooldown_key("a", "inference"))
        self.assertNotEqual(cooldown_key("a", "usage"), cooldown_key("b", "usage"))
        self.assertEqual(cooldown_key("a", "usage"), ("a", "usage"))

    def test_invalid_partition(self):
        for a, e in [("", "usage"), ("a", ""), (None, "usage"), ("a", None)]:
            with self.subTest(a=a, e=e), self.assertRaises(ValueError):
                cooldown_key(a, e)


if __name__ == "__main__":
    unittest.main()
