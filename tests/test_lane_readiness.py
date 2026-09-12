import math
import unittest

from inference_grid.lane_readiness import lane_readiness

NOW = 1789221600.0


def lane(**kw):
    base = dict(
        provider="opencode",
        auth="ok",
        quota_observed_at=NOW - 100,
        quota_freshness_seconds=900,
        used_percent_max=10.5,
        admission_limit_percent=80,
        cooldown_until=None,
        qualification="qualified",
        blocked_until=None,
        blocker=None,
    )
    base.update(kw)
    return base


class LaneTests(unittest.TestCase):
    def test_states_in_priority_order(self):
        cases = [
            (lane(), "ready", "ready", NOW + 800),
            (
                lane(blocked_until=NOW + 5, blocker="weekly reset"),
                "blocked",
                "weekly reset",
                NOW + 5,
            ),
            (lane(blocked_until=NOW + 5, blocker=""), "blocked", "operator_block", NOW + 5),
            (lane(blocked_until=NOW - 5, blocker="old"), "ready", "ready", NOW + 800),
            (lane(auth="expired", cooldown_until=NOW + 50), "blocked", "auth_expired", None),
            (lane(blocked_until=NOW + 5, auth="expired"), "blocked", "operator_block", NOW + 5),
            (
                lane(cooldown_until=NOW + 50, quota_observed_at=None),
                "cooling",
                "usage_cooldown",
                NOW + 50,
            ),
            (lane(cooldown_until=NOW), "ready", "ready", NOW + 800),
            (lane(quota_observed_at=None, used_percent_max=99), "stale", "quota_unobserved", None),
            (lane(quota_observed_at=NOW - 901), "stale", "quota_stale", None),
            (lane(quota_observed_at=NOW - 900), "ready", "ready", NOW),
            (lane(used_percent_max=80), "exhausted", "admission_limit", None),
            (
                lane(used_percent_max=79.9, qualification="normalizer"),
                "unqualified",
                "normalizer",
                None,
            ),
            (lane(qualification="unqualified", auth="unknown"), "unqualified", "unqualified", None),
            (lane(auth="unknown"), "unverified", "auth_unknown", None),
            (lane(used_percent_max=None), "ready", "ready", NOW + 800),
        ]
        for item, state, reason, nxt in cases:
            snapshot = dict(item)
            out = lane_readiness(item, NOW)
            self.assertEqual(
                out,
                {"provider": "opencode", "state": state, "reason": reason, "next_check_at": nxt},
                str(item),
            )
            self.assertEqual(item, snapshot)

    def test_refusals(self):
        bad = [
            lane(provider=""),
            lane(provider="x" * 41),
            lane(auth="yes"),
            lane(quota_observed_at=math.nan),
            lane(quota_observed_at=True),
            lane(quota_freshness_seconds=0),
            lane(used_percent_max=101),
            lane(used_percent_max="5"),
            lane(admission_limit_percent=-1),
            lane(cooldown_until=math.inf),
            lane(qualification="ready"),
            lane(blocked_until="soon"),
            lane(blocker=7),
            lane(blocker="b" * 201),
            {k: v for k, v in lane().items() if k != "blocker"},
            dict(lane(), extra=1),
            "text",
            None,
        ]
        for item in bad:
            with self.assertRaises(ValueError, msg=str(item)[:80]):
                lane_readiness(item, NOW)
        for now in (math.nan, math.inf, True, None, "now"):
            with self.assertRaises(ValueError):
                lane_readiness(lane(), now)


if __name__ == "__main__":
    unittest.main()
