"""board_prepare's goat-account configuration, against fixture observations and a
fake ledger.

No test opens a socket: the observations are written to a temporary directory and the
ledger is a fake that records the exact ``configure_account`` / ``record_lane`` calls, with
stale-ness judged by the real ``lane_readiness`` classifier.
"""

import datetime
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from inference_grid.lane_readiness import lane_readiness
from inference_grid.ledger import classifier_view

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deployments" / "local"


def load(name):
    spec = importlib.util.spec_from_file_location("deploy_" + name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("board_prepare")

NOW = 1_000_000.0


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


GOAT_OBSERVATION = {
    "provider": "command-code",
    "observed_at": iso(NOW - 60),
    "status": "ok",
    "windows": [
        {"id": "five_hour", "used_percent": 25.0, "resets_at": None},
        {"id": "weekly", "used_percent": 40.0, "resets_at": None},
        {"id": "monthly", "used_percent": 50.0, "resets_at": None},
    ],
}

GOAT_LANES = [
    {"lane": "goat", "model": "glm-5.3-flash"},
    {"lane": "goat-mini", "model": "glm-5.3-mini"},
]


class FakeLedger:
    def __init__(self, now=NOW):
        self.now = now
        self.configured = []
        self.lanes = {}

    def configure_account(
        self, name, capacity, windows, expires, models, alias_names=(), observed_at=None
    ):
        self.configured.append(
            {
                "name": name,
                "capacity": capacity,
                "windows": dict(windows),
                "expires": expires,
                "models": list(models),
                "alias_names": list(alias_names),
                "observed_at": observed_at,
            }
        )

    def record_lane(self, provider, record):
        self.lanes[provider] = record
        return lane_readiness(classifier_view(record), self.now)


class ConfigureObservationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def observation(self, name, obs):
        path = self.tmp / name
        path.write_text(json.dumps(obs))
        return path

    def test_goat_observation_configures_the_account_and_one_lane_record_per_lane(self):
        ledger = FakeLedger()
        prepare.configure_observation(
            {},
            ledger,
            "goat-account",
            GOAT_OBSERVATION,
            prepare.GOAT_WINDOW_UNITS,
            prepare.GOAT_VALID,
            GOAT_LANES,
            NOW,
        )
        self.assertEqual(
            ledger.configured,
            [
                {
                    "name": "goat-account",
                    "capacity": 1,
                    # remaining = cap x (100 - used) / 100 against the 14 / 35 / 70 caps
                    "windows": {
                        "five_hour": 14 * 75 / 100,
                        "weekly": 35 * 60 / 100,
                        "monthly": 35.0,
                    },
                    "expires": NOW - 60 + prepare.GOAT_VALID,
                    "models": ["glm-5.3-flash", "glm-5.3-mini"],
                    "alias_names": ["goat", "goat-mini"],
                    "observed_at": NOW - 60,
                }
            ],
        )
        self.assertEqual(set(ledger.lanes), {"goat", "goat-mini"})
        for lane in ("goat", "goat-mini"):
            record = ledger.lanes[lane]
            self.assertEqual(record["provider"], lane)
            self.assertEqual(record["auth"], "ok")
            self.assertEqual(record["quota_observed_at"], NOW - 60)
            self.assertEqual(record["quota_freshness_seconds"], prepare.GOAT_VALID)
            self.assertEqual(record["used_percent_max"], 50.0)
            self.assertEqual(record["admission_limit_percent"], 80)
            self.assertEqual(record["qualification"], "qualified")
            self.assertEqual(ledger.record_lane(lane, record)["state"], "ready")

    def test_a_different_window_unit_scale_reads_correctly(self):
        # configure_observation is shared, not goat-specific: a 100-unit-per-window
        # scale (no cap, a raw percentage) reads exactly like any other unit map.
        ledger = FakeLedger()
        window_units = {"five_hour": 100, "weekly": 100, "monthly": 100}
        prepare.configure_observation(
            {},
            ledger,
            "example-account",
            GOAT_OBSERVATION,
            window_units,
            prepare.GOAT_VALID,
            GOAT_LANES,
            NOW,
        )
        self.assertEqual(
            ledger.configured,
            [
                {
                    "name": "example-account",
                    "capacity": 1,
                    "windows": {"five_hour": 75.0, "weekly": 60.0, "monthly": 50.0},
                    "expires": NOW - 60 + prepare.GOAT_VALID,
                    "models": ["glm-5.3-flash", "glm-5.3-mini"],
                    "alias_names": ["goat", "goat-mini"],
                    "observed_at": NOW - 60,
                }
            ],
        )
        self.assertEqual(ledger.lanes["goat"]["used_percent_max"], 50.0)

    def test_stale_observation_leaves_the_account_and_records_the_lane_stale(self):
        stale = dict(GOAT_OBSERVATION, observed_at=iso(NOW - prepare.GOAT_VALID - 1))
        ledger = FakeLedger()
        prepare.configure_observation(
            {},
            ledger,
            "goat-account",
            stale,
            prepare.GOAT_WINDOW_UNITS,
            prepare.GOAT_VALID,
            GOAT_LANES,
            NOW,
        )
        self.assertEqual(ledger.configured, [])
        self.assertEqual(set(ledger.lanes), {"goat", "goat-mini"})
        for lane in ("goat", "goat-mini"):
            self.assertEqual(ledger.lanes[lane]["quota_observed_at"], NOW - prepare.GOAT_VALID - 1)
            self.assertEqual(ledger.record_lane(lane, ledger.lanes[lane])["reason"], "quota_stale")

    def test_non_ok_observation_leaves_the_account_and_records_the_lane_stale(self):
        ledger = FakeLedger()
        prepare.configure_observation(
            {},
            ledger,
            "goat-account",
            dict(GOAT_OBSERVATION, status="unknown"),
            prepare.GOAT_WINDOW_UNITS,
            prepare.GOAT_VALID,
            GOAT_LANES,
            NOW,
        )
        self.assertEqual(ledger.configured, [])
        self.assertIsNone(ledger.lanes["goat"]["quota_observed_at"])
        self.assertEqual(
            ledger.record_lane("goat", ledger.lanes["goat"])["reason"], "quota_unobserved"
        )

    def test_absent_lane_config_leaves_the_accounts_alone(self):
        ledger = FakeLedger()
        prepare.configure_goat({"goat_observation_path": str(self.tmp / "nope.json")}, ledger, NOW)
        self.assertEqual(ledger.configured, [])
        self.assertEqual(ledger.lanes, {})

    def test_missing_observation_file_records_the_lanes_stale_without_configuring(self):
        ledger = FakeLedger()
        config = {
            "goat_observation_path": str(self.tmp / "absent.json"),
            "goat_lanes": GOAT_LANES,
        }
        prepare.configure_goat(config, ledger, NOW)
        self.assertEqual(ledger.configured, [])
        self.assertEqual(
            {lane: ledger.lanes[lane]["quota_observed_at"] for lane in ledger.lanes},
            {"goat": None, "goat-mini": None},
        )

    def test_configure_reads_the_observation_from_its_config_path(self):
        ledger = FakeLedger()
        config = {
            "goat_observation_path": str(
                self.observation("goat-observation.json", GOAT_OBSERVATION)
            ),
            "goat_lanes": GOAT_LANES,
        }
        prepare.configure_goat(config, ledger, NOW)
        self.assertEqual([c["name"] for c in ledger.configured], ["goat-account"])
        self.assertEqual(set(ledger.lanes), {"goat", "goat-mini"})


class ObservationRecordTests(unittest.TestCase):
    """The record builder the board runner re-reads observation files through (brief L1)."""

    def test_the_builder_counts_every_numeric_window_when_no_unit_map_is_given(self):
        record, observed, used = prepare.observation_record({}, GOAT_OBSERVATION, None, 900)
        self.assertEqual(used, {"five_hour": 25.0, "weekly": 40.0, "monthly": 50.0})
        self.assertEqual(record["used_percent_max"], 50.0)
        self.assertEqual(record["quota_observed_at"], NOW - 60)
        self.assertEqual(record["quota_freshness_seconds"], 900)
        self.assertEqual(record["admission_limit_percent"], 80)
        self.assertEqual(record["qualification"], "qualified")
        self.assertEqual(observed, NOW - 60)

    def test_the_builder_flags_a_non_ok_observation_unobserved(self):
        record, observed, used = prepare.observation_record(
            {}, dict(GOAT_OBSERVATION, status="unknown"), None, 900
        )
        self.assertIsNone(record["quota_observed_at"])
        self.assertEqual(observed, NOW - 60)
        self.assertEqual(used, {"five_hour": 25.0, "weekly": 40.0, "monthly": 50.0})

    def test_the_builder_without_windows_yields_an_unobserved_record(self):
        record, observed, used = prepare.observation_record(
            {}, {"observed_at": iso(NOW), "status": "ok"}, None, 900
        )
        self.assertIsNone(record["quota_observed_at"])
        self.assertIsNone(record["used_percent_max"])
        self.assertEqual(used, {})
        self.assertEqual(observed, NOW)

    def test_the_builder_treats_a_missing_observation_as_unobserved(self):
        record, observed, used = prepare.observation_record({}, None, None, 900)
        self.assertIsNone(record["quota_observed_at"])
        self.assertIsNone(record["used_percent_max"])
        self.assertEqual(used, {})
        self.assertIsNone(observed)


if __name__ == "__main__":
    unittest.main()


class AdmissionLimitTests(unittest.TestCase):
    def test_the_admission_limit_comes_from_config_and_defaults_to_80(self):
        self.assertEqual(prepare.admission_limit({}), 80)
        self.assertEqual(prepare.admission_limit({"admission_limit_percent": 100}), 100)
        self.assertEqual(prepare.admission_limit({"admission_limit_percent": 250}), 80)
