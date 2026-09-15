import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer
from cloud_server import (
    Store,
    handler,
    password_hash,
    token,
    valid_token,
    clean_snapshot,
    clean_outcome,
    COOKIE,
    QUEUED_TTL,
    COLLECTING_TTL,
)
import refresh_agent


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db"
        self.store = Store(self.path)
        self.config = dict(
            host="dashboard.test",
            username="owner",
            salt="ab" * 16,
            password_hash=password_hash("test-password", "ab" * 16),
            session_key="s" * 64,
            upload_token="u" * 64,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.config, self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(method, path, body=body, headers={"Host": "dashboard.test", **(headers or {})})
        r = conn.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        conn.close()
        return out

    def sample(self):
        from datetime import datetime, timezone

        return dict(
            captured_at=datetime.now(timezone.utc).isoformat(),
            accounts=[
                dict(
                    provider="claude",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    status="ok",
                    windows=[dict(id="weekly", used_percent=10)],
                    secret="DO_NOT_SHARE",
                )
            ],
            attempts=[{"task": "private prompt"}],
            credentials="DO_NOT_SHARE",
        )


class Tests(Base):
    def test_unauthenticated_cannot_read(self):
        self.assertEqual(self.request("/api/usage")[0], 401)
        self.assertEqual(self.request("/")[0], 303)
        self.assertEqual(self.request("/index.html")[0], 303)

    def test_host_rejected(self):
        self.assertEqual(self.request("/api/usage", headers={"Host": "evil.test"})[0], 403)

    def test_login_origin_and_password(self):
        form = urlencode(dict(username="owner", password="test-password"))
        self.assertEqual(self.request("/login", "POST", form)[0], 403)
        self.assertEqual(
            self.request(
                "/login",
                "POST",
                urlencode(dict(username="owner", password="wrong")),
                {"Origin": "https://dashboard.test"},
            )[0],
            401,
        )
        status, headers, _ = self.request(
            "/login", "POST", form, {"Origin": "https://dashboard.test"}
        )
        self.assertEqual(status, 303)
        for attr in ["Secure", "HttpOnly", "SameSite=Strict"]:
            self.assertIn(attr, headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.request("/api/usage", headers={"Cookie": cookie})[0], 200)

    def test_browser_login_without_origin(self):
        import re

        status, headers, page = self.request("/login")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        csrf = re.search(rb'name="csrf" value="([^"]+)"', page).group(1).decode()
        cookie = headers["Set-Cookie"].split(";")[0]
        form = urlencode(dict(username="owner", password="test-password", csrf=csrf))
        for origin in (None, "null"):
            h = {"Cookie": cookie}
            if origin is not None:
                h["Origin"] = origin
            self.assertEqual(self.request("/login", "POST", form, h)[0], 303)
        self.assertEqual(
            self.request("/login", "POST", form, {"Cookie": cookie, "Origin": "https://evil.test"})[
                0
            ],
            403,
        )
        self.assertEqual(self.request("/login", "POST", form, {"Origin": "null"})[0], 403)
        self.assertEqual(
            self.request("/login", "POST", form.replace(csrf, "invalid"), {"Cookie": cookie})[0],
            403,
        )
        self.assertEqual(self.request("/logout", "POST", "", {"Cookie": cookie})[0], 403)

    def test_upload_token_separate_from_login(self):
        payload = json.dumps(self.sample())
        self.assertEqual(self.request("/api/snapshot", "POST", payload)[0], 401)
        self.assertEqual(
            self.request(
                "/api/snapshot",
                "POST",
                payload,
                {"Cookie": COOKIE + "=" + token(self.config["session_key"])},
            )[0],
            401,
        )
        self.assertEqual(
            self.request(
                "/api/snapshot",
                "POST",
                payload,
                {"Authorization": "Bearer " + self.config["upload_token"]},
            )[0],
            200,
        )
        self.assertEqual(
            self.request(
                "/api/usage", headers={"Authorization": "Bearer " + self.config["upload_token"]}
            )[0],
            401,
        )
        self.assertNotIn("DO_NOT_SHARE", json.dumps(self.store.get()))
        self.assertNotIn("private prompt", json.dumps(self.store.get()))

    def test_durability_and_old_upload_refusal(self):
        data, captured = clean_snapshot(self.sample())
        self.assertTrue(self.store.put(data, captured))
        self.assertFalse(self.store.put(data, captured - 1))
        self.assertEqual(Store(self.path).get()["captured_at"], data["captured_at"])

    def test_expiry_tamper_and_key_rotation(self):
        v = token("one", now=100)
        self.assertTrue(valid_token(v, "one", now=101))
        self.assertFalse(valid_token(v, "two", now=101))
        self.assertFalse(valid_token(v + "x", "one", now=101))
        self.assertFalse(valid_token(v, "one", now=100 + 31 * 86400))

    def test_healthz_reports_the_deploy_commit_as_json(self):
        # The watcher compares this commit with the deploy bundle's newest commit.
        status, headers, body = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        doc = json.loads(body)
        self.assertTrue(doc["ok"])
        self.assertIsNone(doc["commit"])
        os.environ["GRID_DEPLOY_COMMIT"] = "abc123"
        try:
            self.assertEqual(json.loads(self.request("/healthz")[2])["commit"], "abc123")
        finally:
            del os.environ["GRID_DEPLOY_COMMIT"]
        os.environ["RAILWAY_GIT_COMMIT_SHA"] = "rail-1"
        os.environ["GRID_DEPLOY_COMMIT"] = "abc123"
        try:
            self.assertEqual(json.loads(self.request("/healthz")[2])["commit"], "rail-1")
        finally:
            del os.environ["RAILWAY_GIT_COMMIT_SHA"]
            del os.environ["GRID_DEPLOY_COMMIT"]

    def test_oversize_rejected_before_body_read(self):
        self.assertEqual(
            self.request(
                "/api/snapshot",
                "POST",
                "{}",
                {
                    "Authorization": "Bearer " + self.config["upload_token"],
                    "Content-Length": "200000",
                },
            )[0],
            400,
        )

    def test_future_or_naive_observation_rejected(self):
        s = self.sample()
        s["accounts"][0]["observed_at"] = "2999-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            clean_snapshot(s)
        s = self.sample()
        s["captured_at"] = "2026-09-12T12:00:00"
        with self.assertRaises(ValueError):
            clean_snapshot(s)

    def test_scorecard_upload_is_sanitized_to_evidence_keys(self):
        row = dict(
            family="glm",
            model="glm-5.3-flash",
            category="pure_function",
            attempts=7,
            completed=6,
            accepted=6,
            repairs=0,
            usage={"input_tokens": 10, "output_tokens": 5},
            task_names=["private task"],
            prompt="DO_NOT_SHARE",
        )
        s = self.sample()
        s["scorecard"] = [row, {"family": "glm"}, "junk"]
        data, _ = clean_snapshot(s)
        self.assertEqual(
            data["scorecard"],
            [
                dict(
                    family="glm",
                    model="glm-5.3-flash",
                    category="pure_function",
                    attempts=7,
                    completed=6,
                    accepted=6,
                    repairs=0,
                    usage={"input_tokens": 10, "output_tokens": 5},
                )
            ],
        )
        s["scorecard"] = "not-a-list"
        with self.assertRaises(ValueError):
            clean_snapshot(s)
        payload = json.dumps(self.sample() | {"scorecard": [dict(row, notes="DO_NOT_SHARE")]})
        status, _, _ = self.request(
            "/api/snapshot",
            "POST",
            payload,
            {"Authorization": "Bearer " + self.config["upload_token"]},
        )
        self.assertEqual(status, 200)
        stored = json.dumps(self.store.get())
        self.assertIn("glm-5.3-flash", stored)
        self.assertNotIn("DO_NOT_SHARE", stored)
        self.assertNotIn("private task", stored)
        self.assertNotIn("notes", stored)

    def test_operator_and_accepted_work_upload_sanitized(self):
        # Needs-you rows and the weekly metric: fixed shapes, unknown keys rejected at the
        # cloud boundary (the overlay-side cleaner strips instead; here nothing slips in).
        row = {
            "kind": "blocked_task",
            "id": "t1",
            "reason": "operator decision",
            "since": "2026-09-15T00:00:00+00:00",
        }
        work = {"week": "2026-W37", "account": "zai", "accepted": 1, "attempts": 2}
        s = self.sample()
        s["operator"] = [row]
        s["accepted_work"] = [work]
        data, _ = clean_snapshot(s)
        self.assertEqual(data["operator"], [row])
        self.assertEqual(data["accepted_work"], [work])

    def test_operator_rows_rejected(self):
        s = self.sample()
        for bad in [
            "not-a-list",
            [{}] * 51,
            [{"kind": "blocked_task", "id": "t1", "reason": "r", "since": "x", "extra": 1}],
            [{"kind": " ", "id": "t1", "reason": "r", "since": "x"}],
            [{"kind": "blocked_task", "id": "", "reason": "r", "since": "x"}],
            [{"kind": "blocked_task", "id": "t1", "reason": "r" * 201, "since": "x"}],
            [{"kind": "blocked_task", "id": "t1"}],
        ]:
            with self.assertRaises(ValueError):
                clean_snapshot(s | {"operator": bad})

    def test_accepted_work_rejected(self):
        s = self.sample()
        for bad in [
            "junk",
            [{}] * 51,
            [{"week": "2026-W37", "account": "zai", "accepted": True, "attempts": 2}],
            [{"week": "2026-W37", "account": "zai", "accepted": -1, "attempts": 2}],
            [{"week": "2026-W37", "account": "zai", "accepted": 1, "attempts": 2, "extra": "x"}],
            [{"week": "2026-W37", "account": "zai", "accepted": 1}],
            [{"week": 5, "account": "zai", "accepted": 1, "attempts": 2}],
        ]:
            with self.assertRaises(ValueError):
                clean_snapshot(s | {"accepted_work": bad})

    # --- Observable refresh requests (CLOUD-03) ---
    def session(self):
        return {
            "Cookie": COOKIE + "=" + token(self.config["session_key"]),
            "Origin": "https://dashboard.test",
        }

    def bearer(self):
        return {
            "Authorization": "Bearer " + self.config["upload_token"],
            "Content-Type": "application/json",
        }

    def test_refresh_requires_session_and_origin(self):
        self.assertEqual(self.request("/api/refresh", "POST", "")[0], 401)
        self.assertEqual(
            self.request(
                "/api/refresh",
                "POST",
                "",
                {"Cookie": COOKIE + "=" + token(self.config["session_key"])},
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "/api/refresh", "POST", "", {**self.session(), "Origin": "https://evil.test"}
            )[0],
            403,
        )
        self.assertEqual(self.request("/api/refresh", "POST", "", self.bearer())[0], 401)
        self.assertEqual(self.request("/api/refresh")[0], 401)
        self.assertEqual(self.request("/api/refresh", headers=self.session())[0], 404)

    def test_refresh_lifecycle_coalesces_and_reports(self):
        status, _, body = self.request("/api/refresh", "POST", "", self.session())
        self.assertEqual(status, 202)
        first = json.loads(body)
        self.assertEqual(first["state"], "queued")
        status, _, body = self.request("/api/refresh", "POST", "", self.session())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], first["id"])
        # The dashboard session cannot claim work; the upload token cannot queue it.
        self.assertEqual(self.request("/api/refresh/claim", "POST", "", self.session())[0], 401)
        status, _, body = self.request("/api/refresh/claim", "POST", "", self.bearer())
        self.assertEqual(status, 200)
        claimed = json.loads(body)
        self.assertEqual(claimed["id"], first["id"])
        self.assertEqual(claimed["state"], "collecting")
        self.assertEqual(self.request("/api/refresh/claim", "POST", "", self.bearer())[0], 204)
        status, _, body = self.request("/api/refresh?id=" + first["id"], headers=self.session())
        self.assertEqual(json.loads(body)["state"], "collecting")
        # A second tap while collecting joins the open request rather than queueing another.
        self.assertEqual(
            json.loads(self.request("/api/refresh", "POST", "", self.session())[2])["id"],
            first["id"],
        )
        outcome = {
            "state": "cooldown",
            "reason": "collected",
            "collected": True,
            "providers": [
                {
                    "provider": "claude",
                    "status": "cooldown",
                    "next_eligible_at": "2026-09-12T15:00:00+00:00",
                },
                {
                    "provider": "codex",
                    "status": "ok",
                    "next_eligible_at": None,
                    "raw_body": "DO_NOT_SHARE",
                },
            ],
        }
        self.assertEqual(
            self.request(
                "/api/refresh/complete",
                "POST",
                json.dumps({"id": first["id"], "outcome": outcome}),
                self.session(),
            )[0],
            401,
        )
        self.assertEqual(
            self.request(
                "/api/refresh/complete",
                "POST",
                json.dumps({"id": first["id"], "outcome": {**outcome, "reason": "made up"}}),
                self.bearer(),
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "/api/refresh/complete",
                "POST",
                json.dumps({"id": first["id"], "outcome": outcome}),
                self.bearer(),
            )[0],
            200,
        )
        self.assertEqual(
            self.request(
                "/api/refresh/complete",
                "POST",
                json.dumps({"id": first["id"], "outcome": outcome}),
                self.bearer(),
            )[0],
            409,
        )
        status, _, body = self.request("/api/refresh", headers=self.session())
        done = json.loads(body)
        self.assertEqual(done["state"], "cooldown")
        self.assertIsNotNone(done["completed_at"])
        self.assertNotIn("DO_NOT_SHARE", body.decode())
        self.assertEqual(
            done["outcome"]["providers"][0]["next_eligible_at"], "2026-09-12T15:00:00+00:00"
        )
        status, _, body = self.request("/api/refresh", "POST", "", self.session())
        self.assertEqual(status, 202)
        self.assertNotEqual(json.loads(body)["id"], first["id"])
        self.assertEqual(self.request("/api/refresh?id=zz", headers=self.session())[0], 400)

    def test_refresh_expiry_marks_offline_mac_and_stuck_collection(self):
        row, created = self.store.request_refresh(now=1000)
        self.assertTrue(created)
        self.assertEqual(
            self.store.get_refresh(row["id"], now=1000 + QUEUED_TTL - 1)["state"], "queued"
        )
        expired = self.store.get_refresh(row["id"], now=1000 + QUEUED_TTL + 1)
        self.assertEqual(
            (expired["state"], expired["outcome"]["reason"]), ("failed", "mac_not_reporting")
        )
        self.assertIsNone(self.store.claim_refresh(now=1000 + QUEUED_TTL + 1))
        row, _ = self.store.request_refresh(now=2000)
        claimed = self.store.claim_refresh(now=2001)
        self.assertEqual(claimed["id"], row["id"])
        self.assertIsNone(self.store.claim_refresh(now=2002))
        stuck = self.store.get_refresh(row["id"], now=2001 + COLLECTING_TTL + 1)
        self.assertEqual(
            (stuck["state"], stuck["outcome"]["reason"]), ("failed", "collection_timeout")
        )
        self.assertFalse(
            self.store.complete_refresh(
                row["id"],
                clean_outcome(
                    {
                        "state": "completed",
                        "reason": "collected",
                        "collected": True,
                        "providers": [],
                    }
                ),
                now=2400,
            )
        )
        self.assertEqual(Store(self.path).get_refresh(row["id"], now=2500)["state"], "failed")

    def test_outcome_validation(self):
        good = {"state": "completed", "reason": "collected", "collected": True, "providers": []}
        self.assertEqual(clean_outcome(good)["providers"], [])
        for bad in [
            {**good, "state": "queued"},
            {**good, "collected": "yes"},
            {**good, "providers": [{"provider": "x", "status": "busy"}]},
            {
                **good,
                "providers": [
                    {"provider": "x", "status": "ok", "next_eligible_at": "2026-09-12T15:00:00"}
                ],
            },
            {**good, "providers": [{"provider": "x", "status": "ok"}] * 11},
            "text",
        ]:
            with self.assertRaises(ValueError):
                clean_outcome(bad)


class AgentTests(Base):
    """End-to-end: phone queues a request, the Mac agent claims, collects, uploads and completes it."""

    def setUp(self):
        super().setUp()
        self.config["host"] = "127.0.0.1:%d" % self.server.server_port
        from http.server import BaseHTTPRequestHandler

        feed = json.dumps(
            {
                "accounts": [
                    {
                        "provider": "claude",
                        "status": "ok",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "windows": [{"id": "weekly", "used_percent": 40}],
                        "secret": "DO_NOT_SHARE",
                    }
                ]
            }
        ).encode()

        class Feed(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(feed)

        self.feed = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
        threading.Thread(target=self.feed.serve_forever, daemon=True).start()
        self.client = {
            "remote_url": "http://127.0.0.1:%d" % self.server.server_port,
            "upload_token": self.config["upload_token"],
            "local_feed": "http://127.0.0.1:%d/api/usage" % self.feed.server_port,
            "collect_timeout": 5,
            "poll_interval": 1,
        }

    def tearDown(self):
        self.feed.shutdown()
        self.feed.server_close()
        super().tearDown()

    def request(self, path, method="GET", body=None, headers=None):
        return super().request(path, method, body, {"Host": self.config["host"], **(headers or {})})

    def session(self):
        return {
            "Cookie": COOKIE + "=" + token(self.config["session_key"]),
            "Origin": "https://" + self.config["host"],
        }

    def collector(self, script):
        path = Path(self.tmp.name) / "collect.py"
        path.write_text(script)
        return [sys.executable, str(path)]

    def queue(self):
        return json.loads(self.request("/api/refresh", "POST", "", self.session())[2])["id"]

    def outcome(self, rid):
        return json.loads(self.request("/api/refresh?id=" + rid, headers=self.session())[2])

    def test_idle_poll_and_snapshot_only(self):
        self.assertEqual(refresh_agent.run(self.client, once=True), 0)
        rid = self.queue()
        self.assertEqual(refresh_agent.run(self.client, once=True), 0)
        done = self.outcome(rid)
        self.assertEqual(
            (done["state"], done["outcome"]["reason"], done["outcome"]["collected"]),
            ("completed", "snapshot_only", False),
        )
        self.assertEqual(self.store.get()["accounts"][0]["windows"][0]["used_percent"], 40)
        self.assertNotIn("DO_NOT_SHARE", json.dumps(self.store.get()))

    def test_collector_outcomes(self):
        cases = [
            (
                "import json;print(json.dumps({'providers':[{'provider':'claude','status':'cooldown','next_eligible_at':'2026-09-12T15:00:00+00:00'},{'provider':'codex','status':'ok'}]}))",
                ("cooldown", "collected", True),
            ),
            (
                "import json;print(json.dumps({'providers':[{'provider':'claude','status':'ok'}]}))",
                ("completed", "collected", True),
            ),
            ("raise SystemExit(3)", ("failed", "collector_error", False)),
            ("print('not json: token=DO_NOT_SHARE')", ("failed", "collector_error", False)),
            ("import time;time.sleep(30)", ("failed", "collector_timeout", False)),
        ]
        for script, expected in cases:
            self.client["collect_argv"] = self.collector(script)
            self.client["collect_timeout"] = 1
            rid = self.queue()
            self.assertEqual(refresh_agent.run(self.client, once=True), 0)
            done = self.outcome(rid)
            self.assertEqual(
                (done["state"], done["outcome"]["reason"], done["outcome"]["collected"]),
                expected,
                script,
            )
            self.assertNotIn("DO_NOT_SHARE", json.dumps(done))

    def test_upload_failure_is_reported(self):
        self.client["local_feed"] = "http://127.0.0.1:1/nothing"
        rid = self.queue()
        self.assertEqual(refresh_agent.run(self.client, once=True), 0)
        self.assertEqual(self.outcome(rid)["outcome"]["reason"], "upload_failed")

    def test_dashboard_unreachable_returns_error_without_crash(self):
        self.client["remote_url"] = "http://127.0.0.1:1"
        self.assertEqual(refresh_agent.run(self.client, once=True), 1)

    def test_config_validation(self):
        path = Path(self.tmp.name) / "client.json"
        for bad in [
            {**self.client, "collect_argv": "python"},
            {**self.client, "collect_argv": ["python"]},
            {**self.client, "collect_timeout": 0},
            {**self.client, "poll_interval": 61},
            {k: v for k, v in self.client.items() if k != "upload_token"},
        ]:
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                refresh_agent.load_config(path)
        path.write_text(
            json.dumps({**self.client, "collect_argv": [sys.executable, "-c", "print(1)"]})
        )
        self.assertEqual(refresh_agent.load_config(path)["collect_timeout"], 5)


if __name__ == "__main__":
    unittest.main()
