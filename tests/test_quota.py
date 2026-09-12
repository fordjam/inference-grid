"""Independent boundary tests; no provider traffic or credentials."""

import copy
import unittest
from datetime import UTC, datetime, timedelta

from inference_grid.quota import admit, parse_timestamp


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.snapshot = {
            "status": "ok",
            "observed_at": self.now.isoformat(),
            "windows": [
                {"id": "short", "used_percent": 10, "resets_at": None},
                {"id": "long", "used_percent": 20, "resets_at": None},
            ],
        }
        self.kw = dict(
            now=self.now,
            required_windows={"short", "long"},
            threshold=80,
            ttl_seconds=60,
        )

    def check(self, snapshot=None, **kw):
        return admit(self.snapshot if snapshot is None else snapshot, **(self.kw | kw))

    def test_fresh_unknown_reset(self):
        self.assertTrue(self.check())
        self.assertIsNone(self.snapshot["windows"][0]["resets_at"])

    def test_nanoseconds_floor(self):
        d = parse_timestamp("2026-01-01T00:00:00.123456999Z")
        self.assertEqual(d.microsecond, 123456)
        self.assertEqual(d.utcoffset(), timedelta(0))

    def test_offsets(self):
        self.assertEqual(parse_timestamp("2026-01-01T01:00:00+01:00"), self.now)

    def test_invalid_timestamps(self):
        for s in [
            None,
            1,
            "",
            "2026-01-01",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00.1234567890Z",
            "2026-99-01T00:00:00Z",
        ]:
            with self.subTest(s=s), self.assertRaises((ValueError, TypeError)):
                parse_timestamp(s)

    def test_age_boundaries(self):
        self.assertTrue(self.check(now=self.now + timedelta(seconds=60)))
        self.assertFalse(self.check(now=self.now + timedelta(seconds=60, microseconds=1)))
        self.assertFalse(self.check(now=self.now - timedelta(microseconds=1)))
        self.assertFalse(self.check(now=self.now.replace(tzinfo=None)))

    def test_usage_values(self):
        for v in [True, False, None, "10", float("nan"), float("inf"), -1, 101, 80]:
            self.snapshot["windows"][0]["used_percent"] = v
            with self.subTest(v=v):
                self.assertFalse(self.check())

    def test_reset_boundary(self):
        for s, allowed in [
            ("2026-01-01T00:00:00Z", False),
            ("2026-01-01T00:00:00.000001Z", True),
            ("bad", False),
        ]:
            self.snapshot["windows"][0]["resets_at"] = s
            self.assertEqual(self.check(), allowed)

    def test_windows_exact(self):
        for windows in [
            [],
            self.snapshot["windows"][:1],
            self.snapshot["windows"] + [self.snapshot["windows"][0]],
            self.snapshot["windows"] + [{"id": "extra", "used_percent": 0, "resets_at": None}],
        ]:
            s = copy.deepcopy(self.snapshot)
            s["windows"] = windows
            self.assertFalse(self.check(s))

    def test_missing_reset_key(self):
        del self.snapshot["windows"][0]["resets_at"]
        self.assertFalse(self.check())

    def test_bad_policy(self):
        for v in [True, 0, -1, float("nan"), float("inf"), "60", None]:
            self.assertFalse(self.check(ttl_seconds=v))
        for v in [True, 0, -1, 101, float("nan"), float("inf"), "80", None]:
            self.assertFalse(self.check(threshold=v))
        for v in [set(), {"", "short"}, ["short", "long"], {"short", 1}]:
            self.assertFalse(self.check(required_windows=v))

    def test_bad_snapshot(self):
        for s in [
            [],
            "ok",
            {},
            {"status": "unknown"},
            {"status": "ok", "observed_at": "bad", "windows": []},
        ]:
            self.assertFalse(self.check(s))

    def test_no_mutation(self):
        saved = copy.deepcopy(self.snapshot)
        self.check()
        self.assertEqual(saved, self.snapshot)


if __name__ == "__main__":
    unittest.main()
