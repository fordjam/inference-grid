"""02-A6: dead-man for the tick-boards loop.

Alarms by email when the loop's heartbeat (deployments/local/tick_boards.py,
DEFAULT_HEARTBEAT) is stale. Reads last_pass_at when present (a wedged pass
keeps written_at fresh on its own, which is exactly the gap 02-A1's review
found -- see tick_boards.py's write_heartbeat docstring); before any pass has
ever completed (e.g. right after a restart, or a first pass still legitimately
in flight), reads started_at instead, against a much larger allowance matching
tick_boards.TICK_TIMEOUT -- the first pass may take that long; falls back to
written_at, at the normal threshold, only for a heartbeat written before
either field existed.

THRESHOLD_SECONDS is NOT the plan's literal "30 min": tick_boards.py's own
IDLE_SECONDS is 1800s, and last_pass_at only lands up to HEARTBEAT_INTERVAL
(300s) after a pass truly ends, so a perfectly healthy idle loop's observed
last_pass_at age routinely reaches ~1800+300s -- a flat 1800s threshold
false-alarms on a quiet night, verified in review. THRESHOLD_SECONDS is
IDLE_SECONDS + HEARTBEAT_INTERVAL + a tick-time margin instead.

Same email mechanism as vix-rs and the other household projects: plain
smtplib over SMTP_SSL, Gmail app password read at runtime from
~/.seats_gmail_app_password. Nothing here ever stores, logs or prints the
password.

This script only renders a plist template (--render-plist); it never calls
launchctl and is not installed by any command in this repo. Loading the
agent is an operator step, same as install.py.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tick_boards import IDLE_SECONDS, HEARTBEAT_INTERVAL, TICK_TIMEOUT  # noqa: E402

GMAIL_USER = "james.d.fordham@gmail.com"
APP_PASSWORD_FILE = Path(os.path.expanduser("~/.seats_gmail_app_password"))
DEFAULT_HEARTBEAT = Path.home() / ".local/share/inference-grid/heartbeat/tick-boards.json"
TICK_TIME_MARGIN_SECONDS = 300
THRESHOLD_SECONDS = IDLE_SECONDS + HEARTBEAT_INTERVAL + TICK_TIME_MARGIN_SECONDS  # 2400s = 40min
# The first pass after a (re)start may legitimately take up to TICK_TIMEOUT; give it
# that plus the same beat-lag margin before treating "no pass yet" as an outage.
FIRST_PASS_ALLOWANCE_SECONDS = TICK_TIMEOUT + HEARTBEAT_INTERVAL + TICK_TIME_MARGIN_SECONDS
CHECK_INTERVAL_SECONDS = 15 * 60


def recipients() -> list[str]:
    override = os.environ.get("INFERENCE_GRID_EMAIL_TO", "")
    if override.strip():
        return [addr.strip() for addr in override.split(",") if addr.strip()]
    return [GMAIL_USER]


def send_email(subject: str, body: str) -> None:
    """Send a plain-text alert. Raises on failure so callers can log it."""
    if not APP_PASSWORD_FILE.exists():
        raise RuntimeError(
            f"Gmail app password file missing: {APP_PASSWORD_FILE}\n"
            "Create at https://myaccount.google.com/apppasswords, then:\n"
            f"  echo 'your-app-password' > {APP_PASSWORD_FILE} && chmod 600 {APP_PASSWORD_FILE}"
        )
    app_password = APP_PASSWORD_FILE.read_text().strip()
    to = recipients()
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = f"inference-grid <{GMAIL_USER}>"
    msg["To"] = ", ".join(to)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, app_password)
        server.sendmail(GMAIL_USER, to, msg.as_string())


def evaluate(
    path,
    now: float,
    threshold_seconds: float = THRESHOLD_SECONDS,
    first_pass_allowance_seconds: float = FIRST_PASS_ALLOWANCE_SECONDS,
) -> dict:
    """Pure verdict on the heartbeat file: state / alert / reason. No side effects.

    Three tiers, in order: last_pass_at (a real pass completed; normal threshold),
    started_at (no pass has completed yet -- the process may still be inside its
    first, legitimately long, pass; the much larger first_pass_allowance_seconds),
    written_at (neither field exists -- an old-format heartbeat; normal threshold,
    since there is no way to distinguish "just started" from "long since wedged").
    """
    try:
        heartbeat = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {
            "state": "alarm",
            "alert": True,
            "reason": f"heartbeat file missing: {path}",
        }
    except (OSError, ValueError) as exc:
        return {
            "state": "alarm",
            "alert": True,
            "reason": f"heartbeat unreadable ({exc!r})",
        }
    if heartbeat.get("last_pass_at"):
        stamp, kind, allowance = heartbeat["last_pass_at"], "last_pass_at", threshold_seconds
    elif heartbeat.get("started_at"):
        stamp, kind, allowance = (
            heartbeat["started_at"],
            "started_at (no pass has completed yet)",
            first_pass_allowance_seconds,
        )
    elif heartbeat.get("written_at"):
        stamp, kind, allowance = heartbeat["written_at"], "written_at", threshold_seconds
    else:
        return {
            "state": "alarm",
            "alert": True,
            "reason": "heartbeat has none of last_pass_at, started_at or written_at",
        }
    try:
        # The stamp is UTC (tick_boards.py writes it via time.gmtime()); mktime()
        # would reinterpret the parsed struct as local time and skew the age by the
        # local UTC offset. timegm() is the correct inverse of gmtime().
        at = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError as exc:
        return {
            "state": "alarm",
            "alert": True,
            "reason": f"heartbeat timestamp unparsable ({exc!r}): {stamp!r}",
        }
    age = now - at
    if age > allowance:
        return {
            "state": "alarm",
            "alert": True,
            "reason": f"{kind} is {age / 60:.0f} min old (over the {allowance / 60:.0f} min allowance)",
        }
    return {
        "state": "ok",
        "alert": False,
        "reason": f"{kind} is {age / 60:.0f} min old",
    }


PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>{python}</string>
\t\t<string>{script}</string>
\t\t<string>--heartbeat</string>
\t\t<string>{heartbeat}</string>
\t\t<string>--threshold-minutes</string>
\t\t<string>{threshold_minutes}</string>
\t</array>
\t<key>StartInterval</key>
\t<integer>{interval}</integer>
\t<key>RunAtLoad</key>
\t<false/>
\t<key>StandardErrorPath</key>
\t<string>{log}</string>
\t<key>StandardOutPath</key>
\t<string>{log}</string>
</dict>
</plist>
"""


def render_plist(
    python, script, heartbeat, log,
    threshold_minutes=THRESHOLD_SECONDS / 60, interval=CHECK_INTERVAL_SECONDS,
):
    """The dead-man's plist body. A StartInterval job, unlike the four KeepAlive runtimes
    in install.py: this check must run periodically even while the loop is healthy, not
    stay alive continuously."""
    return PLIST_TEMPLATE.format(
        label="com.inference-grid.tick-boards-deadman",
        python=python,
        script=script,
        heartbeat=heartbeat,
        threshold_minutes=threshold_minutes,
        interval=interval,
        log=log,
    )


def bootstrap_command(plist_path):
    return f"launchctl bootstrap gui/{os.getuid()} {plist_path}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", default=str(DEFAULT_HEARTBEAT))
    parser.add_argument("--threshold-minutes", type=float, default=THRESHOLD_SECONDS / 60)
    parser.add_argument("--now", type=float, default=None, help="override epoch seconds (drills/tests)")
    parser.add_argument(
        "--render-plist",
        metavar="OUT_DIR",
        default=None,
        help="write the plist template to OUT_DIR and print the (not-run) bootstrap command; "
        "never calls launchctl",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--script", default=str(Path(__file__).resolve()),
        help="path this script will be invoked from once installed",
    )
    parser.add_argument(
        "--log", default=str(Path.home() / "Library/Logs/inference-grid/tick-boards-deadman.log"),
    )
    args = parser.parse_args(argv)

    if args.render_plist:
        out_dir = Path(args.render_plist).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        plist_path = out_dir / "com.inference-grid.tick-boards-deadman.plist"
        plist_path.write_text(
            render_plist(args.python, args.script, args.heartbeat, args.log, args.threshold_minutes)
        )
        print(f"wrote {plist_path}")
        print(bootstrap_command(plist_path))
        return 0

    now = args.now if args.now is not None else time.time()
    verdict = evaluate(args.heartbeat, now, threshold_seconds=args.threshold_minutes * 60)
    print(f"{time.strftime('%FT%TZ', time.gmtime(now))} {verdict['state']}: {verdict['reason']}")
    if verdict["alert"]:
        send_email(
            "inference-grid tick-boards NO HEARTBEAT",
            f"The tick-boards loop's heartbeat is stale.\n\nReason: {verdict['reason']}\n\n"
            "Check `launchctl list | grep inference-grid` and the tick-boards log.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
