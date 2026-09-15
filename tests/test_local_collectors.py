"""The versioned local deployment layer: collectors, the Claude poller, the scheduler loop,
the board tick loop and the installer, all against fixture payloads and injected network.

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
cline = load("collect_cline")
claude = load("refresh_claude")
overlay = load("overlay_build")
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

CLINE_BODY = {
    "data": {
        "limits": [
            {"type": "five_hour", "percentUsed": 12.5, "resetsAt": "2026-09-15T07:00:00Z"},
            {"type": "weekly", "percentUsed": 44.0, "resetsAt": "2026-09-19T00:00:00Z"},
            {"type": "monthly", "percentUsed": 9.0, "resetsAt": None},
        ]
    }
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
        self.assertIn(obs["status"], ("ok", "unknown"))
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
        obs = zai.parse({"code": 401, "success": False})
        self.assertEqual(obs["status"], "unknown")
        self.assertEqual(obs["error"], "ValueError")
        self.assertEqual(obs["windows"], [])

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
        obs = zai.observe(raise_(RuntimeError()), config)
        self.assertEqual((obs["status"], obs["error"]), ("unknown", "RuntimeError"))
        obs = zai.observe(
            raise_(RuntimeError()), {"zai_credential_path": str(tmp.path / "missing.json")}
        )
        self.assertEqual((obs["status"], obs["error"]), ("unknown", "FileNotFoundError"))

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

        obs = goat.observe(org_fetch, config, status_cmd=ok_status)
        self.assertEqual((obs["status"], obs["error"]), ("unknown", "ValueError"))

        def closed_status():
            return {"authenticated": False}

        obs = goat.observe(org_fetch, config, status_cmd=closed_status)
        self.assertEqual(obs["status"], "unknown")

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


class ClineCollectorTests(ObservationShapeMixin, unittest.TestCase):
    def test_parse_reads_all_three_windows(self):
        obs = cline.parse(CLINE_BODY)
        self.assertEqual(obs["status"], "ok")
        self.assert_observation_shape(obs, "clinepass")
        windows = {w["id"]: w for w in obs["windows"]}
        self.assertEqual(set(windows), {"five_hour", "weekly", "monthly"})
        self.assertEqual(windows["five_hour"]["used_percent"], 12.5)
        self.assertEqual(windows["five_hour"]["resets_at"], "2026-09-15T07:00:00+00:00")
        self.assertIsNone(windows["monthly"]["resets_at"])

    def test_parse_is_unknown_until_every_window_answered(self):
        body = {"data": {"limits": [dict(CLINE_BODY["data"]["limits"][0])]}}
        obs = cline.parse(body)
        self.assertEqual(obs["status"], "unknown")
        self.assertEqual([w["id"] for w in obs["windows"]], ["five_hour"])

    def test_read_key_from_the_configured_env_file(self):
        tmp = self.enterContext(_TmpDir())
        env = tmp.path / ".env.local"
        env.write_text('OTHER=1\nCLINE_API_KEY="fake-key"\n')
        self.assertEqual(cline.read_key(env), "fake-key")
        self.assertIsNone(cline.read_key(tmp.path / "missing"))

    def test_observe_sends_the_bearer_key(self):
        tmp = self.enterContext(_TmpDir())
        env = tmp.path / ".env.local"
        env.write_text("CLINE_API_KEY=fake-key\n")
        seen = {}

        def fetch(url, headers, timeout):
            seen["url"], seen["headers"] = url, headers
            return CLINE_BODY

        obs = cline.observe(fetch, {"cline_credential_path": str(env)})
        self.assertEqual(obs["status"], "ok")
        self.assertEqual(seen["url"], cline.API_URL)
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
        self.assertEqual(a["status"], "unknown")
        self.assertEqual(a["windows"], prior["windows"])
        self.assertTrue(a["stale"])
        self.assertEqual(delay, 900)

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


class TickBoardsTests(unittest.TestCase):
    def test_prepare_runs_before_every_board(self):
        clock = FakeClock()
        events = []

        def prepare():
            events.append("prepare")

        def tick(board):
            events.append("tick:" + board)
            return 0

        tick_boards.run(
            ["a", "b"],
            deadline=7200,
            prepare=prepare,
            tick=tick,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        self.assertEqual(events, ["prepare", "tick:a", "prepare", "tick:b"] * 4)
        self.assertEqual(clock.now, 7200)

    def test_ready_count_chooses_the_sleep_interval(self):
        sleeps = []
        clock = FakeClock()

        def nap(seconds):
            sleeps.append(seconds)
            clock.sleep(seconds)

        tick_boards.run(
            ["a"],
            deadline=1000,
            prepare=lambda: None,
            tick=lambda board: 2,
            sleep=nap,
            clock=clock.clock,
        )
        self.assertEqual(sleeps, [tick_boards.BUSY_SECONDS] * 4)
        sleeps.clear()
        clock = FakeClock()
        tick_boards.run(
            ["a"],
            deadline=1000,
            prepare=lambda: None,
            tick=lambda board: 0,
            sleep=nap,
            clock=clock.clock,
        )
        self.assertEqual(sleeps, [tick_boards.IDLE_SECONDS])

    def test_count_ready_reads_the_board_directory(self):
        tmp = self.enterContext(_TmpDir())
        (tmp.path / "t1.json").write_text(json.dumps({"state": "ready"}))
        (tmp.path / "t2.json").write_text(json.dumps({"state": "running"}))
        self.assertEqual(tick_boards.count_ready(str(tmp.path)), 1)
        self.assertEqual(tick_boards.count_ready(None), 0)

    def test_a_signal_lets_the_tick_finish_then_ends_the_loop(self):
        tmp = self.enterContext(_TmpDir())
        state = tmp.path / "tick-boards.draining"
        drain = tick_boards.Drain(state)
        with_drain_handlers(self, drain)
        events = []
        started = threading.Event()
        finish = threading.Event()

        def tick(board):
            events.append("tick:" + board)
            started.set()
            self.assertTrue(finish.wait(5))
            return 0

        def send():
            self.assertTrue(started.wait(5))
            os.kill(os.getpid(), signal.SIGTERM)
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
        self.assertEqual(events, ["tick:a"])
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

    def test_every_pass_runs_the_trigger_before_the_boards(self):
        clock = FakeClock()
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
        self.assertEqual(events.count("calibrate"), 4)
        self.assertEqual(events[:3], ["calibrate", "prepare", "tick:a"])

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
        (tmp.path / "cline-observation.json").write_text(json.dumps(cline.parse(CLINE_BODY)))
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
            ["zai", "codex", "command-code", "clinepass"],
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

    def test_no_rendered_plist_carries_a_start_interval(self):
        tmp = self.enterContext(_TmpDir())
        installer.main(["--out-dir", str(tmp.path), "--python", "/usr/bin/python3"])
        for name, _script, _log in installer.RUNTIMES:
            plist = tmp.path / f"com.inference-grid.{name}.plist"
            self.assertNotIn("StartInterval", plistlib.loads(plist.read_bytes()))
            self.assertNotIn("StartInterval", plist.read_text())


if __name__ == "__main__":
    unittest.main()


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

    def test_the_default_timeout_outlasts_the_longest_admissible_packet(self):
        self.assertGreaterEqual(tick_boards.TICK_TIMEOUT, 8 * 3600)
