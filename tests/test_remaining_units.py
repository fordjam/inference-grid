import math
import unittest
from inference_grid.remaining_units import remaining_units


def obs(windows):
    return {
        "provider": "opencode",
        "observed_at": "2026-09-12T14:00:00Z",
        "observed_ts": 1789221600.0,
        "windows": windows,
    }


def W(i, u):
    return {"id": i, "used_percent": u, "resets_at": None}


class RemainingTests(unittest.TestCase):
    def test_conversion_floors_and_ignores_extra_windows(self):
        out = remaining_units(
            obs([W("five_hour", 0), W("weekly", 0.1), W("monthly", 33.3333)]),
            {"five_hour": 12, "weekly": 30},
        )
        self.assertEqual(out, {"five_hour": 12.0, "weekly": 29.97})
        self.assertIsInstance(out["five_hour"], float)
        out = remaining_units(obs([W("monthly", 33.3333)]), {"monthly": 60})
        self.assertAlmostEqual(out["monthly"], 40.00002, delta=1e-6)
        self.assertLessEqual(out["monthly"], 60 * (100 - 33.3333) / 100)
        self.assertEqual(remaining_units(obs([W("weekly", 100)]), {"weekly": 7})["weekly"], 0.0)

    def test_inputs_not_mutated(self):
        o = obs([W("weekly", 10)])
        p = {"weekly": 30}
        remaining_units(o, p)
        self.assertEqual(o, obs([W("weekly", 10)]))
        self.assertEqual(p, {"weekly": 30})

    def test_refusals(self):
        good = obs([W("weekly", 10), W("five_hour", 5)])
        cases = [
            (good, {}),
            (good, "weekly"),
            (good, {"weekly": 0}),
            (good, {"weekly": -1}),
            (good, {"weekly": True}),
            (good, {"weekly": math.inf}),
            (good, {"": 30}),
            (good, {"monthly": 60}),
            (obs([W("weekly", 10), W("weekly", 20)]), {"weekly": 30}),
            (obs([W("weekly", None)]), {"weekly": 30}),
            (obs([{"id": "weekly"}]), {"weekly": 30}),
            (obs([W("weekly", True)]), {"weekly": 30}),
            (obs([W("weekly", 101)]), {"weekly": 30}),
            (obs([W("weekly", -0.5)]), {"weekly": 30}),
            (obs([W("weekly", math.nan)]), {"weekly": 30}),
            (obs([W("weekly", "10")]), {"weekly": 30}),
            ({"windows": "x"}, {"weekly": 30}),
            ("text", {"weekly": 30}),
            (obs(["weekly"]), {"weekly": 30}),
        ]
        for o, p in cases:
            with self.assertRaises(ValueError, msg=str((o, p))[:100]):
                remaining_units(o, p)


if __name__ == "__main__":
    unittest.main()
