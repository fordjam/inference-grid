"""End-to-end refresh loop test: phone queues, Mac agent collects, uploads, completes."""

import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CAPACITY = str(Path(__file__).resolve().parent)
for _p in (str(Path(CAPACITY).parent), CAPACITY):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cloud_server  # noqa: E402
import refresh_agent  # noqa: E402
import upload  # noqa: E402,F401  (refresh_agent's `from upload import upload` must resolve)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = cloud_server.Store(Path(self.tmp.name) / "db")
        self.config = dict(
            host="dashboard.test",
            username="owner",
            salt="ab" * 16,
            password_hash=cloud_server.password_hash("test-password", "ab" * 16),
            session_key="s" * 64,
            upload_token="u" * 64,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), cloud_server.handler(self.config, self.store)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.config["host"] = "127.0.0.1:%d" % self.server.server_port

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(
            method, path, body=body, headers={"Host": self.config["host"], **(headers or {})}
        )
        r = conn.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        conn.close()
        return out

    def session(self):
        return {
            "Cookie": cloud_server.COOKIE + "=" + cloud_server.token(self.config["session_key"]),
            "Origin": "https://" + self.config["host"],
        }

    def queue(self):
        status, _, body = self.request("/api/refresh", "POST", "", self.session())
        self.assertEqual(status, 202, body)
        return json.loads(body)["id"]

    def outcome(self, rid):
        status, _, body = self.request("/api/refresh?id=" + rid, headers=self.session())
        self.assertEqual(status, 200, body)
        return json.loads(body)


class RefreshEndToEndTests(Base):
    """Full loop through cloud_server, refresh_agent and upload over 127.0.0.1 only."""

    def setUp(self):
        super().setUp()
        self.feed_body = json.dumps(
            {
                "accounts": [
                    {
                        "provider": "claude",
                        "status": "ok",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "windows": [{"id": "weekly", "used_percent": 55}],
                        "secret": "DO_NOT_SHARE",
                    }
                ]
            }
        ).encode()

        class Feed(BaseHTTPRequestHandler):
            body = self.feed_body

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(self.body)

        self.feed = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
        threading.Thread(target=self.feed.serve_forever, daemon=True).start()
        self.addCleanup(self._stop_feed)
        self.client = {
            "remote_url": "http://127.0.0.1:%d" % self.server.server_port,
            "upload_token": self.config["upload_token"],
            "local_feed": "http://127.0.0.1:%d/api/usage" % self.feed.server_port,
            "collect_timeout": 10,
            "poll_interval": 1,
        }

    def _stop_feed(self):
        self.feed.shutdown()
        self.feed.server_close()

    def collector(self, script):
        path = Path(self.tmp.name) / "collect.py"
        path.write_text(script)
        argv = [sys.executable, str(path)]
        self.client["collect_argv"] = argv
        return argv

    def test_full_refresh_loop_cooldown(self):
        self.collector(
            "import json;print(json.dumps({'providers':["
            "{'provider':'claude','status':'cooldown','next_eligible_at':'2026-09-12T15:00:00+00:00'},"
            "{'provider':'codex','status':'ok'}]}))"
        )
        rid = self.queue()
        self.assertEqual(refresh_agent.run(self.client, once=True), 0)
        done = self.outcome(rid)
        self.assertEqual(done["state"], "cooldown")
        self.assertIsNotNone(done["completed_at"])
        self.assertEqual(done["outcome"]["state"], "cooldown")
        self.assertEqual(done["outcome"]["reason"], "collected")
        self.assertTrue(done["outcome"]["collected"])
        providers = {p["provider"]: p for p in done["outcome"]["providers"]}
        self.assertEqual(set(providers), {"claude", "codex"})
        self.assertEqual(providers["claude"]["status"], "cooldown")
        self.assertEqual(providers["claude"]["next_eligible_at"], "2026-09-12T15:00:00+00:00")
        self.assertEqual(providers["codex"]["status"], "ok")
        snapshot = self.store.get()
        self.assertEqual(snapshot["accounts"][0]["provider"], "claude")
        self.assertEqual(snapshot["accounts"][0]["windows"][0]["used_percent"], 55)
        blob = json.dumps(snapshot) + json.dumps(done)
        self.assertNotIn("DO_NOT_SHARE", blob)
        for value in blob.replace("'", '"').split('"'):
            self.assertNotEqual(value, "secret")

    def test_collector_failure_marks_request_failed(self):
        self.collector('import sys;print("boom", file=sys.stderr);raise SystemExit(3)')
        before = self.store.get()
        rid = self.queue()
        self.assertEqual(refresh_agent.run(self.client, once=True), 0)
        done = self.outcome(rid)
        self.assertEqual(done["state"], "failed")
        self.assertIsNotNone(done["completed_at"])
        self.assertEqual(done["outcome"]["state"], "failed")
        self.assertEqual(done["outcome"]["reason"], "collector_error")
        self.assertFalse(done["outcome"]["collected"])
        self.assertEqual(done["outcome"]["providers"], [])
        self.assertEqual(self.store.get(), before)
        self.assertNotIn("DO_NOT_SHARE", json.dumps(done))


if __name__ == "__main__":
    unittest.main()
