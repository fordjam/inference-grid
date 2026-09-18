"""The versioned local deployment layer: collectors, the Claude poller, the scheduler loop,
the quota feed, the board tick loop and the installer, all against fixture payloads and
injected network.

No test opens a socket: every network call goes through an injected ``fetch`` (or a fake
status command), and every credential path points at a file in a temporary directory.
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import plistlib
import signal
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deployments" / "local"


def load(name):
    spec = importlib.util.spec_from_file_location("deploy_" + name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


zai = load("collect_zai")
codex = load("collect_codex")
goat = load("collect_goat")
claude = load("refresh_claude")
overlay = load("overlay_build")
feed = load("capacity_feed")
loop = load("capacity_loop")
tick_boards = load("tick_boards")
upload = load("upload")
installer = load("install")


def iso_ms(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def iso_s(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def raise_(exc):
    return lambda *a, **k: (_ for _ in ()).throw(exc)


def raise_exit(code):
    raise SystemExit(code)


def with_drain_handlers(test, drain):
    """Real SIGTERM/SIGINT handlers for the process, restored after the test."""
    previous = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    tick_boards.install_drain(drain)
    test.addCleanup(signal.signal, signal.SIGTERM, previous[0])
    test.addCleanup(signal.signal, signal.SIGINT, previous[1])


class _TmpDir:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()


# ---------------------------------------------------------------- provider fixtures

ZAI_BODY = {
    "code": 200,
    "success": True,
    "data": {
        "level": "level2",
        "limits": [
            {
                "unit": 3,
                "number": 5,
                "percentage": 42.5,
                "remaining": 1150,
                "usage": 2000,
                "nextResetTime": 1757916000000,
            },
            {
                "unit": 6,
                "number": 1,
                "percentage": 7.0,
                "remaining": 9300,
                "usage": 10000,
                "nextResetTime": 1758434400000,
            },
        ],
    },
}

CODEX_BODY = {
    "plan_type": "pro",
    "rate_limit": {
        "limit_reached": False,
        "primary_window": {
            "limit_window_seconds": 604800,
            "used_percent": 11.25,
            "reset_at": 1758434400,
        },
        "secondary_window": {
            "limit_window_seconds": 18000,
            "used_percent": 3.5,
            "reset_at": 1757916000,
        },
    },
}

GOAT_CREDITS = {
    "windowLimits": {
        "fiveHour": {"used": 5.0, "cap": 14.0, "resetAt": 1757916000000},
        "weekly": {"used": 30.0, "cap": 35.0, "resetAt": 1758434400000},
    },
    "credits": {"monthlyCredits": 21},
}

CLAUDE_USAGE = {
    "five_hour": {"utilization": 33.0, "resets_at": "2026-09-15T07:00:00Z"},
    "seven_day": {"utilization": 12.0, "resets_at": "2026-09-19T00:00:00Z"},
    "seven_day_opus": {"utilization": 5.0, "resets_at": "2026-09-19T00:00:00Z"},
}


class ObservationShapeMixin:
    """Every collector's parse output is the account entry the overlay merges."""

    def assert_observation_shape(self, obs, provider):
        self.assertEqual(obs["provider"], provider)
        self.assertIn(obs["status"], ("ok", "unknown", "error"))
        self.assertIsInstance(obs["observed_at"], str)
        for w in obs["windows"]:
            self.assertIn(w["id"], ("five_hour", "weekly", "monthly", "weekly_opus"))
            self.assertIsInstance(w["used_percent"], (int, float))
            self.assertTrue(w["resets_at"] is None or isinstance(w["resets_at"], str))


class ZaiCollectorTests(ObservationShapeMixin, unittest.TestCase):
    def test_parse_maps_unit_number_pairs_to_windows(self):
        obs = zai.parse(ZAI_BODY)
        self.assertEqual(obs["status"], "ok")
        self.assertEqual(obs["level"], "level2")
        self.assert_observation_shape(obs, "zai")
        windows = {w["id"]: w for w in obs["windows"]}
        self.assertEqual(set(windows), {"five_hour", "weekly"})
        self.assertEqual(windows["five_hour"]["used_percent"], 42.5)
        self.assertEqual(windows["five_hour"]["resets_at"], iso_ms(1757916000000))
        self.assertEqual(windows["weekly"]["used_percent"], 7.0)
        self.assertEqual(windows["weekly"]["resets_at"], iso_ms(1758434400000))
        self.assertEqual(windows["five_hour"]["remaining_units"], 1150)

    def test_parse_skips_unknown_and_malformed_windows(self):
        body = {
            "code": 200,
            "success": True,
            "data": {
                "limits": [
                    {"unit": 1, "number": 1, "percentage": 50.0, "nextResetTime": 1757916000000},
                    {"unit": 3, "number": 5, "percentage": True, "nextResetTime": 1757916000000},
                    {"unit": 3, "number": 5, "percentage": 120.0},
                    {"unit": 3, "number": 5, "percentage": 10.0, "nextResetTime": 1757916000000},
                ]
            },
        }
        obs = zai.parse(body)
        self.assertEqual([w["id"] for w in obs["windows"]], ["five_hour"])
        self.assertEqual(obs["windows"][0]["resets_at"], iso_ms(1757916000000))

    def test_parse_reports_api_refusals(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            obs = zai.parse({"code": 401, "success": False})
        self.assertEqual(obs["status"], "error")
        self.assertEqual(obs["error"], "ValueError")
        self.assertEqual(obs["windows"], [])
        self.assertIn("Traceback (most recent call last)", err.getvalue())

    def test_observe_reads_the_key_from_the_configured_path(self):
        tmp = self.enterContext(_TmpDir())
        cred = tmp.path / "zai-coding-plan.json"
        cred.write_text(json.dumps({"api_key": "fake-key"}))
        seen = {}

        def fetch(url, headers, timeout):
            seen["url"], seen["headers"] = url, headers
            return ZAI_BODY

        obs = zai.observe(fetch, {"zai_credential_path": str(cred)})
        self.assertEqual(obs["status"], "ok")
        self.assertEqual(seen["url"], zai.API_URL)
        self.assertEqual(seen["headers"]["Authorization"], "fake-key")

    def test_observe_failure_is_reported_not_raised(self):
        tmp = self.enterContext(_TmpDir())
        cred = tmp.path / "zai-coding-plan.json"
        cred.write_text(json.dumps({"api_key": "fake-key"}))
        config = {"zai_credential_path": str(cred)}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            obs = zai.observe(raise_(RuntimeError()), config)
        self.assertEqual((obs["status"], obs["error"]), ("error", "RuntimeError"))
        self.assertIn("Traceback (most recent call last)", err.getvalue())
        obs = zai.observe(
            raise_(RuntimeError()), {"zai_credential_path": str(tmp.path / "missing.json")}
        )
        self.assertEqual((obs["status"], obs["error"]), ("error", "FileNotFoundError"))

    def test_write_updates_the_ledger_quota_only_when_ok(self):
        tmp = self.enterContext(_TmpDir())
        now = datetime.datetime.now(datetime.timezone.utc)
        config = {"output_dir": str(tmp.path), "zai_quota_path": str(tmp.path / "zai-quota.json")}
        zai.write(zai.parse(ZAI_BODY, now), config)
        self.assertTrue((tmp.path / "zai-observation.json").exists())
        quota = json.loads((tmp.path / "zai-quota.json").read_text())
        self.assertEqual(quota["source"], "api")
        self.assertEqual(quota["five_hour_used_percent"], 42.5)
        (tmp.path / "zai-quota.json").unlink()
        zai.write(zai.parse({"code": 500, "success": False}, now), config)
        self.assertFalse((tmp.path / "zai-quota.json").exists())


class CodexCollectorTests(ObservationShapeMixin, unittest.TestCase):
    def test_parse_maps_window_seconds_to_windows(self):
        usage = codex.parse_usage(CODEX_BODY)
        windows = {w["id"]: w for w in usage["windows"]}
        self.assertEqual(set(windows), {"five_hour", "weekly"})
        self.assertEqual(windows["weekly"]["used_percent"], 11.25)
        self.assertEqual(windows["weekly"]["resets_at"], iso_s(1758434400))
        self.assertEqual(windows["five_hour"]["resets_at"], iso_s(1757916000))
        self.assertFalse(usage["limit_reached"])
        self.assertEqual(usage["plan_type"], "pro")

    def test_parse_ignores_unknown_windows_and_bad_percent(self):
        body = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 60, "used_percent": 5.0},
                "secondary_window": {"limit_window_seconds": 18000, "used_percent": None},
            }
        }
        self.assertEqual(codex.parse_usage(body)["windows"], [])

    def test_observe_first_home_that_answers_wins(self):
        tmp = self.enterContext(_TmpDir())
        home = tmp.path / ".codex-seat-2"
        home.mkdir()
        (home / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "fake-token", "account_id": "acc-1"}})
        )
        seen = {}

        def fetch(url, headers, timeout):
            seen["url"], seen["headers"] = url, headers
            return CODEX_BODY

        obs = codex.observe(fetch, {}, homes=[tmp.path / ".codex-absent", home])
        self.assertEqual(obs["status"], "ok")
        self.assertEqual(obs["source_home"], ".codex-seat-2")
        self.assert_observation_shape(obs, "codex")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer fake-token")
        self.assertEqual(seen["headers"]["ChatGPT-Account-Id"], "acc-1")
        self.assertEqual(seen["url"], codex.API_URL)

    def test_observe_auth_failure_marks_the_lane(self):
        tmp = self.enterContext(_TmpDir())
        home = tmp.path / ".codex"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fake-token"}}))

        def fetch(url, headers, timeout):
            raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)

        obs = codex.observe(fetch, {}, homes=[home])
        self.assertEqual((obs["status"], obs["error"]), ("auth_required", "HTTP_403"))
        self.assertEqual(obs["windows"], [])

    def test_observe_a_home_that_answers_with_no_recognized_windows_still_wins(self):
        """First home that answers wins, even when its answer has no windows this
        module recognizes (status "unknown") -- observe() must not fall through to a
        later home just because the winning reading's own status isn't "ok"."""
        tmp = self.enterContext(_TmpDir())
        answers_but_empty = tmp.path / ".codex"
        answers_but_empty.mkdir()
        (answers_but_empty / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "t", "account_id": "a"}})
        )
        never_home = tmp.path / ".codex-seat1"

        def fetch(url, headers, timeout):
            return {"rate_limit": {"primary_window": {"limit_window_seconds": 60, "used_percent": 5.0}},
                    "rate_limit_reached_type": {"type": "usage_limit"}, "plan_type": "pro"}

        obs = codex.observe(fetch, {}, homes=[answers_but_empty, never_home])
        self.assertEqual(obs["status"], "unknown")
        self.assertEqual(obs["source_home"], ".codex")
        self.assertEqual(obs["plan_type"], "pro")
        self.assertEqual(obs["source_reason"], "usage_limit")
        self.assertNotIn("error", obs)

    def test_observe_unhandled_exception_logs_a_traceback_and_is_not_unknown(self):
        tmp = self.enterContext(_TmpDir())
        home = tmp.path / ".codex"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fake-token"}}))

        def fetch(url, headers, timeout):
            raise RuntimeError("connection reset")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            obs = codex.observe(fetch, {}, homes=[home])
        self.assertEqual((obs["status"], obs["error"]), ("error", "RuntimeError"))
        self.assertIn("RuntimeError: connection reset", err.getvalue())
        self.assertIn("Traceback (most recent call last)", err.getvalue())

    def _seat_home(self, tmp, name, account_id, token="fake-token"):
        home = tmp.path / name
        home.mkdir()
        (home / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": token, "account_id": account_id}})
        )
        return home

    def test_observe_seats_reads_every_home_even_after_one_succeeds(self):
        """Unlike observe() (first home that answers wins), the codex-luna/terra/sol trio
        (docs/LANES.md) needs every seat's own status, so observe_seats must not stop early."""
        tmp = self.enterContext(_TmpDir())
        main_home = self._seat_home(tmp, ".codex", "acc-main")
        seat1 = self._seat_home(tmp, ".codex-seat1", "acc-seat1")
        seen_homes = []

        def fetch(url, headers, timeout):
            seen_homes.append(headers["ChatGPT-Account-Id"])
            return CODEX_BODY

        seats = codex.observe_seats(fetch, {}, homes=[main_home, seat1])
        self.assertEqual(seen_homes, ["acc-main", "acc-seat1"])
        self.assertEqual([s["home"] for s in seats], [".codex", ".codex-seat1"])
        self.assertTrue(all(s["status"] == "ok" for s in seats))

    def test_observe_seats_reports_each_homes_own_status_independently(self):
        """One seat's expired login must not hide the other seats' readings, and must not
        make observe_seats stop -- the whole point is per-seat visibility."""
        tmp = self.enterContext(_TmpDir())
        good = self._seat_home(tmp, ".codex", "acc-main")
        expired = self._seat_home(tmp, ".codex-seat1", "acc-seat1")
        missing = tmp.path / ".codex-seat2"  # no auth.json at all

        def fetch(url, headers, timeout):
            if headers["ChatGPT-Account-Id"] == "acc-seat1":
                raise urllib.error.HTTPError(url, 401, "expired", {}, None)
            return CODEX_BODY

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seats = codex.observe_seats(fetch, {}, homes=[good, expired, missing])
        by_home = {s["home"]: s for s in seats}
        self.assertEqual(by_home[".codex"]["status"], "ok")
        self.assertEqual(by_home[".codex-seat1"]["status"], "auth_required")
        self.assertEqual(by_home[".codex-seat2"]["status"], "error")
        self.assertEqual(by_home[".codex-seat2"]["error"], "FileNotFoundError")

    def test_write_folds_seats_in_without_disturbing_the_merged_shape(self):
        tmp = self.enterContext(_TmpDir())
        config = {"output_dir": str(tmp.path)}
        obs = {"provider": "codex", "status": "ok", "observed_at": "t", "windows": []}
        seats = [{"home": ".codex", "status": "ok", "windows": []}]
        codex.write(obs, config, seats=seats)
        written = json.loads((tmp.path / "codex-observation.json").read_text())
        self.assertEqual(written["provider"], "codex")
        self.assertEqual(written["status"], "ok")
        self.assertEqual(written["seats"], seats)

    def test_write_without_seats_keeps_the_old_shape(self):
        tmp = self.enterContext(_TmpDir())
        config = {"output_dir": str(tmp.path)}
        obs = {"provider": "codex", "status": "ok", "observed_at": "t", "windows": []}
        codex.write(obs, config)
        written = json.loads((tmp.path / "codex-observation.json").read_text())
        self.assertNotIn("seats", written)


class GoatCollectorTests(ObservationShapeMixin, unittest.TestCase):
    def credential(self):
        tmp = self.enterContext(_TmpDir())
        cred = tmp.path / "auth.json"
        cred.write_text(json.dumps({"apiKey": "fake-key"}))
        return tmp, cred

    def test_parse_derives_monthly_percent_from_the_70_cap(self):
        obs = goat.parse(GOAT_CREDITS)
        self.assertEqual(obs["status"], "ok")
        self.assert_observation_shape(obs, "command-code")
        windows = {w["id"]: w for w in obs["windows"]}
        self.assertEqual(set(windows), {"five_hour", "weekly", "monthly"})
        self.assertAlmostEqual(windows["five_hour"]["used_percent"], 5.0 / 14.0 * 100)
        self.assertAlmostEqual(windows["weekly"]["used_percent"], 30.0 / 35.0 * 100)
        self.assertEqual(windows["five_hour"]["resets_at"], iso_ms(1757916000000))
        # monthlyCredits remaining against the plan's monthly cap; no reset time exposed
        self.assertEqual(obs["monthly_remaining"], 21)
        self.assertAlmostEqual(windows["monthly"]["used_percent"], (70 - 21) / 70 * 100)
        self.assertIsNone(windows["monthly"]["resets_at"])

    def test_parse_tolerates_a_missing_monthly_credit_value(self):
        credits = {"windowLimits": GOAT_CREDITS["windowLimits"], "credits": {}}
        obs = goat.parse(credits)
        self.assertEqual([w["id"] for w in obs["windows"]], ["five_hour", "weekly"])
        self.assertIsNone(obs["monthly_remaining"])

    def test_observe_uses_the_fake_status_command_and_two_endpoints(self):
        tmp, cred = self.credential()
        urls = []

        def fetch(url, headers, timeout):
            urls.append(url)
            if "whoami" in url:
                return {}
            return GOAT_CREDITS

        def status_cmd():
            return {"authenticated": True, "version": "9.9.9"}

        obs = goat.observe(fetch, {"goat_credential_path": str(cred)}, status_cmd=status_cmd)
        self.assertEqual(obs["status"], "ok")
        self.assertEqual(
            urls, [goat.API_BASE + "whoami?limits=1", goat.API_BASE + "billing/credits"]
        )

    def test_observe_refuses_organizations_and_unauthenticated_cli(self):
        tmp, cred = self.credential()
        config = {"goat_credential_path": str(cred)}

        def ok_status():
            return {"authenticated": True, "version": "9.9.9"}

        def org_fetch(url, headers, timeout):
            return {"org": "some-org"}

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            obs = goat.observe(org_fetch, config, status_cmd=ok_status)
        self.assertEqual((obs["status"], obs["error"]), ("error", "ValueError"))
        self.assertIn("Traceback (most recent call last)", err.getvalue())

        def closed_status():
            return {"authenticated": False}

        obs = goat.observe(org_fetch, config, status_cmd=closed_status)
        self.assertEqual(obs["status"], "error")

    def test_observe_sends_the_bearer_key(self):
        tmp, cred = self.credential()
        seen = {}

        def fetch(url, headers, timeout):
            seen["headers"] = headers
            if "whoami" in url:
                return {}
            return GOAT_CREDITS

        obs = goat.observe(
            fetch,
            {"goat_credential_path": str(cred)},
            status_cmd=lambda: {"authenticated": True, "version": "1.0"},
        )
        self.assertEqual(obs["status"], "ok")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer fake-key")


class RefreshClaudeTests(unittest.TestCase):
    NOW = 1_000_000.0

    def credentials(self, expires_in=600.0):
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "fake-token",
                    "expiresAt": (self.NOW + expires_in) * 1000,
                }
            }
        )

    def observe(self, fetch, credentials=None, prior=None, state=None):
        return claude.observe(
            fetch, credentials or self.credentials(), prior or {}, state or {}, now=self.NOW
        )

    def test_success_maps_the_three_windows(self):
        a, delay, info = self.observe(lambda url, headers, timeout: CLAUDE_USAGE)
        self.assertEqual(a["status"], "ok")
        self.assertEqual([w["id"] for w in a["windows"]], ["five_hour", "weekly", "weekly_opus"])
        self.assertEqual(a["windows"][1]["used_percent"], 12.0)
        self.assertIsNone(info["http_status"])

    def test_expired_token_is_auth_required_and_carries_prior_windows_stale(self):
        prior = {
            "windows": [
                {"id": "five_hour", "used_percent": 8.0, "resets_at": "2026-09-15T06:00:00+00:00"}
            ],
            "observed_at": "earlier",
        }
        a, delay, _ = self.observe(
            raise_(AssertionError("no request expected")),
            credentials=self.credentials(expires_in=-60),
            prior=prior,
        )
        self.assertEqual(a["status"], "auth_required")
        self.assertEqual(a["windows"], prior["windows"])
        self.assertTrue(a["stale"])
        self.assertEqual(a["observed_at"], "earlier")
        self.assertEqual(delay, 900)

    def test_any_failure_carries_prior_windows_stale(self):
        prior = {"windows": [{"id": "weekly", "used_percent": 2.0, "resets_at": None}]}
        a, delay, _ = self.observe(raise_(RuntimeError("socket")), prior=prior)
        self.assertEqual(a["status"], "error")
        self.assertEqual(a["windows"], prior["windows"])
        self.assertTrue(a["stale"])
        self.assertEqual(delay, 900)

    def test_an_unhandled_exception_logs_a_timestamped_traceback(self):
        """02-A4: grep -c unknown claude-collector.log must stop growing, and a failure
        must be diagnosable from the log alone, not just a status word."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.observe(raise_(RuntimeError("socket exploded")))
        logged = err.getvalue()
        self.assertIn("ERROR claude poll failed", logged)
        self.assertIn("RuntimeError: socket exploded", logged)
        self.assertIn("Traceback (most recent call last)", logged)

    def test_rate_limit_backs_off_and_honours_retry_after(self):
        err = urllib.error.HTTPError(
            claude.USAGE_URL, 429, "slow down", {"Retry-After": "1200"}, None
        )
        prior = {"windows": [{"id": "five_hour", "used_percent": 1.0, "resets_at": None}]}
        a, delay, info = self.observe(raise_(err), prior=prior, state={"delay": 450})
        self.assertEqual((a["status"], info["http_status"]), ("rate_limited", 429))
        self.assertEqual(info["retry_after_seconds"], 1200)
        self.assertEqual(delay, 1200)
        self.assertTrue(a["stale"])

    def test_auth_errors_are_reported_as_auth_required(self):
        err = urllib.error.HTTPError(claude.USAGE_URL, 401, "auth", {}, None)
        a, delay, info = self.observe(raise_(err))
        self.assertEqual((a["status"], delay, info["http_status"]), ("auth_required", 900, 401))

    def test_retry_seconds(self):
        self.assertEqual(claude.retry_seconds("30", self.NOW), 30)
        self.assertEqual(claude.retry_seconds("nonsense", self.NOW), None)
        self.assertEqual(claude.retry_seconds(None, self.NOW), None)
        future = datetime.datetime.fromtimestamp(self.NOW + 60, datetime.timezone.utc)
        self.assertAlmostEqual(
            claude.retry_seconds(future.strftime("%a, %d %b %Y %H:%M:%S GMT"), self.NOW),
            60,
            delta=2,
        )


class CapacityLoopTests(unittest.TestCase):
    def job(self, name, period):
        return {
            "period": period,
            "argv": ["python", f"/x/{name}.py"],
            "log": "unused",
            "error_log": "unused",
        }

    def test_each_job_runs_on_its_own_period(self):
        clock = FakeClock()
        runs = {"collector_a": 0, "collector_b": 0}

        def spawn(job):
            name = Path(job["argv"][1]).stem
            runs[name] += 1

        jobs = [self.job("collector_a", 300), self.job("collector_b", 60)]
        loop.run(jobs, clock.clock, clock.sleep, spawn, stop=lambda: clock.now > 1000)
        self.assertEqual(runs, {"collector_a": 4, "collector_b": 17})

    def test_one_job_exception_never_stops_the_others(self):
        clock = FakeClock()
        tmp = self.enterContext(_TmpDir())
        good_runs = []

        def spawn(job):
            if "bad" in job["argv"][1]:
                raise RuntimeError("child failed")
            good_runs.append(clock.now)

        jobs = [self.job("bad_job", 100), self.job("good_job", 100)]
        for job in jobs:
            job["error_log"] = str(tmp.path / "errors.log")
        loop.run(jobs, clock.clock, clock.sleep, spawn, stop=lambda: clock.now > 350)
        self.assertEqual(len(good_runs), 4)
        self.assertIn(
            "capacity-loop: bad_job.py RuntimeError", (tmp.path / "errors.log").read_text()
        )

    def test_failure_logging_survives_an_unwritable_log(self):
        job = self.job("bad_job", 60)
        job["error_log"] = "/nonexistent-dir-for-tests/errors.log"
        with contextlib.redirect_stdout(io.StringIO()):
            loop._log_failure(job, RuntimeError("x"))


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ThreadClock:
    """A fake clock per thread: each board loop of tick_boards.run sleeps on its own
    thread, so its deadline arithmetic must not see the other boards' sleeps."""

    def __init__(self, start=0.0):
        self._start = start
        self._local = threading.local()
        self.sleeps = []
        self._lock = threading.Lock()

    @property
    def now(self):
        return getattr(self._local, "now", self._start)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        with self._lock:
            self.sleeps.append(seconds)
        self._local.now = self.now + seconds


class TickBoardsTests(unittest.TestCase):
    def test_prepare_runs_before_every_tick_of_every_board(self):
        clock = ThreadClock()
        events = []
        lock = threading.Lock()

        def prepare():
            with lock:
                events.append(("prepare", threading.current_thread().name))

        def tick(board):
            # Each board loops on its own thread: its ticks are preceded by its own
            # prepares, and the two boards' loops interleave however they like.
            with lock:
                events.append(("tick:" + board, threading.current_thread().name))
            return 0

        tick_boards.run(
            ["a", "b"],
            deadline=7200,
            prepare=prepare,
            tick=tick,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        for board in ("a", "b"):
            own = [kind for kind, thread in events if thread == board]
            self.assertEqual(own, ["prepare", "tick:" + board] * 4)  # 7200 s of idle sleeps
        self.assertEqual(sorted(clock.sleeps), [tick_boards.IDLE_SECONDS] * 8)

    def test_on_pass_fires_once_per_successful_board_tick(self):
        # With each board looping on its own thread there is no synchronized "pass"
        # shared across boards any more (unlike the old single-loop-thread design);
        # on_pass now fires once per board's own successful tick, so two boards each
        # completing two idle passes before the deadline fire it four times in total.
        clock = ThreadClock()
        passes = []
        lock = threading.Lock()

        def on_pass():
            with lock:
                passes.append(1)

        tick_boards.run(
            ["a", "b"],
            deadline=3600,
            prepare=lambda: None,
            tick=lambda board: 0,
            sleep=clock.sleep,
            clock=clock.clock,
            on_pass=on_pass,
        )
        self.assertEqual(len(passes), 4)

    def test_boards_tick_concurrently_and_the_ready_count_is_summed(self):
        clock = ThreadClock()
        rendezvous = threading.Barrier(2, timeout=10)

        def tick(board):
            # Both boards are in flight at once; a serial loop would time out here.
            rendezvous.wait()
            return 1

        ready = tick_boards.run(
            ["a", "b"],
            deadline=1,
            prepare=lambda: None,
            tick=tick,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        self.assertEqual(ready, 2)  # the boards' last counts, summed
        self.assertEqual(clock.sleeps, [tick_boards.BUSY_SECONDS] * 2)  # one tick each

    def test_a_slow_board_never_holds_the_others(self):
        # A failed assertion raised inside `tick` would be swallowed by run()'s own
        # except Exception (a's "packet" appearing to fail and get retried) and the
        # test would still pass vacuously -- so the wait's own result is recorded
        # here and asserted only after run() returns, outside tick's exception scope.
        clock = ThreadClock()
        released = threading.Event()
        ticks = {"a": 0, "b": 0}
        lock = threading.Lock()
        waited = []
        out = io.StringIO()

        def tick(board):
            with lock:
                ticks[board] += 1
                b_done = ticks["b"]
            if board == "a":
                # a's first tick is a long packet: it returns only once b has ticked
                # four times on its own — the pass-and-join loop would deadlock here.
                waited.append(released.wait(10))
            elif b_done == 4:
                released.set()
            return 0

        with contextlib.redirect_stdout(out):
            tick_boards.run(
                ["a", "b"],
                deadline=7200,
                prepare=lambda: None,
                tick=tick,
                sleep=clock.sleep,
                clock=clock.clock,
            )
        self.assertEqual(ticks["b"], 4)
        self.assertGreaterEqual(ticks["a"], 1)
        self.assertTrue(waited and all(waited))
        self.assertNotIn("tick failed", out.getvalue())

    def test_a_failing_board_never_stops_another_board(self):
        # Merged from main (which had one board's tick exception stop every board's
        # loop and re-raise) with the branch's own, stronger isolation guarantee
        # (c634f8a): a board's own failure -- prepare or tick -- blocks only that
        # board for that pass and is never allowed to touch another board's loop.
        # b keeps ticking on its own schedule while a fails once and then recovers.
        clock = ThreadClock()
        ticked = {"a": 0, "b": 0}
        lock = threading.Lock()
        failed_once = threading.Event()

        def tick(board):
            with lock:
                ticked[board] += 1
            if board == "a" and not failed_once.is_set():
                failed_once.set()
                raise RuntimeError("transient failure on a")
            return 0

        ready = tick_boards.run(
            ["a", "b"],
            deadline=3600,
            prepare=lambda: None,
            tick=tick,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        # a's first tick failed and was retried; b was never blocked by it.
        self.assertEqual(ticked["a"], 2)
        self.assertEqual(ticked["b"], 2)
        self.assertEqual(ready, 0)  # both boards' last successful count was 0

    def test_ready_count_chooses_the_sleep_interval(self):
        clock = ThreadClock()
        tick_boards.run(
            ["a"],
            deadline=1000,
            prepare=lambda: None,
            tick=lambda board: 2,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        self.assertEqual(clock.sleeps, [tick_boards.BUSY_SECONDS] * 4)
        clock = ThreadClock()
        tick_boards.run(
            ["a"],
            deadline=1000,
            prepare=lambda: None,
            tick=lambda board: 0,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        self.assertEqual(clock.sleeps, [tick_boards.IDLE_SECONDS])

    def test_count_ready_reads_the_board_directory(self):
        tmp = self.enterContext(_TmpDir())
        (tmp.path / "t1.json").write_text(json.dumps({"state": "ready"}))
        (tmp.path / "t2.json").write_text(json.dumps({"state": "running"}))
        self.assertEqual(tick_boards.count_ready(str(tmp.path)), 1)
        self.assertEqual(tick_boards.count_ready(None), 0)

    def test_a_signal_lets_the_ticks_in_flight_finish_then_ends_the_loop(self):
        tmp = self.enterContext(_TmpDir())
        state = tmp.path / "tick-boards.draining"
        drain = tick_boards.Drain(state)
        with_drain_handlers(self, drain)
        events = []
        rendezvous = threading.Barrier(3, timeout=10)
        finish = threading.Event()

        def tick(board):
            # Both boards of the pass are in flight at once (J6); each waits here until
            # the signal lets it finish.
            events.append("tick:" + board)
            rendezvous.wait()
            self.assertTrue(finish.wait(10))
            return 0

        def send():
            rendezvous.wait()  # both ticks started: the tick in flight is the whole pass
            os.kill(os.getpid(), signal.SIGTERM)
            # The handler runs asynchronously on the main thread; wait for it to have
            # actually landed (drain.draining true) before releasing the ticks in
            # flight, or a board could see draining() still False and start a second
            # tick before the signal is processed.
            deadline = time.time() + 5
            while not drain.draining and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(drain.draining)
            finish.set()

        sender = threading.Thread(target=send)
        sender.start()
        tick_boards.run(
            ["a", "b"],
            deadline=time.time() + 30,
            prepare=lambda: None,
            tick=tick,
            sleep=lambda seconds: events.append("sleep"),
            clock=time.time,
            drain=drain,
        )
        sender.join(5)
        self.assertEqual(sorted(events), ["tick:a", "tick:b"])
        self.assertNotIn("sleep", events)
        self.assertTrue(drain.draining)
        self.assertEqual(state.read_text(), "draining\n")

    def test_a_second_signal_ends_the_tick_at_once(self):
        tmp = self.enterContext(_TmpDir())
        drain = tick_boards.Drain(tmp.path / "tick-boards.draining", exit=raise_exit)
        with_drain_handlers(self, drain)
        events = []
        started = threading.Event()

        def tick(board):
            events.append("tick:" + board)
            started.set()
            time.sleep(5)
            events.append("finished")

        def send():
            self.assertTrue(started.wait(5))
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        sender = threading.Thread(target=send)
        sender.start()
        with self.assertRaises(SystemExit) as caught:
            tick_boards.run(
                ["a"],
                deadline=time.time() + 30,
                prepare=lambda: None,
                tick=tick,
                sleep=lambda seconds: None,
                clock=time.time,
                drain=drain,
            )
        sender.join(5)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(events, ["tick:a"])


class DrainTests(unittest.TestCase):
    def test_first_signal_starts_the_drain_and_writes_the_state_file(self):
        tmp = self.enterContext(_TmpDir())
        state = tmp.path / "tick-boards.draining"
        drain = tick_boards.Drain(state, clock=lambda: 100.0)
        drain.on_signal(signal.SIGTERM, None)
        self.assertTrue(drain.draining)
        self.assertEqual(drain.since, 100.0)
        self.assertEqual(state.read_text(), "draining\n")

    def test_second_signal_within_the_grace_exits_at_once(self):
        tmp = self.enterContext(_TmpDir())
        now = [100.0]
        exits = []
        drain = tick_boards.Drain(tmp.path / "s", clock=lambda: now[0], exit=exits.append)
        drain.on_signal(signal.SIGTERM, None)
        now[0] += 10
        drain.on_signal(signal.SIGINT, None)
        self.assertEqual(exits, [128 + signal.SIGINT])
        self.assertTrue(drain.draining)

    def test_second_signal_after_the_grace_keeps_draining(self):
        tmp = self.enterContext(_TmpDir())
        now = [100.0]
        exits = []
        drain = tick_boards.Drain(tmp.path / "s", clock=lambda: now[0], exit=exits.append)
        drain.on_signal(signal.SIGTERM, None)
        now[0] += tick_boards.DRAIN_GRACE_SECONDS + 1
        drain.on_signal(signal.SIGTERM, None)
        self.assertEqual(exits, [])
        self.assertTrue(drain.draining)

    def test_clear_removes_the_marker_and_tolerates_its_absence(self):
        tmp = self.enterContext(_TmpDir())
        state = tmp.path / "s"
        drain = tick_boards.Drain(state)
        drain.clear()
        state.write_text("draining\n")
        drain.clear()
        self.assertFalse(state.exists())


class CalibrationTriggerTests(unittest.TestCase):
    def test_due_when_never_scored_or_past_the_window(self):
        now = 1_700_000_000.0
        self.assertTrue(tick_boards.calibration_due(None, 7, now))
        self.assertFalse(tick_boards.calibration_due(now - 6 * 86400, 7, now))
        self.assertTrue(tick_boards.calibration_due(now - 7 * 86400, 7, now))
        self.assertTrue(tick_boards.calibration_due(now - 30 * 86400, 7, now))

    def test_the_trigger_runs_before_the_boards(self):
        clock = ThreadClock()
        events = []
        tick_boards.run(
            ["a"],
            deadline=7200,
            prepare=lambda: events.append("prepare"),
            tick=lambda board: (events.append("tick:" + board), 0)[1],
            sleep=clock.sleep,
            clock=clock.clock,
            calibrate=lambda: events.append("calibrate"),
        )
        # This uses the boards' own injected (fake, per-thread) clock, which the outer
        # joining thread's own next-interval check never advances -- so this proves only
        # the one call before the boards start, not the recurring schedule; see
        # test_calibrate_recurs_on_the_outer_thread_at_the_idle_interval below for that.
        self.assertEqual(events.count("calibrate"), 1)
        self.assertEqual(events[:3], ["calibrate", "prepare", "tick:a"])
        self.assertEqual(events.count("tick:a"), 4)

    def test_calibrate_recurs_on_the_outer_thread_at_the_idle_interval(self):
        # Unlike the test above, this uses the real wall clock (the outer loop's own
        # `next_calibrate` check is always against real time via its hardcoded
        # thread.join(1.0) poll, regardless of what clock/sleep a caller injects for
        # the boards) with a tiny real idle, so the recurring schedule genuinely fires
        # more than once within the test's short real deadline.
        calls = []
        lock = threading.Lock()

        def calibrate():
            with lock:
                calls.append(time.time())

        def tick(board):
            time.sleep(0.05)
            return 0

        tick_boards.run(
            ["a"],
            deadline=time.time() + 2.5,
            prepare=lambda: None,
            tick=tick,
            sleep=time.sleep,
            clock=time.time,
            idle=0.3,
            calibrate=calibrate,
        )
        # One call before the boards start, and at least one more from the recurring
        # schedule -- not just the single up-front call the test above covers.
        self.assertGreaterEqual(len(calls), 2)

    def test_default_calibrate_reads_the_ledger_clock(self):
        tmp = self.enterContext(_TmpDir())
        from inference_grid.board.calibration import newest_calibration_at
        from inference_grid.ledger import Ledger, attempts as attempts_t, tasks as tasks_t

        url = "sqlite:///" + str(tmp.path / "l.sqlite")
        ledger = Ledger(url)
        ledger.initialize()
        board = tmp.path / "project" / "grid" / "board"
        corpus = tmp.path / "corpus"
        corpus.mkdir(parents=True)
        config = {
            "database_url": url,
            "calibration": {
                "board_dir": str(board),
                "corpus_dir": str(corpus),
                "lanes": ["go"],
                "every_days": 7,
            },
        }
        # Nothing scored yet: the trigger authors a run onto the configured board.
        first = tick_boards.default_calibrate(config, now=1_700_000_000.0)
        self.assertTrue(first.startswith("calibration-auto-"))
        self.assertTrue((board / (first + ".json")).is_file())
        # A fresh calibration outcome (the ledger's own state) closes the window.
        aid = "11111111-2222-3333-4444-777777777777"
        with ledger.engine.begin() as con:
            con.execute(tasks_t.insert().values(id="t1", project="p", spec={}))
            con.execute(
                attempts_t.insert().values(
                    id=aid,
                    task="t1",
                    account="a",
                    generation=1,
                    state="completed",
                    estimate={},
                    workspace="/w",
                    receipt={},
                    updated=0.0,
                )
            )
        ledger.record_outcome(aid, "calibration", True, note="calibration auto-1/c1: 1/1 recalled")
        scored = newest_calibration_at(ledger)
        self.assertIsNone(tick_boards.default_calibrate(config, now=scored + 1))
        again = tick_boards.default_calibrate(config, now=scored + 8 * 86400)
        self.assertTrue(again.startswith("calibration-auto-"))


class UploadTests(unittest.TestCase):
    def config(self):
        return {
            "local_feed": "http://feed.local/overlay.json",
            "remote_url": "http://remote.example",
            "upload_token": "fake-token",
        }

    def test_only_sanitized_keys_leave_the_machine(self):
        posted = {}

        def fetch(url, headers, timeout, data=None, want_status=False):
            if url == self.config()["local_feed"]:
                return {
                    "accounts": [
                        {
                            "provider": "zai",
                            "status": "ok",
                            "observed_at": "t",
                            "windows": [{"id": "five_hour", "used_percent": 1.0}],
                            "monthly_remaining": None,
                            "secret_never_uploaded": "x",
                        }
                    ]
                }
            posted["url"], posted["headers"], posted["body"] = url, headers, json.loads(data)
            return 200

        status = upload.run(self.config(), fetch, now="now")
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["providers"], 1)
        account = posted["body"]["accounts"][0]
        self.assertEqual(set(account), set(upload.ACCOUNT_KEYS))
        self.assertNotIn("secret_never_uploaded", account)
        self.assertEqual(posted["headers"]["Authorization"], "Bearer fake-token")
        self.assertEqual(posted["url"], "http://remote.example/api/snapshot")

    def test_boards_never_leave_the_machine(self):
        # The local Boards section carries task ids and titles (the operator's project
        # names); the upload body is built from the account keys only.
        posted = {}

        def fetch(url, headers, timeout, data=None, want_status=False):
            if url == self.config()["local_feed"]:
                return {
                    "accounts": [{"provider": "zai", "status": "ok", "observed_at": "t"}],
                    "boards": [
                        {"name": "myproject", "planned": [{"id": "packet-x1", "title": "S"}]}
                    ],
                }
            posted["body"] = json.loads(data)
            return 200

        status = upload.run(self.config(), fetch, now="now")
        self.assertEqual(status["status"], "ok")
        self.assertNotIn("boards", posted["body"])
        self.assertNotIn("packet-x1", json.dumps(posted["body"]))

    def test_failure_is_reported_not_raised(self):
        status = upload.run(self.config(), raise_(RuntimeError("feed down")), now="now")
        self.assertEqual((status["status"], status["error"]), ("failed", "RuntimeError"))

    def test_main_writes_status_and_exits_nonzero_on_failure(self):
        tmp = self.enterContext(_TmpDir())
        config_path = tmp.path / "client.json"
        config_path.write_text(json.dumps(self.config()))
        original = upload.default_fetch
        upload.default_fetch = raise_(RuntimeError("feed down"))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = upload.main([str(config_path)])
        finally:
            upload.default_fetch = original
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads((tmp.path / "upload-status.json").read_text())["status"], "failed"
        )


class OverlayBuildTests(unittest.TestCase):
    def test_build_merges_observation_files_and_reads_attempts_readonly(self):
        tmp = self.enterContext(_TmpDir())
        (tmp.path / "zai-observation.json").write_text(json.dumps(zai.parse(ZAI_BODY)))
        (tmp.path / "codex-observation.json").write_text(
            json.dumps({"provider": "codex", "status": "ok", "observed_at": "t", "windows": []})
        )
        (tmp.path / "goat-observation.json").write_text(json.dumps(goat.parse(GOAT_CREDITS)))
        config = {
            "output_dir": str(tmp.path),
            "claude_overlay_path": str(tmp.path / "no-prior-overlay.json"),
            "go_live_path": str(tmp.path / "no-go-live.json"),
            "board_db": str(tmp.path / "no-board.sqlite"),
            "codex_homes": [str(tmp.path / "no-codex-home")],
        }
        result = overlay.build(config)
        self.assertEqual(
            [a["provider"] for a in result["accounts"]],
            ["zai", "codex", "command-code"],
        )
        self.assertEqual(result["attempts"], [])

    def test_zai_falls_back_to_the_operator_quota_file(self):
        tmp = self.enterContext(_TmpDir())
        (tmp.path / "zai-observation.json").write_text(
            json.dumps({"provider": "zai", "status": "unknown", "windows": []})
        )
        (tmp.path / "zai-quota.json").write_text(
            json.dumps(
                {"observed_at": "t", "five_hour_used_percent": 10.0, "weekly_used_percent": 20.0}
            )
        )
        config = {"output_dir": str(tmp.path), "zai_quota_path": str(tmp.path / "zai-quota.json")}
        account = overlay.zai_account(config)
        self.assertEqual(account["status"], "ok")
        self.assertEqual([w["used_percent"] for w in account["windows"]], [10.0, 20.0])

    def test_prior_overlay_accounts_survive_except_zai(self):
        tmp = self.enterContext(_TmpDir())
        (tmp.path / "prior.json").write_text(
            json.dumps(
                {
                    "accounts": [
                        {"provider": "zai", "status": "ok"},
                        {"provider": "claude", "status": "ok"},
                    ]
                }
            )
        )
        accounts = overlay.prior_accounts(tmp.path / "prior.json")
        self.assertEqual([a["provider"] for a in accounts], ["claude"])


def make_board_db(path, attempt_rows=(), event_rows=()):
    """A throwaway board.sqlite with just the two tables capacity_panel reads.

    attempt_rows: (id, account, state, estimate_dict, updated_epoch)
    event_rows: (attempt, kind, detail_dict)
    """
    con = sqlite3.connect(str(path))
    con.execute(
        "create table attempts (id text, task text, account text, generation int, "
        "state text, estimate text, workspace text, receipt text, reason text, updated real)"
    )
    con.execute("create table events (id text, attempt text, kind text, detail text, at real)")
    for i, (aid, account, state, estimate, updated) in enumerate(attempt_rows):
        con.execute(
            "insert into attempts (id, task, account, generation, state, estimate, updated) "
            "values (?, ?, ?, 1, ?, ?, ?)",
            (aid, "t" + str(i), account, state, json.dumps(estimate), updated),
        )
    for i, (aid, kind, detail) in enumerate(event_rows):
        con.execute(
            "insert into events (id, attempt, kind, detail, at) values (?, ?, ?, ?, ?)",
            ("e" + str(i), aid, kind, json.dumps(detail), 0.0),
        )
    con.commit()
    con.close()


class OperatorListsCollectorAgesTests(unittest.TestCase):
    """02-A1/C3: operator_lists() wires the disk-free and collector-age rows through
    from config, defaulting collector paths to the same output_dir/zai_quota_path
    the collectors themselves already use."""

    def _initialized_url(self, tmp):
        from inference_grid.ledger import Ledger

        url = "sqlite:///" + str(tmp.path / "l.sqlite")
        Ledger(url).initialize()
        return url

    def test_defaults_collector_paths_from_output_dir_and_zai_quota_path(self):
        tmp = self.enterContext(_TmpDir())
        (tmp.path / "goat-observation.json").write_text(json.dumps({"observed_at": time.time()}))
        config = {
            "database_url": self._initialized_url(tmp),
            "output_dir": str(tmp.path),
            "zai_quota_path": str(tmp.path / "zai-quota.json"),
            "go_live_path": str(tmp.path / "no-go-live.json"),
            # Stubbed: no test may shell out to the real `df` (the row's own brief).
            "disk_df_run": lambda path: 999 * 1024**3,
        }
        result = overlay.operator_lists(config)
        self.assertEqual(
            {r["name"] for r in result["collector_ages"]}, {"go-live", "goat", "codex", "zai-quota"}
        )
        self.assertEqual(result["disk_free"]["state"], "ok")

    def test_an_explicit_collector_paths_config_overrides_the_default(self):
        tmp = self.enterContext(_TmpDir())
        only = tmp.path / "only-observation.json"
        only.write_text(json.dumps({"observed_at": time.time()}))
        config = {
            "database_url": self._initialized_url(tmp),
            "collector_paths": {"only": str(only)},
            "disk_df_run": lambda path: 999 * 1024**3,
        }
        result = overlay.operator_lists(config)
        self.assertEqual({r["name"] for r in result["collector_ages"]}, {"only"})

    def test_an_empty_collector_paths_config_disables_the_rows_rather_than_defaulting(self):
        """The review finding this row must not repeat: `collector_paths: {}` must mean
        "no collectors", the same as `needs_you()` itself treats it -- not silently fall
        back to the real ~/.local/share defaults just because `{}` is falsy."""
        tmp = self.enterContext(_TmpDir())
        config = {
            "database_url": self._initialized_url(tmp),
            "collector_paths": {},
            "disk_df_run": lambda path: 999 * 1024**3,
        }
        result = overlay.operator_lists(config)
        self.assertEqual(result["collector_ages"], [])

    def test_disk_path_config_is_honoured(self):
        tmp = self.enterContext(_TmpDir())

        def boom(path):
            self.assertEqual(path, "/no/such/volume")
            raise OSError("df: no such volume")

        config = {
            "database_url": self._initialized_url(tmp),
            "disk_path": "/no/such/volume",
            "collector_paths": {},
            "disk_df_run": boom,
        }
        result = overlay.operator_lists(config)
        # A failed df alarms rather than vanishing (same review finding as
        # inference_grid.needs_you.disk_free_row's own tests).
        self.assertEqual(result["disk_free"]["state"], "alarm")

    def test_an_unavailable_ledger_still_reports_the_two_new_rows_as_empty(self):
        result = overlay.operator_lists({"database_url": "sqlite:////no/such/dir/db.sqlite"})
        self.assertEqual(result["collector_ages"], [])
        self.assertIsNone(result["disk_free"])


class CapacityPanelTests(unittest.TestCase):
    """02-B5: per subscription, capacity consumed this window by attempts that
    landed vs failed/abandoned, from board.sqlite attempts+events, read-only."""

    def test_landed_and_failed_are_bucketed_and_summed_per_provider(self):
        tmp = self.enterContext(_TmpDir())
        db = tmp.path / "board.sqlite"
        make_board_db(
            db,
            attempt_rows=[
                ("a1", "zai-account", "accepted", {"five_hour": 3.0}, time.time()),
                ("a2", "zai-account", "accepted", {"five_hour": 2.0}, time.time()),
                ("a3", "goat-account", "failed", {"monthly": 5.0}, time.time()),
            ],
        )
        rows = {r["provider"]: r for r in overlay.capacity_panel(db)}
        self.assertEqual(rows["zai"]["landed_count"], 2)
        self.assertAlmostEqual(rows["zai"]["landed_consumed"], 5.0)
        self.assertEqual(rows["zai"]["failed_abandoned_count"], 0)
        self.assertEqual(rows["command-code"]["failed_abandoned_count"], 1)
        self.assertAlmostEqual(rows["command-code"]["failed_abandoned_consumed"], 5.0)

    def test_abandoned_always_consumes_zero_even_with_an_estimate(self):
        """ledger.py's resolve(): state="abandoned" follows only outcome="released",
        which never debits the account -- an abandoned attempt's reservation was
        given back, regardless of what its pre-run estimate said."""
        tmp = self.enterContext(_TmpDir())
        db = tmp.path / "board.sqlite"
        make_board_db(
            db,
            attempt_rows=[("a1", "zai-account", "abandoned", {"weekly": 40.0}, time.time())],
        )
        rows = {r["provider"]: r for r in overlay.capacity_panel(db)}
        self.assertEqual(rows["zai"]["failed_abandoned_count"], 1)
        self.assertEqual(rows["zai"]["failed_abandoned_consumed"], 0.0)

    def test_active_attempts_are_excluded_entirely(self):
        tmp = self.enterContext(_TmpDir())
        db = tmp.path / "board.sqlite"
        make_board_db(
            db,
            attempt_rows=[
                ("a1", "zai-account", "queued", {"five_hour": 3.0}, time.time()),
                ("a2", "zai-account", "dispatching", {"five_hour": 3.0}, time.time()),
                ("a3", "zai-account", "held", {"five_hour": 3.0}, time.time()),
            ],
        )
        self.assertEqual(overlay.capacity_panel(db), [])

    def test_completed_attempts_count_as_landed_and_consume_their_estimate(self):
        """ledger.py's finish(): state="completed" debits the estimate immediately,
        before any human review -- an attempt sitting in "completed" (not yet
        "accepted") has already consumed real capacity and must not be invisible."""
        tmp = self.enterContext(_TmpDir())
        db = tmp.path / "board.sqlite"
        make_board_db(
            db,
            attempt_rows=[("a1", "zai-account", "completed", {"five_hour": 4.0}, time.time())],
        )
        rows = {r["provider"]: r for r in overlay.capacity_panel(db)}
        self.assertEqual(rows["zai"]["landed_count"], 1)
        self.assertAlmostEqual(rows["zai"]["landed_consumed"], 4.0)

    def test_outcome_recorded_usage_is_never_summed_into_consumed(self):
        """usage (outcome_recorded events) is arbitrary caller-supplied units
        (token counts, etc) that record_outcome()'s own docstring says "feeds
        scorecard, never routing" -- nothing in ledger.py ever debits it, only
        estimate is. Summing it in would mix incompatible units."""
        tmp = self.enterContext(_TmpDir())
        db = tmp.path / "board.sqlite"
        make_board_db(
            db,
            attempt_rows=[("a1", "zai-account", "accepted", {"five_hour": 3.0}, time.time())],
            event_rows=[("a1", "outcome_recorded", {"usage": {"input_tokens": 26410}})],
        )
        rows = {r["provider"]: r for r in overlay.capacity_panel(db)}
        self.assertAlmostEqual(rows["zai"]["landed_consumed"], 3.0)

    def test_outside_the_window_is_excluded(self):
        tmp = self.enterContext(_TmpDir())
        db = tmp.path / "board.sqlite"
        now = time.time()
        make_board_db(
            db,
            attempt_rows=[("a1", "zai-account", "accepted", {"five_hour": 3.0}, now - 200000)],
        )
        self.assertEqual(overlay.capacity_panel(db, now=now, window_seconds=86400), [])
        rows = {r["provider"]: r for r in overlay.capacity_panel(db, now=now, window_seconds=300000)}
        self.assertEqual(rows["zai"]["landed_count"], 1)

    def test_unmapped_lane_becomes_unknown_provider_in_the_raw_overlay(self):
        """The raw overlay_build.capacity_panel() row exists (this is what a future
        LANE_PROVIDER entry would need to preserve) -- but src/inference_grid/capacity.py's
        clean_capacity_panel() drops "unknown" (it isn't in PROVIDERS), so this data
        does not reach the dashboard; see test_capacity.py for that half."""
        tmp = self.enterContext(_TmpDir())
        db = tmp.path / "board.sqlite"
        make_board_db(
            db, attempt_rows=[("a1", "mystery-account", "accepted", {}, time.time())]
        )
        rows = {r["provider"]: r for r in overlay.capacity_panel(db)}
        self.assertIn("unknown", rows)

    def test_missing_database_returns_empty_not_raise(self):
        self.assertEqual(overlay.capacity_panel("/no/such/board.sqlite"), [])

    def test_build_includes_the_panel_in_the_overlay(self):
        tmp = self.enterContext(_TmpDir())
        db = tmp.path / "board.sqlite"
        make_board_db(
            db, attempt_rows=[("a1", "zai-account", "accepted", {"five_hour": 1.0}, time.time())]
        )
        config = {
            "output_dir": str(tmp.path),
            "claude_overlay_path": str(tmp.path / "no-prior-overlay.json"),
            "go_live_path": str(tmp.path / "no-go-live.json"),
            "board_db": str(db),
            "codex_homes": [str(tmp.path / "no-codex-home")],
        }
        result = overlay.build(config)
        self.assertEqual([r["provider"] for r in result["capacity_panel"]], ["zai"])


def feed_account(provider, used):
    """One overlay account entry, the shape overlay_build writes per provider."""
    return {
        "provider": provider,
        "observed_at": "2026-09-16T03:00:00+00:00",
        "status": "ok",
        "windows": [
            {"id": "five_hour", "used_percent": used, "resets_at": "2026-09-16T07:00:00+00:00"},
            {"id": "weekly", "used_percent": used / 2, "resets_at": "2026-09-19T00:00:00+00:00"},
        ],
    }


FEED_OVERLAY = {
    "accounts": [
        feed_account("zai", 12.5),
        feed_account("codex", 11.25),
        feed_account("opencode", 30.0),
        feed_account("command-code", 35.7),
        feed_account("clinepass", 12.5),
        feed_account("claude", 33.0),
    ],
    "attempts": [
        {
            "task": "packet-l4",
            "provider": "zai",
            "model": "glm-5.3-flash via zcode",
            "status": "running",
            "at": "2026-09-16T03:10:00+00:00",
        }
    ],
}


def serve_feed(overlay_path):
    """Handler.do_GET over a fake connection: (status line, headers, body) with no socket."""
    handler = feed.Handler.__new__(feed.Handler)
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /api/usage HTTP/1.1"
    handler.overlay_path = overlay_path
    handler.wfile = io.BytesIO()
    handler.do_GET()
    head, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    headers = dict(line.decode().split(": ", 1) for line in lines[1:])
    return lines[0], headers, body


class CapacityFeedTests(unittest.TestCase):
    def write_overlay(self, tmp, text):
        path = tmp.path / "overlay.json"
        path.write_text(text)
        return path

    def test_a_fixture_overlay_round_trips(self):
        tmp = self.enterContext(_TmpDir())
        path = self.write_overlay(tmp, json.dumps(FEED_OVERLAY))
        self.assertEqual(feed.snapshot(path), FEED_OVERLAY)

    def test_do_get_serves_the_overlay(self):
        tmp = self.enterContext(_TmpDir())
        path = self.write_overlay(tmp, json.dumps(FEED_OVERLAY))
        status, headers, body = serve_feed(path)
        self.assertTrue(status.endswith(b"200 OK"))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), FEED_OVERLAY)

    def test_a_rebuilt_overlay_is_served_without_a_restart(self):
        tmp = self.enterContext(_TmpDir())
        path = self.write_overlay(tmp, json.dumps(FEED_OVERLAY))
        serve_feed(path)
        rebuilt = {"accounts": [feed_account("zai", 90.0)], "attempts": []}
        path.write_text(json.dumps(rebuilt))
        _status, _headers, body = serve_feed(path)
        self.assertEqual(json.loads(body), rebuilt)

    def test_missing_garbage_and_old_shaped_overlays_degrade(self):
        tmp = self.enterContext(_TmpDir())
        empty = {"accounts": [], "attempts": []}
        self.assertEqual(feed.snapshot(tmp.path / "absent.json"), empty)
        garbage = self.write_overlay(tmp, "{not json")
        self.assertEqual(feed.snapshot(garbage), empty)
        bare = tmp.path / "bare.json"
        bare.write_text(json.dumps([{"provider": "zai", "status": "ok"}]))
        self.assertEqual(
            feed.snapshot(bare), {"accounts": [{"provider": "zai", "status": "ok"}], "attempts": []}
        )


class InstallTests(unittest.TestCase):
    def test_render_embeds_label_python_script_and_log_paths(self):
        text = installer.render(
            "com.inference-grid.capacity-loop",
            "/usr/bin/python3",
            "/x/capacity_loop.py",
            "/x/loop.log",
            3660,
        )
        self.assertIn("<string>com.inference-grid.capacity-loop</string>", text)
        self.assertIn("<string>/usr/bin/python3</string>", text)
        self.assertIn("<string>/x/capacity_loop.py</string>", text)
        self.assertIn("<key>KeepAlive</key>", text)
        self.assertIn("<key>RunAtLoad</key>", text)
        self.assertIn("<key>ExitTimeOut</key>", text)
        self.assertIn("<integer>3660</integer>", text)
        self.assertEqual(text.count("<string>/x/loop.log</string>"), 2)

    def test_render_turns_off_output_buffering(self):
        text = installer.render("com.inference-grid.tick-boards", "/usr/bin/python3", "/x/t.py", "/x/l.log", 3660)
        self.assertIn("<key>PYTHONUNBUFFERED</key>", text)
        self.assertIn("<string>1</string>", text)

    def test_exit_timeout_is_the_longest_wall_clock_plus_a_minute(self):
        lanes = {"lanes": {"a": {"wall_seconds": 600}, "b": {"wall_seconds": 3600}}}
        self.assertEqual(installer.exit_timeout(lanes), 3660)
        self.assertEqual(installer.exit_timeout({"lanes": {"a": {"wall_seconds": 900}}}), 960)
        self.assertEqual(installer.exit_timeout(), 3660)
        self.assertEqual(installer.exit_timeout({"lanes": {"a": {"wall_seconds": "900"}}}), 3660)

    def test_main_writes_the_plist_and_prints_the_bootstrap_command(self):
        tmp = self.enterContext(_TmpDir())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            installer.main(
                [
                    "--out-dir",
                    str(tmp.path),
                    "--python",
                    "/usr/bin/python3",
                    "--script",
                    "/x/capacity_loop.py",
                ]
            )
        plist = tmp.path / "com.inference-grid.capacity-loop.plist"
        self.assertTrue(plist.exists())
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()} {plist}", out.getvalue())
        self.assertIn("<string>/x/capacity_loop.py</string>", plist.read_text())

    def test_main_renders_a_kept_alive_plist_per_runtime_naming_the_operator_paths(self):
        runtimes = ("capacity-loop", "capacity-feed", "capacity-web", "tick-boards")
        self.assertEqual([name for name, _s, _l in installer.RUNTIMES], list(runtimes))
        tmp = self.enterContext(_TmpDir())
        flags = []
        for name in runtimes:
            flags += [
                f"--{name}-script",
                f"/operator/{name}.py",
                f"--{name}-log",
                f"/operator/{name}.log",
            ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            installer.main(["--out-dir", str(tmp.path), "--python", "/usr/bin/python3", *flags])
        for name in runtimes:
            plist = tmp.path / f"com.inference-grid.{name}.plist"
            data = plistlib.loads(plist.read_bytes())
            self.assertEqual(data["Label"], f"com.inference-grid.{name}")
            self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", f"/operator/{name}.py"])
            self.assertEqual(data["StandardOutPath"], f"/operator/{name}.log")
            self.assertEqual(data["StandardErrorPath"], f"/operator/{name}.log")
            self.assertIs(data["KeepAlive"], True)
            self.assertIs(data["RunAtLoad"], True)
            self.assertEqual(data["ProcessType"], "Interactive")
            self.assertEqual(data["ExitTimeOut"], installer.exit_timeout())
            self.assertIn(f"launchctl bootstrap gui/{os.getuid()} {plist}", out.getvalue())

    def test_lanes_file_sets_the_rendered_exit_timeout(self):
        tmp = self.enterContext(_TmpDir())
        lanes = tmp.path / "lanes.json"
        lanes.write_text(json.dumps({"lanes": {"a": {"wall_seconds": 600}}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            installer.main(
                [
                    "--out-dir",
                    str(tmp.path / "out"),
                    "--python",
                    "/usr/bin/python3",
                    "--lanes",
                    str(lanes),
                ]
            )
        plist = tmp.path / "out" / "com.inference-grid.tick-boards.plist"
        self.assertEqual(plistlib.loads(plist.read_bytes())["ExitTimeOut"], 660)

    def test_tick_boards_log_defaults_under_library_logs_not_the_capacity_state_dir(self):
        """02-A3: no grid logs under any product repo, and never /tmp. The other three
        runtimes still default beside the capacity state; only tick-boards moved."""
        tmp = self.enterContext(_TmpDir())
        installer.main(["--out-dir", str(tmp.path), "--python", "/usr/bin/python3"])
        tick = plistlib.loads((tmp.path / "com.inference-grid.tick-boards.plist").read_bytes())
        self.assertEqual(tick["StandardOutPath"], str(installer.DEFAULT_TICK_BOARDS_LOG))
        self.assertEqual(str(installer.DEFAULT_TICK_BOARDS_LOG),
                          str(Path.home() / "Library/Logs/inference-grid/tick-boards.log"))
        loop = plistlib.loads((tmp.path / "com.inference-grid.capacity-loop.plist").read_bytes())
        self.assertEqual(loop["StandardOutPath"], str(installer.DEFAULT_DIR / "capacity-loop.log"))

    def test_no_rendered_plist_carries_a_start_interval(self):
        tmp = self.enterContext(_TmpDir())
        installer.main(["--out-dir", str(tmp.path), "--python", "/usr/bin/python3"])
        for name, _s, _l in installer.RUNTIMES:
            plist = tmp.path / f"com.inference-grid.{name}.plist"
            self.assertNotIn("StartInterval", plistlib.loads(plist.read_bytes()))
            self.assertNotIn("StartInterval", plist.read_text())


class TickTimeoutTests(unittest.TestCase):
    def test_a_tick_past_the_timeout_is_logged_not_fatal(self):
        """A board whose packets outrun the timeout costs that board's pass, never the runtime."""
        import subprocess as sp
        from unittest import mock

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "board").mkdir()
            out = tmp / "tick-b.json"
            out.write_text(json.dumps({"board_dir": str(tmp / "board")}))
            config = {"tick_dir": str(tmp), "log_path": str(tmp / "log"), "tick_timeout": 1}
            calls = []

            def fake_run(argv, **kw):
                calls.append(kw.get("timeout"))
                raise sp.TimeoutExpired(argv, kw.get("timeout"))

            with mock.patch.object(tick_boards.subprocess, "run", fake_run):
                ready = tick_boards.default_tick("b", config)
            self.assertEqual(calls, [1])
            self.assertEqual(ready, 0)
            self.assertIn("exceeded 1s", (tmp / "log").read_text())

    def test_default_tick_creates_a_missing_log_directory(self):
        """02-A3 moved the default log under ~/Library/Logs/inference-grid/, which does
        not exist on a fresh install; the log open must not fail because of that."""
        import subprocess as sp
        from unittest import mock

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "board").mkdir()
            out = tmp / "tick-b.json"
            out.write_text(json.dumps({"board_dir": str(tmp / "board")}))
            nested_log = tmp / "nested" / "not" / "yet" / "created" / "tick-boards.log"
            config = {"tick_dir": str(tmp), "log_path": str(nested_log)}

            def fake_run(argv, **kw):
                return sp.CompletedProcess(argv, 0, stdout="[]", stderr="")

            with mock.patch.object(tick_boards.subprocess, "run", fake_run):
                tick_boards.default_tick("b", config)
            self.assertTrue(nested_log.exists())

    def test_the_default_timeout_outlasts_the_longest_admissible_packet(self):
        self.assertGreaterEqual(tick_boards.TICK_TIMEOUT, 8 * 3600)

    def test_a_failing_board_prepare_blocks_the_board_not_the_loop(self):
        clock = ThreadClock()  # each board loops on its own thread; the clock must too
        ticked = []
        out = io.StringIO()

        def prepare():
            raise RuntimeError("ledger refused the refresh")

        def tick(board):
            ticked.append(board)
            return 1

        with contextlib.redirect_stdout(out):
            tick_boards.run(
                ["a", "b"],
                deadline=7200,
                prepare=prepare,
                tick=tick,
                sleep=clock.sleep,
                clock=clock.clock,
            )
        # No board ever ticked, and the loop genuinely retries rather than giving up
        # after the first failed pass: 4 passes (clock 0, 1800, 3600, 5400) x 2 boards
        # = 8 failed prepares. (A deadline of 1000 with the default 1800s idle sleep
        # would end after exactly one pass and pass this test even if run() never
        # retried at all -- this reproduces and fixes that gap.)
        self.assertEqual(ticked, [])
        self.assertEqual(out.getvalue().count("board-prepare failed"), 8)
        self.assertIn("ledger refused the refresh", out.getvalue())
        # Both boards' own loops ran all four idle passes through to the deadline.
        self.assertEqual(sorted(clock.sleeps), [tick_boards.IDLE_SECONDS] * 8)

    def test_on_pass_never_fires_while_every_board_is_permanently_blocked(self):
        """A loop where every board's prepare() fails forever (stale accounts, a
        refused ledger) is alive but doing zero real work -- exactly the case the
        dead-man exists to catch. on_pass must not fire and mask it."""
        clock = ThreadClock()  # each board loops on its own thread; the clock must too
        passes = []

        tick_boards.run(
            ["a", "b"],
            deadline=3600,
            prepare=lambda: (_ for _ in ()).throw(RuntimeError("ledger refused")),
            tick=lambda board: 0,
            sleep=clock.sleep,
            clock=clock.clock,
            on_pass=lambda: passes.append(1),
        )
        self.assertEqual(passes, [])
        # Both boards' own loops genuinely retried to the deadline rather than one
        # racing the other's shared clock past it after a single failed prepare.
        self.assertEqual(sorted(clock.sleeps), [tick_boards.IDLE_SECONDS] * 4)

    def test_a_board_prepare_failure_is_per_board_and_per_pass(self):
        clock = ThreadClock()  # each board loops on its own thread; the clock must too
        ticked = []
        out = io.StringIO()
        first = threading.Lock()  # exactly one prepare call fails: the very first

        def prepare():
            if first.acquire(False):
                raise RuntimeError("one bad refresh")
            return None

        def tick(board):
            ticked.append(board)
            return 0

        with contextlib.redirect_stdout(out):
            tick_boards.run(
                ["a", "b"],
                deadline=7200,
                prepare=prepare,
                tick=tick,
                sleep=clock.sleep,
                clock=clock.clock,
            )
        self.assertEqual(out.getvalue().count("board-prepare failed"), 1)
        # Whichever board lost the first prepare, every later prepare still ticked:
        # 8 prepare calls (2 boards x 4 passes) minus the one that blocked.
        self.assertEqual(len(ticked), 7)

    def test_a_tick_exception_blocks_only_that_board_not_the_loop(self):
        """A malformed task file (KeyError/ValueError in count_ready) on one board must
        not take the whole runtime down with it -- reproduces the reviewer's finding
        that only prepare() was wrapped and a tick failure still killed every board."""
        clock = ThreadClock()  # each board loops on its own thread; the clock must too
        ticked = []
        out = io.StringIO()

        def tick(board):
            if board == "bad":
                raise KeyError("state")
            ticked.append(board)
            return 1

        with contextlib.redirect_stdout(out):
            ready = tick_boards.run(
                ["bad", "good"],
                deadline=1,
                prepare=lambda: None,
                tick=tick,
                sleep=clock.sleep,
                clock=clock.clock,
            )
        self.assertEqual(ticked, ["good"])
        self.assertEqual(ready, 1)
        self.assertIn("bad: tick failed; board blocked this pass", out.getvalue())


class HeartbeatTests(unittest.TestCase):
    def test_write_heartbeat_is_atomic_and_names_the_process_and_boards(self):
        tmp = self.enterContext(_TmpDir())
        path = tmp.path / "nested/heartbeat/tick-boards.json"
        tick_boards.write_heartbeat(path, ["alpha", "beta"], now=1789000000)
        payload = json.loads(path.read_text())
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["boards"], ["alpha", "beta"])
        self.assertTrue(payload["written_at"].endswith("Z"))
        self.assertEqual(list(tmp.path.glob("**/*.tmp")), [])  # no half-written leftovers
        tick_boards.write_heartbeat(path, ["alpha"], now=1789000060)
        self.assertEqual(json.loads(path.read_text())["boards"], ["alpha"])

    def test_write_heartbeat_never_raises_on_an_unwritable_path(self):
        self.assertIsNone(tick_boards.write_heartbeat("/proc/no/such/path.json", []))

    def test_heartbeat_path_reads_the_config_and_expands_the_operator_home(self):
        self.assertEqual(tick_boards.heartbeat_path({}), tick_boards.DEFAULT_HEARTBEAT)
        self.assertEqual(
            tick_boards.heartbeat_path({"heartbeat_path": "~/somewhere/hb.json"}),
            Path.home() / "somewhere/hb.json",
        )

    def test_start_heartbeat_keeps_the_file_fresh_until_stopped(self):
        tmp = self.enterContext(_TmpDir())
        path = tmp.path / "hb.json"
        stop, mark_pass = tick_boards.start_heartbeat({"heartbeat_path": str(path)}, interval=0.05)
        self.assertTrue(_await(lambda: path.exists()), "first beat within a second")
        first = path.read_text()
        self.assertTrue(_await(lambda: path.read_text() != first), "a later beat lands")
        stop.set()

    def test_beat_carries_last_pass_at_only_after_a_pass_completes(self):
        tmp = self.enterContext(_TmpDir())
        path = tmp.path / "hb.json"
        stop, mark_pass = tick_boards.start_heartbeat(
            {"heartbeat_path": str(path)}, interval=0.05, clock=lambda: 1789000000
        )
        self.assertTrue(_await(lambda: path.exists()))
        self.assertNotIn("last_pass_at", json.loads(path.read_text()))
        mark_pass()
        self.assertTrue(_await(lambda: "last_pass_at" in json.loads(path.read_text())))
        stop.set()

    def test_the_default_heartbeat_lives_beside_the_state_not_the_log(self):
        self.assertEqual(
            tick_boards.DEFAULT_HEARTBEAT,
            Path.home() / ".local/share/inference-grid/heartbeat/tick-boards.json",
        )

    def test_the_default_log_lives_under_library_logs_not_the_capacity_state_dir(self):
        self.assertEqual(
            tick_boards.DEFAULT_LOG_PATH,
            Path.home() / "Library/Logs/inference-grid/tick-boards.log",
        )


def _await(predicate, timeout=5.0):
    """True once predicate holds within the timeout; the heartbeat thread is asynchronous."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
