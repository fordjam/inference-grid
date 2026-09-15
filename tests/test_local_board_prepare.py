"""board_prepare's goat-account and cline configuration, against fixture observations and a
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

CLINE_OBSERVATION = {
    "provider": "clinepass",
    "observed_at": iso(NOW - 60),
    "status": "ok",
    "windows": [
        {"id": "five_hour", "used_percent": 12.5, "resets_at": None},
        {"id": "weekly", "used_percent": 44.0, "resets_at": None},
        {"id": "monthly", "used_percent": 9.0, "resets_at": None},
    ],
}

GOAT_LANES = [
    {"lane": "goat", "model": "glm-5.3-flash"},
    {"lane": "goat-mini", "model": "glm-5.3-mini"},
]
CLINE_LANES = [{"lane": "cline", "model": "cline-pass/qwen3.8-max"}]


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

    def test_cline_windows_read_against_a_100_unit_scale_per_window(self):
        ledger = FakeLedger()
        prepare.configure_observation(
            {},
            ledger,
            "cline",
            CLINE_OBSERVATION,
            prepare.CLINE_WINDOW_UNITS,
            prepare.CLINE_VALID,
            CLINE_LANES,
            NOW,
        )
        self.assertEqual(
            ledger.configured,
            [
                {
                    "name": "cline",
                    "capacity": 1,
                    "windows": {"five_hour": 87.5, "weekly": 56.0, "monthly": 91.0},
                    "expires": NOW - 60 + prepare.CLINE_VALID,
                    "models": ["cline-pass/qwen3.8-max"],
                    "alias_names": ["cline"],
                    "observed_at": NOW - 60,
                }
            ],
        )
        self.assertEqual(ledger.lanes["cline"]["used_percent_max"], 44.0)

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
            "cline",
            dict(CLINE_OBSERVATION, status="unknown"),
            prepare.CLINE_WINDOW_UNITS,
            prepare.CLINE_VALID,
            CLINE_LANES,
            NOW,
        )
        self.assertEqual(ledger.configured, [])
        self.assertIsNone(ledger.lanes["cline"]["quota_observed_at"])
        self.assertEqual(
            ledger.record_lane("cline", ledger.lanes["cline"])["reason"], "quota_unobserved"
        )

    def test_absent_lane_config_leaves_the_accounts_alone(self):
        ledger = FakeLedger()
        prepare.configure_goat({"goat_observation_path": str(self.tmp / "nope.json")}, ledger, NOW)
        prepare.configure_cline(
            {"cline_observation_path": str(self.tmp / "nope.json")}, ledger, NOW
        )
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

    def test_configure_reads_both_observations_from_their_config_paths(self):
        ledger = FakeLedger()
        config = {
            "goat_observation_path": str(
                self.observation("goat-observation.json", GOAT_OBSERVATION)
            ),
            "goat_lanes": GOAT_LANES,
            "cline_observation_path": str(
                self.observation("cline-observation.json", CLINE_OBSERVATION)
            ),
            "cline_lanes": CLINE_LANES,
        }
        prepare.configure_goat(config, ledger, NOW)
        prepare.configure_cline(config, ledger, NOW)
        self.assertEqual([c["name"] for c in ledger.configured], ["goat-account", "cline"])
        self.assertEqual(set(ledger.lanes), {"goat", "goat-mini", "cline"})


if __name__ == "__main__":
    unittest.main()
