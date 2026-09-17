"""Loopback-only PWA for a sanitized, read-only quota feed."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

PROVIDERS = {"codex", "claude", "clinepass", "command-code", "opencode", "zai"}
MAX_BOARD_ROWS = 200
BOARD_LISTS = ("planned", "active", "blocked", "landed_today")
BOARD_ROW_STRINGS = (
    "project",
    "id",
    "title",
    "focus",
    "section",
    "state",
    "lane",
    "model",
    "reason",
)
BOARD_ROW_NUMBERS = ("age", "minutes", "round", "max_rounds")
SCORECARD_COUNTS = (
    "attempts",
    "completed",
    "accepted",
    "held",
    "resolved",
    "repairs",
    "usage_reported",
)


def clean_scorecard(rows):
    """Sanitize scorecard rows to their identity keys, counts and usage totals.

    Unknown keys are stripped; rows without family/model/category are dropped. This is
    routing evidence for the dashboard, never task names, attempts or credentials.
    """
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        entry = {}
        for key in ("family", "model", "category"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value[:60]
        if len(entry) != 3:
            continue
        for key in SCORECARD_COUNTS:
            value = row.get(key)
            if type(value) is int and not isinstance(value, bool) and 0 <= value <= 10**9:
                entry[key] = value
        usage = row.get("usage")
        if isinstance(usage, dict):
            safe_usage = {}
            for name, value in list(usage.items())[:12]:
                if (
                    isinstance(name, str)
                    and type(value) in (int, float)
                    and math.isfinite(value)
                    and value >= 0
                ):
                    safe_usage[name[:40]] = value
            if safe_usage:
                entry["usage"] = safe_usage
        clean.append(entry)
    return clean


def clean_operator(rows):
    """Sanitize operator rows to their kind, id, reason and since.

    Unknown keys are stripped; rows without kind and id are dropped. This is routing
    attention for the dashboard, never task names, prompts or credentials.
    """
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        entry = {}
        for key in ("kind", "id", "reason", "since"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value[:200]
        if "kind" in entry and "id" in entry:
            clean.append(entry)
    return clean


def clean_accepted_work(rows):
    """Sanitize accepted-work rows to week, account and their two counts.

    Unknown keys are stripped; rows without week and account, or with counts outside
    0..10^9, are dropped.
    """
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        entry = {}
        for key in ("week", "account"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value[:200]
        if len(entry) != 2:
            continue
        for key in ("accepted", "attempts"):
            value = row.get(key)
            if type(value) is int and not isinstance(value, bool) and 0 <= value <= 10**9:
                entry[key] = value
        if len(entry) == 4:
            clean.append(entry)
    return clean


def clean_reviewer_recall(rows):
    """Sanitize reviewer-recall rows to run, lane, the two rates and the scored instant.

    Unknown keys are stripped; rows without a run and lane, or whose rates are not
    fractions in 0..1, are dropped. Recall and precision keep the fixed row shape: a lane
    with no scorable cases stays null rather than becoming 0.
    """
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        entry = {}
        for key in ("run_id", "lane", "scored_at"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value[:200]
        if "run_id" not in entry or "lane" not in entry:
            continue
        for key in ("recall", "precision"):
            value = row.get(key)
            if value is None:
                entry[key] = None
            elif type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
                entry[key] = float(value)
            else:
                entry[key] = None
        clean.append(entry)
    return clean


CAPACITY_PANEL_ROW_COUNTS = (
    "landed_count",
    "failed_abandoned_count",
)
CAPACITY_PANEL_ROW_AMOUNTS = (
    "landed_consumed",
    "failed_abandoned_consumed",
)


def clean_capacity_panel(rows):
    """Sanitize the 02-B5 capacity panel: provider plus two counts and two amounts.

    Unknown keys are stripped; rows without a recognized provider are dropped.
    """
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows[:50]:
        if not isinstance(row, dict) or row.get("provider") not in PROVIDERS:
            continue
        entry = {"provider": row["provider"]}
        for key in CAPACITY_PANEL_ROW_COUNTS:
            value = row.get(key)
            if type(value) is int and not isinstance(value, bool) and 0 <= value <= 10**9:
                entry[key] = value
            else:
                entry[key] = 0
        for key in CAPACITY_PANEL_ROW_AMOUNTS:
            value = row.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                entry[key] = float(value)
            else:
                entry[key] = 0.0
        clean.append(entry)
    return clean


def _clean_board_row(row):
    entry = {}
    for key in BOARD_ROW_STRINGS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            entry[key] = value[:2000]
        elif value is None:
            entry[key] = None
    if not isinstance(entry.get("id"), str):
        return None
    for key in BOARD_ROW_NUMBERS:
        value = row.get(key)
        if type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value):
            entry[key] = value
    if "operator_owed" in row:
        entry["operator_owed"] = bool(row["operator_owed"])
    gate = row.get("gate")
    if isinstance(gate, dict):
        entry["gate"] = {
            k: (gate[k][:2000] if isinstance(gate.get(k), str) else None)
            for k in ("name", "tail")
            if k in gate
        }
    links = row.get("links")
    if isinstance(links, dict):
        entry["links"] = {
            k: (links[k][:2000] if isinstance(links.get(k), str) else None)
            for k in ("brief", "report", "attempt")
            if k in links
        }
    return entry


def clean_boards(boards):
    """Local-only Boards section: the fixed row shape with every string bounded.

    Task ids and titles are the operator's project names, so this section never leaves
    the machine — `upload.py` drops it and the cloud's `clean_snapshot` refuses it. The
    cleaner keeps only what the page renders and drops rows without an id.
    """
    if not isinstance(boards, list):
        return []
    clean = []
    for board in boards[:20]:
        if not isinstance(board, dict) or not isinstance(board.get("name"), str):
            continue
        entry = {"name": board["name"][:80]}
        for key in BOARD_LISTS:
            rows = board.get(key)
            rows = rows if isinstance(rows, list) else []
            entry[key] = [
                cleaned
                for cleaned in (_clean_board_row(row) for row in rows[:MAX_BOARD_ROWS])
                if cleaned is not None
            ]
        clean.append(entry)
    return clean


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0


def project(raw, overlays=()):
    # An overlay is a list of account observations or {"accounts": [...], "attempts": [...],
    # "scorecard": [...], "operator": [...], "accepted_work": [...], "boards": [...]};
    # the boards list is local-only (task ids are project names) and carried for the local
    # page; overlay attempts replace
    # the upstream activity list when present, the scorecard (per model routing evidence) and
    # the operator/accepted-work lists (needs-you rows and the goal's weekly metric) are
    # accepted from the overlay only.
    overlay_accounts = overlays.get("accounts", []) if isinstance(overlays, dict) else overlays
    overlay_attempts = overlays.get("attempts") if isinstance(overlays, dict) else None
    overlay_scorecard = overlays.get("scorecard") if isinstance(overlays, dict) else None
    overlay_operator = overlays.get("operator") if isinstance(overlays, dict) else None
    overlay_accepted = overlays.get("accepted_work") if isinstance(overlays, dict) else None
    overlay_recall = overlays.get("reviewer_recall") if isinstance(overlays, dict) else None
    overlay_boards = overlays.get("boards") if isinstance(overlays, dict) else None
    overlay_capacity_panel = overlays.get("capacity_panel") if isinstance(overlays, dict) else None
    accounts = {}
    for a in [*raw.get("accounts", []), *overlay_accounts]:
        if not isinstance(a, dict) or a.get("provider") not in PROVIDERS:
            continue
        key = a["provider"]
        # Newer observations win; on an equal timestamp the later (overlay) entry wins so an
        # overlay can enrich the same observation, e.g. with derived reset instants.
        if key in accounts and timestamp(a.get("observed_at")) < timestamp(
            accounts[key].get("observed_at")
        ):
            continue
        windows = []
        for w in a.get("windows", []):
            if not isinstance(w, dict):
                continue
            value = w.get("used_percent")
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                value = None
            windows.append(
                dict(
                    id=str(w.get("id", "unknown"))[:80],
                    used_percent=value,
                    resets_at=w.get("resets_at"),
                    reset_label=w.get("reset_label"),
                )
            )
        remaining = a.get("monthly_remaining")
        if type(remaining) not in (int, float) or not math.isfinite(remaining):
            remaining = None
        accounts[key] = dict(
            provider=key,
            observed_at=a.get("observed_at"),
            status=a.get("status", "unknown"),
            windows=windows,
            monthly_remaining=remaining,
        )
    return dict(
        accounts=list(accounts.values()),
        attempts=[
            {k: a.get(k) for k in ("task", "provider", "model", "status", "at")}
            for a in (
                overlay_attempts if isinstance(overlay_attempts, list) else raw.get("attempts", [])
            )[:30]
            if isinstance(a, dict)
        ],
        scorecard=clean_scorecard(overlay_scorecard),
        operator=clean_operator(overlay_operator),
        accepted_work=clean_accepted_work(overlay_accepted),
        reviewer_recall=clean_reviewer_recall(overlay_recall),
        boards=clean_boards(overlay_boards),
        capacity_panel=clean_capacity_panel(overlay_capacity_panel),
        served_at=datetime.now(timezone.utc).isoformat(),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    parser.add_argument("--upstream", default="http://127.0.0.1:8020/api/usage")
    parser.add_argument("--overlay", type=Path)
    args = parser.parse_args()
    parsed = urlsplit(args.upstream)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username
        or parsed.password
    ):
        parser.error("upstream must be a credential-free loopback HTTP feed")
    root = Path(__file__).with_name("capacity_web")
    allowed = {"/": "index.html", **{("/" + p.name): p.name for p in root.iterdir() if p.is_file()}}
    types = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css",
        ".js": "application/javascript",
        ".png": "image/png",
        ".webmanifest": "application/manifest+json",
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header(
                "Cache-Control", "no-store" if mime.startswith("application/json") else "no-cache"
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def overlay(self):
            if args.overlay and args.overlay.exists():
                try:
                    return json.loads(args.overlay.read_text())
                except (OSError, ValueError):
                    return []
            return []

        def do_GET(self):
            if self.headers.get("Host") not in {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}:
                return self.reply(403, b"Forbidden", "text/plain")
            path = urlsplit(self.path).path
            if path == "/api/boards":
                # Local only and independent of the upstream feed: the Boards section the
                # overlay builder writes is always answerable, even with no quota feed.
                overlays = self.overlay()
                boards = overlays.get("boards") if isinstance(overlays, dict) else None
                body = json.dumps({"boards": clean_boards(boards)}, allow_nan=False).encode()
                return self.reply(200, body, "application/json")
            if path == "/api/usage":
                try:
                    with urlopen(args.upstream, timeout=8) as response:
                        raw = json.load(response)
                    body = json.dumps(project(raw, self.overlay()), allow_nan=False).encode()
                    return self.reply(200, body, "application/json")
                except Exception:
                    return self.reply(
                        503, b'{"error":"Local quota feed unavailable"}', "application/json"
                    )
            if path not in allowed:
                return self.reply(404, b"Not found", "text/plain")
            p = root / allowed[path]
            return self.reply(200, p.read_bytes(), types.get(p.suffix, "application/octet-stream"))

    print(f"Inference capacity PWA: http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
