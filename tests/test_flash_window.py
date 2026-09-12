import datetime as dt
import math
import unittest

from inference_grid.flash_window import flash_window

SGT = dt.timezone(dt.timedelta(hours=8))


def ts(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=SGT).timestamp()


class WindowTests(unittest.TestCase):
    def test_boundaries(self):
        cases = [
            (ts(2026, 9, 12, 22, 59), False, ts(2026, 9, 12, 23)),
            (ts(2026, 9, 12, 23, 0), True, ts(2026, 9, 13, 9)),
            (ts(2026, 9, 13, 3, 30), True, ts(2026, 9, 13, 9)),
            (ts(2026, 9, 13, 8, 59), True, ts(2026, 9, 13, 9)),
            (ts(2026, 9, 13, 9, 0), False, ts(2026, 9, 13, 23)),
            (ts(2026, 9, 1, 12, 0), False, ts(2026, 9, 3, 23)),
            (ts(2026, 9, 3, 22, 59), False, ts(2026, 9, 3, 23)),
            (ts(2026, 9, 20, 23, 30), True, ts(2026, 9, 21, 9)),
            (ts(2026, 9, 21, 8, 59), True, ts(2026, 9, 21, 9)),
            (ts(2026, 9, 21, 9, 0), False, None),
            (ts(2026, 10, 1, 0, 0), False, None),
        ]
        for now, active, nxt in cases:
            out = flash_window(now)
            self.assertEqual(
                out,
                {"active": active, "multiplier": 2 if active else 1, "next_change_at": nxt},
                str(dt.datetime.fromtimestamp(now, SGT)),
            )
        # UTC framing: 15:00 UTC is 23:00 SGT
        self.assertTrue(
            flash_window(dt.datetime(2026, 9, 12, 15, 0, tzinfo=dt.timezone.utc).timestamp())[
                "active"
            ]
        )
        self.assertFalse(
            flash_window(dt.datetime(2026, 9, 12, 14, 59, tzinfo=dt.timezone.utc).timestamp())[
                "active"
            ]
        )

    def test_refusals(self):
        for bad in (True, None, "now", math.nan, math.inf):
            with self.assertRaises(ValueError):
                flash_window(bad)


if __name__ == "__main__":
    unittest.main()
