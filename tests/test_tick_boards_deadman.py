"""02-A6: the tick-boards dead-man. Never sends real email or touches launchctl."""
import importlib.util
import io
import json
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "deployments/local/tick_boards_deadman.py"
spec = importlib.util.spec_from_file_location("tick_boards_deadman", MODULE)
deadman = importlib.util.module_from_spec(spec)
sys.modules["tick_boards_deadman"] = deadman
spec.loader.exec_module(deadman)


def stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class EvaluateTests(unittest.TestCase):
    def test_missing_heartbeat_alarms(self):
        v = deadman.evaluate("/no/such/path.json", now=1_000_000.0)
        self.assertEqual(v["state"], "alarm")
        self.assertIn("missing", v["reason"])

    def test_corrupt_json_alarms(self):
        with self._tmp_file("{not json") as path:
            v = deadman.evaluate(path, now=1_000_000.0)
            self.assertEqual(v["state"], "alarm")
            self.assertIn("unreadable", v["reason"])

    def test_fresh_last_pass_at_is_ok(self):
        now = 1_000_000.0
        body = json.dumps({"written_at": stamp(now - 3600), "last_pass_at": stamp(now - 60)})
        with self._tmp_file(body) as path:
            v = deadman.evaluate(path, now=now)
            self.assertEqual(v["state"], "ok")
            self.assertIn("last_pass_at", v["reason"])

    def test_stale_last_pass_at_alarms_even_with_a_fresh_written_at(self):
        """The wedged-pass case 02-A1's review found: written_at alone stays fresh
        forever via the daemon beat, so the dead-man must key off last_pass_at."""
        now = 1_000_000.0
        body = json.dumps({
            "written_at": stamp(now - 30),          # daemon beat 30s ago: looks alive
            "last_pass_at": stamp(now - 3600),       # but no pass has completed in an hour
        })
        with self._tmp_file(body) as path:
            v = deadman.evaluate(path, now=now)
            self.assertEqual(v["state"], "alarm")
            self.assertIn("last_pass_at", v["reason"])

    def test_falls_back_to_written_at_when_last_pass_at_absent(self):
        now = 1_000_000.0
        body = json.dumps({"written_at": stamp(now - 60)})
        with self._tmp_file(body) as path:
            v = deadman.evaluate(path, now=now)
            self.assertEqual(v["state"], "ok")
            self.assertIn("written_at", v["reason"])

    def test_neither_timestamp_alarms(self):
        with self._tmp_file(json.dumps({"pid": 1})) as path:
            v = deadman.evaluate(path, now=1_000_000.0)
            self.assertEqual(v["state"], "alarm")

    def test_exactly_at_threshold_is_still_ok_one_second_over_alarms(self):
        now = 1_000_000.0
        at_threshold = json.dumps({"last_pass_at": stamp(now - 1800)})
        with self._tmp_file(at_threshold) as path:
            self.assertEqual(deadman.evaluate(path, now=now)["state"], "ok")
        over_threshold = json.dumps({"last_pass_at": stamp(now - 1801)})
        with self._tmp_file(over_threshold) as path:
            self.assertEqual(deadman.evaluate(path, now=now)["state"], "alarm")

    def test_custom_threshold_is_honoured(self):
        now = 1_000_000.0
        body = json.dumps({"last_pass_at": stamp(now - 700)})
        with self._tmp_file(body) as path:
            self.assertEqual(
                deadman.evaluate(path, now=now, threshold_seconds=600)["state"], "alarm"
            )
            self.assertEqual(
                deadman.evaluate(path, now=now, threshold_seconds=900)["state"], "ok"
            )

    def _tmp_file(self, content):
        import tempfile

        class _Ctx:
            def __enter__(inner):
                fd, inner.path = tempfile.mkstemp(suffix=".json")
                import os

                with os.fdopen(fd, "w") as f:
                    f.write(content)
                return inner.path

            def __exit__(inner, *a):
                import os

                os.unlink(inner.path)

        return _Ctx()


class SendEmailTests(unittest.TestCase):
    def test_missing_password_file_raises_without_touching_smtp(self):
        with mock.patch.object(deadman, "APP_PASSWORD_FILE", Path("/no/such/password/file")):
            with mock.patch("smtplib.SMTP_SSL") as smtp:
                with self.assertRaises(RuntimeError):
                    deadman.send_email("subject", "body")
                smtp.assert_not_called()

    def test_sends_via_smtp_ssl_with_the_app_password(self):
        tmp = self._tmp_password_file("fake-app-password")
        with mock.patch.object(deadman, "APP_PASSWORD_FILE", tmp):
            with mock.patch("smtplib.SMTP_SSL") as smtp:
                server = smtp.return_value.__enter__.return_value
                deadman.send_email("subject", "body")
                server.login.assert_called_once_with(deadman.GMAIL_USER, "fake-app-password")
                self.assertEqual(server.sendmail.call_count, 1)
                args = server.sendmail.call_args[0]
                self.assertEqual(args[0], deadman.GMAIL_USER)
                self.assertIn("subject", args[2])
                # The password itself must never appear in the composed message.
                self.assertNotIn("fake-app-password", args[2])

    def _tmp_password_file(self, content):
        import tempfile

        fd, path = tempfile.mkstemp()
        import os

        with os.fdopen(fd, "w") as f:
            f.write(content)
        self.addCleanup(lambda: os.unlink(path))
        return Path(path)


class RenderPlistTests(unittest.TestCase):
    def test_render_plist_is_a_start_interval_job_not_keepalive(self):
        text = deadman.render_plist("/usr/bin/python3", "/x/deadman.py", "/x/hb.json", "/x/log")
        self.assertIn("<key>StartInterval</key>", text)
        self.assertIn(f"<integer>{deadman.CHECK_INTERVAL_SECONDS}</integer>", text)
        self.assertNotIn("KeepAlive", text)
        self.assertIn("<false/>", text)  # RunAtLoad

    def test_main_render_plist_never_calls_launchctl(self):
        with tempfile_dir() as out_dir:
            out = io.StringIO()
            with redirect_stdout(out):
                with mock.patch("subprocess.run") as run:
                    code = deadman.main(["--render-plist", str(out_dir)])
                    run.assert_not_called()
            self.assertEqual(code, 0)
            plist = out_dir / "com.inference-grid.tick-boards-deadman.plist"
            self.assertTrue(plist.exists())
            self.assertIn("launchctl bootstrap", out.getvalue())
            self.assertIn(str(plist), out.getvalue())


def tempfile_dir():
    import tempfile

    class _Ctx:
        def __enter__(inner):
            inner.d = tempfile.mkdtemp()
            return Path(inner.d)

        def __exit__(inner, *a):
            import shutil

            shutil.rmtree(inner.d, ignore_errors=True)

    return _Ctx()


class MainAlertTests(unittest.TestCase):
    def test_main_sends_email_only_on_alert(self):
        with mock.patch.object(deadman, "send_email") as send:
            with mock.patch.object(deadman, "evaluate",
                                    return_value={"state": "ok", "alert": False, "reason": "fresh"}):
                deadman.main(["--now", "1000000"])
            send.assert_not_called()
            with mock.patch.object(deadman, "evaluate",
                                    return_value={"state": "alarm", "alert": True, "reason": "stale"}):
                deadman.main(["--now", "1000000"])
            send.assert_called_once()
            self.assertIn("NO HEARTBEAT", send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
