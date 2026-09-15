"""Authenticated snapshot dashboard; contains no native provider credentials."""

from collections import deque
from datetime import datetime
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
from capacity import clean_scorecard, project

MAX_BODY = 128 * 1024
COOKIE = "__Host-grid_session"
CSRF_COOKIE = "__Host-grid_csrf"
# A request waits this long for the Mac's outbound poll before it is marked failed,
# and this long for a claimed collection to finish. Both are wall-clock seconds.
QUEUED_TTL = 120
COLLECTING_TTL = 300
REFRESH_TERMINAL = ("completed", "cooldown", "failed")
PROVIDER_STATUSES = ("ok", "cooldown", "auth_required", "error", "unknown")
REASONS = (
    "collected",
    "snapshot_only",
    "collector_unavailable",
    "collector_error",
    "collector_timeout",
    "upload_failed",
    "mac_not_reporting",
    "collection_timeout",
)


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def token(secret, now=None):
    expires = int(time.time() if now is None else now) + 30 * 86400
    body = f"{expires}.{secrets.token_hex(16)}"
    return body + "." + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def valid_token(value, secret, now=None):
    try:
        expiry, nonce, sig = value.split(".")
        expected = hmac.new(
            secret.encode(), f"{expiry}.{nonce}".encode(), hashlib.sha256
        ).hexdigest()
        return (
            len(value) < 256
            and int(expiry) > (time.time() if now is None else now)
            and hmac.compare_digest(sig, expected)
        )
    except (ValueError, TypeError, AttributeError):
        return False


def clean_snapshot(raw, now=None):
    now = time.time() if now is None else now
    if not isinstance(raw, dict):
        raise ValueError("object required")
    captured = raw.get("captured_at")
    dt = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    if dt.utcoffset() is None or not now - 600 <= dt.timestamp() <= now + 60:
        raise ValueError("stale upload")
    accounts = raw.get("accounts")
    if not isinstance(accounts, list) or len(accounts) > 10:
        raise ValueError("invalid accounts")
    for a in accounts:
        if (
            not isinstance(a, dict)
            or not isinstance(a.get("windows"), list)
            or len(a["windows"]) > 12
        ):
            raise ValueError("invalid windows")
        for key in ("provider", "status", "observed_at"):
            if a.get(key) is not None and (not isinstance(a[key], str) or len(a[key]) > 100):
                raise ValueError("invalid field")
        if a.get("observed_at"):
            observed = datetime.fromisoformat(a["observed_at"].replace("Z", "+00:00"))
            if observed.utcoffset() is None or observed.timestamp() > now + 60:
                raise ValueError("invalid observation")
        for w in a["windows"]:
            if not isinstance(w, dict):
                raise ValueError("invalid window")
            for key in ("id", "resets_at", "reset_label"):
                if w.get(key) is not None and (not isinstance(w[key], str) or len(w[key]) > 150):
                    raise ValueError("invalid window field")
    result = project({"accounts": accounts, "attempts": []})
    # Never upload local task names, prompts, account IDs, cookies or credentials.
    result["attempts"] = []
    scorecard = raw.get("scorecard", [])
    if not isinstance(scorecard, list) or len(scorecard) > 100:
        raise ValueError("invalid scorecard")
    # Routing evidence only: clean_scorecard strips every unknown key from each row.
    result["scorecard"] = clean_scorecard(scorecard)
    result["captured_at"] = captured
    return result, dt.timestamp()


def clean_outcome(raw, now=None):
    """Accept only the fixed refresh-outcome shape; never store raw collector output."""
    now = time.time() if now is None else now
    if not isinstance(raw, dict):
        raise ValueError("object required")
    state = raw.get("state")
    reason = raw.get("reason")
    if state not in REFRESH_TERMINAL or reason not in REASONS:
        raise ValueError("invalid outcome")
    providers = raw.get("providers", [])
    if not isinstance(providers, list) or len(providers) > 10:
        raise ValueError("invalid providers")
    clean = []
    for p in providers:
        if (
            not isinstance(p, dict)
            or not isinstance(p.get("provider"), str)
            or len(p["provider"]) > 40
        ):
            raise ValueError("invalid provider")
        if p.get("status") not in PROVIDER_STATUSES:
            raise ValueError("invalid provider status")
        eligible = p.get("next_eligible_at")
        if eligible is not None:
            if not isinstance(eligible, str) or len(eligible) > 100:
                raise ValueError("invalid eligibility")
            dt = datetime.fromisoformat(eligible.replace("Z", "+00:00"))
            if dt.utcoffset() is None or dt.timestamp() > now + 30 * 86400:
                raise ValueError("invalid eligibility")
        clean.append(
            {"provider": p["provider"], "status": p["status"], "next_eligible_at": eligible}
        )
    collected = raw.get("collected")
    if not isinstance(collected, bool):
        raise ValueError("collected flag required")
    return {"state": state, "reason": reason, "collected": collected, "providers": clean}


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS snapshot(id INTEGER PRIMARY KEY CHECK(id=1), captured REAL NOT NULL, body TEXT NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS refresh_request(id TEXT PRIMARY KEY, state TEXT NOT NULL, requested_at REAL NOT NULL, claimed_at REAL, completed_at REAL, outcome TEXT)"
            )

    def put(self, data, captured):
        body = json.dumps(data, allow_nan=False)
        with sqlite3.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT captured FROM snapshot WHERE id=1").fetchone()
            if old and captured <= old[0]:
                return False
            c.execute(
                "INSERT INTO snapshot VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET captured=excluded.captured,body=excluded.body",
                (captured, body),
            )
        return True

    def get(self):
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT body FROM snapshot WHERE id=1").fetchone()
        return (
            json.loads(row[0])
            if row
            else {"accounts": [], "attempts": [], "scorecard": [], "captured_at": None}
        )

    # Refresh requests: queued -> collecting -> completed | cooldown | failed.
    # Rows are written by the phone (queue), the Mac (claim, complete) and expiry.
    @staticmethod
    def _row(r):
        if not r:
            return None
        keys = ("id", "state", "requested_at", "claimed_at", "completed_at", "outcome")
        d = dict(zip(keys, r))
        d["outcome"] = json.loads(d["outcome"]) if d["outcome"] else None
        return d

    @staticmethod
    def _expire(c, now):
        def failed(reason):
            return json.dumps(
                {"state": "failed", "reason": reason, "collected": False, "providers": []}
            )

        c.execute(
            "UPDATE refresh_request SET state='failed',completed_at=?,outcome=? WHERE state='queued' AND requested_at<?",
            (now, failed("mac_not_reporting"), now - QUEUED_TTL),
        )
        c.execute(
            "UPDATE refresh_request SET state='failed',completed_at=?,outcome=? WHERE state='collecting' AND claimed_at<?",
            (now, failed("collection_timeout"), now - COLLECTING_TTL),
        )
        c.execute("DELETE FROM refresh_request WHERE completed_at<?", (now - 86400,))

    def request_refresh(self, now=None):
        """Queue a collection request; an open request is returned instead of a duplicate."""
        now = time.time() if now is None else now
        with sqlite3.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire(c, now)
            row = c.execute(
                "SELECT * FROM refresh_request WHERE state IN ('queued','collecting') ORDER BY requested_at LIMIT 1"
            ).fetchone()
            if row:
                return self._row(row), False
            rid = secrets.token_hex(8)
            c.execute("INSERT INTO refresh_request VALUES(?,'queued',?,NULL,NULL,NULL)", (rid, now))
            return self._row(
                c.execute("SELECT * FROM refresh_request WHERE id=?", (rid,)).fetchone()
            ), True

    def get_refresh(self, rid=None, now=None):
        now = time.time() if now is None else now
        with sqlite3.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire(c, now)
            if rid is None:
                row = c.execute(
                    "SELECT * FROM refresh_request ORDER BY requested_at DESC LIMIT 1"
                ).fetchone()
            else:
                row = c.execute("SELECT * FROM refresh_request WHERE id=?", (rid,)).fetchone()
            return self._row(row)

    def claim_refresh(self, now=None):
        """Mac side: take the oldest queued request. At most one request collects at a time."""
        now = time.time() if now is None else now
        with sqlite3.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire(c, now)
            if c.execute("SELECT 1 FROM refresh_request WHERE state='collecting'").fetchone():
                return None
            row = c.execute(
                "SELECT id FROM refresh_request WHERE state='queued' ORDER BY requested_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE refresh_request SET state='collecting',claimed_at=? WHERE id=? AND state='queued'",
                (now, row[0]),
            )
            return self._row(
                c.execute("SELECT * FROM refresh_request WHERE id=?", (row[0],)).fetchone()
            )

    def complete_refresh(self, rid, outcome, now=None):
        """Mac side: record a sanitized terminal outcome. Only a collecting request can complete."""
        now = time.time() if now is None else now
        with sqlite3.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire(c, now)
            changed = c.execute(
                "UPDATE refresh_request SET state=?,completed_at=?,outcome=? WHERE id=? AND state='collecting'",
                (outcome["state"], now, json.dumps(outcome), rid),
            ).rowcount
            return changed == 1


def handler(config, store):
    failures = deque()
    lock = threading.Lock()
    root = Path(__file__).with_name("web")
    static = {("/" + p.name): p for p in root.iterdir() if p.is_file()}
    types = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css",
        ".js": "application/javascript",
        ".png": "image/png",
        ".webmanifest": "application/manifest+json",
    }
    login = b"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#102521"><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/icon-192.png"><link rel="stylesheet" href="/app.css"><title>Sign in - Inference Grid</title></head><body><main style="margin:8vh auto;max-width:470px"><span class="eyebrow">INFERENCE GRID</span><h1 style="margin-top:25px">Your capacity, anywhere.</h1><p>Sign in to see your private usage dashboard.</p><form method="post" action="/login"><label for="username">Username</label><input id="username" name="username" value="owner" autocomplete="username" required style="display:block;width:100%;padding:12px;margin:8px 0 20px"><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required style="display:block;width:100%;padding:12px;margin:8px 0 20px"><button class="primary" type="submit">Sign in</button></form><p class="caption">Provider credentials stay on your Mac. This site receives usage snapshots only.</p></main></body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body=b"", mime="text/plain", headers=None):
            self.send_response(code)
            for k, v in {
                "Content-Type": mime,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "Strict-Transport-Security": "max-age=31536000",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                **(headers or {}),
            }.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            return self.headers.get("Host") == config["host"]

        def signed_in(self):
            try:
                c = SimpleCookie()
                c.load(self.headers.get("Cookie", ""))
                return COOKIE in c and valid_token(c[COOKIE].value, config["session_key"])
            except Exception:
                return False

        def same_origin(self):
            return self.headers.get("Origin") == "https://" + config["host"]

        def body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("invalid body size")
            self.connection.settimeout(10)
            return self.rfile.read(length)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                # The watcher compares this commit with the deploy bundle's newest commit.
                commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get(
                    "GRID_DEPLOY_COMMIT"
                )
                return self.reply(
                    200, json.dumps({"ok": True, "commit": commit}).encode(), "application/json"
                )
            if not self.host_ok():
                return self.reply(403, b"Forbidden")
            if path == "/login":
                csrf = token(config["session_key"] + "csrf")
                page = login.replace(
                    b'<label for="username">',
                    (
                        '<input type="hidden" name="csrf" value="'
                        + csrf
                        + '"><label for="username">'
                    ).encode(),
                )
                return self.reply(
                    200,
                    page,
                    "text/html; charset=utf-8",
                    {
                        "Set-Cookie": CSRF_COOKIE
                        + "="
                        + csrf
                        + "; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=1800"
                    },
                )
            if path == "/api/usage":
                if not self.signed_in():
                    return self.reply(401, b'{"error":"Sign in required"}', "application/json")
                return self.reply(200, json.dumps(store.get()).encode(), "application/json")
            if path == "/api/refresh":
                if not self.signed_in():
                    return self.reply(401, b'{"error":"Sign in required"}', "application/json")
                try:
                    rid = parse_qs(urlsplit(self.path).query, max_num_fields=2).get("id", [None])[0]
                    if rid is not None and not (
                        len(rid) == 16 and all(ch in "0123456789abcdef" for ch in rid)
                    ):
                        raise ValueError
                except ValueError:
                    return self.reply(400, b'{"error":"Invalid request id"}', "application/json")
                row = store.get_refresh(rid)
                if not row:
                    return self.reply(404, b'{"error":"No refresh request"}', "application/json")
                return self.reply(200, json.dumps(row).encode(), "application/json")
            if path in ("/", "/index.html"):
                if not self.signed_in():
                    return self.reply(303, headers={"Location": "/login"})
                return self.reply(
                    200, (root / "index.html").read_bytes(), "text/html; charset=utf-8"
                )
            if path in static and path != "/index.html":
                return self.reply(
                    200,
                    static[path].read_bytes(),
                    types.get(static[path].suffix, "application/octet-stream"),
                )
            return self.reply(404, b"Not found")

        def do_POST(self):
            if not self.host_ok():
                return self.reply(403, b"Forbidden")
            path = urlsplit(self.path).path
            if path == "/api/snapshot":
                auth = self.headers.get("Authorization", "")
                if not hmac.compare_digest(auth, "Bearer " + config["upload_token"]):
                    return self.reply(401, b"Unauthorized")
                try:
                    data, captured = clean_snapshot(json.loads(self.body()))
                    if not store.put(data, captured):
                        return self.reply(409, b"Older snapshot refused")
                    return self.reply(200, b'{"stored":true}', "application/json")
                except Exception:
                    return self.reply(400, b"Invalid snapshot")
            if path in ("/api/refresh/claim", "/api/refresh/complete"):
                # Outbound poll from the Mac. The upload token cannot read the dashboard
                # and the dashboard session cannot claim or complete collection work.
                auth = self.headers.get("Authorization", "")
                if not hmac.compare_digest(auth, "Bearer " + config["upload_token"]):
                    return self.reply(401, b"Unauthorized")
                if path == "/api/refresh/claim":
                    row = store.claim_refresh()
                    return (
                        self.reply(200, json.dumps(row).encode(), "application/json")
                        if row
                        else self.reply(204)
                    )
                try:
                    body = json.loads(self.body())
                    rid = body.get("id")
                    outcome = clean_outcome(body.get("outcome"))
                    if not isinstance(rid, str) or len(rid) != 16:
                        raise ValueError("invalid id")
                except Exception:
                    return self.reply(400, b"Invalid outcome")
                if not store.complete_refresh(rid, outcome):
                    return self.reply(
                        409, b'{"error":"Request is not collecting"}', "application/json"
                    )
                return self.reply(200, b'{"recorded":true}', "application/json")
            if path == "/api/refresh":
                # Only a signed-in same-origin page may ask the Mac to collect. The request
                # cannot launch inference, change credentials or shorten a provider cooldown.
                if not self.signed_in():
                    return self.reply(401, b'{"error":"Sign in required"}', "application/json")
                if not self.same_origin():
                    return self.reply(403, b"Forbidden origin")
                row, created = store.request_refresh()
                return self.reply(
                    202 if created else 200, json.dumps(row).encode(), "application/json"
                )
            if path not in ("/login", "/logout"):
                return self.reply(404, b"Not found")
            form = None
            if not self.same_origin():
                # Browsers may omit Origin or send null on form navigation. Require
                # a signed, cookie-bound form token; never accept another origin.
                if path != "/login" or self.headers.get("Origin") not in (None, "null"):
                    return self.reply(403, b"Forbidden origin")
                try:
                    form = parse_qs(self.body().decode(), max_num_fields=4)
                    csrf = form.get("csrf", [""])[0]
                    cookies = SimpleCookie()
                    cookies.load(self.headers.get("Cookie", ""))
                    valid = (
                        CSRF_COOKIE in cookies
                        and hmac.compare_digest(csrf, cookies[CSRF_COOKIE].value)
                        and valid_token(csrf, config["session_key"] + "csrf")
                    )
                except Exception:
                    valid = False
                if not valid:
                    return self.reply(403, b"Login page expired. Reload /login and try again.")
            if path == "/logout":
                return self.reply(
                    303,
                    headers={
                        "Location": "/login",
                        "Set-Cookie": COOKIE
                        + "=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0",
                    },
                )
            with lock:
                while failures and failures[0] < time.time() - 60:
                    failures.popleft()
                if len(failures) >= 10:
                    return self.reply(429, b"Too many attempts. Try again in one minute.")
                failures.append(time.time())
            try:
                if form is None:
                    form = parse_qs(self.body().decode(), max_num_fields=4)
                user = form.get("username", [""])[0]
                password = form.get("password", [""])[0]
                ok = (
                    len(password) <= 512
                    and hmac.compare_digest(user, config["username"])
                    and hmac.compare_digest(
                        password_hash(password, config["salt"]), config["password_hash"]
                    )
                )
            except Exception:
                ok = False
            if not ok:
                return self.reply(401, b"Sign-in failed. Go back and try again.")
            cookie = (
                COOKIE
                + "="
                + token(config["session_key"])
                + "; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=2592000"
            )
            return self.reply(303, headers={"Location": "/", "Set-Cookie": cookie})

    return Handler


def main():
    keys = {
        "host": "GRID_PUBLIC_HOST",
        "username": "GRID_USERNAME",
        "salt": "GRID_PASSWORD_SALT",
        "password_hash": "GRID_PASSWORD_HASH",
        "session_key": "GRID_SESSION_KEY",
        "upload_token": "GRID_UPLOAD_TOKEN",
    }
    config = {k: os.environ[v] for k, v in keys.items()}
    if len(config["session_key"]) < 32 or len(config["upload_token"]) < 32:
        raise ValueError("Strong keys required")
    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "8080"))),
        handler(config, Store(os.environ.get("GRID_SNAPSHOT_DB", "/data/capacity.sqlite"))),
    )
    print("Capacity dashboard ready", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
