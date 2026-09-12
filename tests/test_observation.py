import math
import unittest
from inference_grid.observation import normalize_observation

NOW = 1789221756.0


def obs(**kw):
    base = {
        "provider": "claude",
        "observed_at": "2026-09-12T14:00:00Z",
        "windows": [
            {"id": "weekly", "used_percent": 40},
            {"id": "five_hour", "used_percent": 0, "resets_at": "2026-09-12T16:00:00+00:00"},
        ],
        "cookie": "DO_NOT_SHARE",
    }
    base.update(kw)
    return base


class NormalizeTests(unittest.TestCase):
    def test_ok_sorted_and_sanitized(self):
        out = normalize_observation(obs(), NOW)
        self.assertEqual(set(out), {"provider", "observed_at", "observed_ts", "windows"})
        self.assertEqual([w["id"] for w in out["windows"]], ["five_hour", "weekly"])
        self.assertEqual(
            out["windows"][1], {"id": "weekly", "used_percent": 40.0, "resets_at": None}
        )
        self.assertEqual(out["observed_ts"], 1789221600.0)
        self.assertNotIn("cookie", str(out))

    def test_none_usage_kept_not_zero(self):
        out = normalize_observation(obs(windows=[{"id": "monthly", "used_percent": None}]), NOW)
        self.assertIsNone(out["windows"][0]["used_percent"])

    def test_refusals(self):
        bad = [
            "text",
            obs(provider="openai"),
            obs(observed_at="2026-09-12T14:00:00"),
            obs(observed_at="2026-09-12T14:05:00Z"),
            obs(observed_at=5),
            obs(windows=[]),
            obs(windows=[{"id": "weekly", "used_percent": 1}] * 13),
            obs(windows=[{"id": "daily", "used_percent": 1}]),
            obs(windows=[{"id": "weekly", "used_percent": 1}, {"id": "weekly", "used_percent": 2}]),
            obs(windows=[{"id": "weekly"}]),
            obs(windows=[{"id": "weekly", "used_percent": True}]),
            obs(windows=[{"id": "weekly", "used_percent": 101}]),
            obs(windows=[{"id": "weekly", "used_percent": -0.1}]),
            obs(windows=[{"id": "weekly", "used_percent": math.nan}]),
            obs(windows=[{"id": "weekly", "used_percent": "5"}]),
            obs(windows=[{"id": "weekly", "used_percent": 1, "resets_at": "2026-09-12T13:00:00Z"}]),
            obs(windows=[{"id": "weekly", "used_percent": 1, "resets_at": "2026-09-13T13:00:00"}]),
            obs(windows=[{"id": "weekly", "used_percent": 1, "resets_at": 7}]),
            obs(windows=["weekly"]),
        ]
        for case in bad:
            with self.assertRaises(ValueError, msg=str(case)[:80]):
                normalize_observation(case, NOW)

    def test_future_tolerance_boundary(self):
        normalize_observation(obs(observed_at="2026-09-12T14:03:36Z"), NOW)
        with self.assertRaises(ValueError):
            normalize_observation(obs(observed_at="2026-09-12T14:03:37Z"), NOW)


if __name__ == "__main__":
    unittest.main()
