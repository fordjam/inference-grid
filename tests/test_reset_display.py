import math
import unittest

from inference_grid.reset_display import reset_instant

T = 1789221600.0


class ResetDisplayTests(unittest.TestCase):
    def test_conversions(self):
        cases = [
            ("1 hour 51 minutes", 6660, 60),
            ("26 days 19 hours", 26 * 86400 + 19 * 3600, 3600),
            ("5 hours 0 minutes", 18000, 60),
            ("45 minutes", 2700, 60),
            ("2 days", 172800, 86400),
            ("1 day 6 hours 3 minutes", 86400 + 21600 + 180, 60),
            ("1 day, 6 hours", 86400 + 21600, 3600),
            ("  3 Hours   2 Minutes ", 10920, 60),
            ("30 seconds", 30, 1),
            ("1 hour 0 minutes 0 seconds", 3600, 1),
        ]
        for text, seconds, precision in cases:
            self.assertEqual(
                reset_instant(T, text),
                {"resets_at": T + seconds, "precision_seconds": precision},
                text,
            )
        self.assertIsNone(reset_instant(T, None))
        self.assertIsNone(reset_instant(T, "   "))
        self.assertIsInstance(reset_instant(T, "2 days")["resets_at"], float)

    def test_refusals(self):
        for text in (
            "in 2 hours",
            "unknown",
            "2 hours at noon",
            "-1 hours",
            "1.5 hours",
            "3 weeks",
            "2 hours 3 hours",
            "hours",
            "2",
            "2 hours and 5 minutes",
        ):
            with self.assertRaises(ValueError, msg=text):
                reset_instant(T, text)
        for now in (True, None, "0", math.nan, math.inf):
            with self.assertRaises(ValueError):
                reset_instant(now, "2 hours")
        with self.assertRaises(ValueError):
            reset_instant(T, 5)


if __name__ == "__main__":
    unittest.main()
