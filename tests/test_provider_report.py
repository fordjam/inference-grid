import math
import unittest

from inference_grid.provider_report import provider_report

NOW = 1789221600.0


class ReportTests(unittest.TestCase):
    def test_statuses(self):
        cases = [
            (None, None, "unknown", None),
            ({"status": "published", "windows": {"w": 1}, "secret": "x"}, None, "ok", None),
            ({"status": "stale"}, None, "unknown", None),
            ({"status": "refused", "reason": "r"}, None, "error", None),
            ({"status": "auth_required"}, None, "auth_required", None),
            ({"status": "published"}, NOW + 60, "cooldown", "2026-09-12T14:01:00+00:00"),
            ({"status": "refused"}, NOW + 60, "cooldown", "2026-09-12T14:01:00+00:00"),
            ({"status": "published"}, NOW, "ok", None),
            ({"status": "published"}, NOW - 1, "ok", None),
            (None, NOW + 1, "cooldown", "2026-09-12T14:00:01+00:00"),
        ]
        for publish, until, status, eligible in cases:
            out = provider_report("claude", publish, until, NOW)
            self.assertEqual(
                out,
                {"provider": "claude", "status": status, "next_eligible_at": eligible},
                str(publish),
            )
            self.assertNotIn("secret", str(out))

    def test_refusals(self):
        cases = [
            ("", None, None, NOW),
            ("x" * 41, None, None, NOW),
            (7, None, None, NOW),
            ("claude", {"status": "busy"}, None, NOW),
            ("claude", "published", None, NOW),
            ("claude", {}, None, NOW),
            ("claude", None, math.nan, NOW),
            ("claude", None, True, NOW),
            ("claude", None, "60", NOW),
            ("claude", None, None, math.inf),
            ("claude", None, None, None),
            ("claude", None, None, False),
        ]
        for args in cases:
            with self.assertRaises(ValueError, msg=str(args)):
                provider_report(*args)


if __name__ == "__main__":
    unittest.main()
