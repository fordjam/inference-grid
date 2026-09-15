"""Render one launchd plist per local capacity runtime, all kept alive.

Four runtimes run the operator's capacity layer: ``capacity-loop`` (the KeepAlive
scheduler), ``capacity-feed``, ``capacity-web`` (the dashboard server) and ``tick-boards``
(the boards loop). Each gets its own plist in a directory the operator names, and the
``launchctl bootstrap`` command for each is printed. It never runs ``launchctl`` itself;
loading the agents is an operator step.

Every plist is the same shape — ``KeepAlive`` and ``RunAtLoad`` true, ``ProcessType:
Interactive``, a PATH that includes ``/opt/homebrew/bin`` (the CLIs the lanes spawn are
``#!/usr/bin/env node`` scripts, and launchd's default PATH cannot find node), and an
``ExitTimeOut`` of the longest packet wall clock plus a minute, so launchd does not
``SIGKILL`` the tick loop while it drains a signal — and never ``StartInterval``. launchd
parks interval spawns for a GUI-session
agent while the display is off ("pended nondemand spawn = interval"), which is exactly when
the phone dashboard is the only view; a process that is already running is not held. So the
runtimes are kept alive rather than scheduled, and they come back after a reboot: the
2026-09-15 reboot killed the hand-started feed, dashboard server and boards loop while the one
KeepAlive agent survived.

    python3 install.py --out-dir ~/Library/LaunchAgents \
        --python ~/.local/share/inference-grid/venv/bin/python \
        --script ~/.local/share/inference-grid-capacity/capacity_loop.py
"""

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
DOMAIN = "com.inference-grid"
# lanes/config.py caps a lane's wall_seconds at 3600, so without a lanes.json that is the
# longest packet wall clock there can be.
LANE_WALL_CAP = 3600

# One row per runtime: short name (also the label suffix), default script file, default log.
RUNTIMES = (
    ("capacity-loop", "capacity_loop.py", "capacity-loop.log"),
    ("capacity-feed", "capacity_feed.py", "capacity-feed.log"),
    ("capacity-web", "capacity_web.py", "capacity-web.log"),
    ("tick-boards", "tick_boards.py", "tick-boards.log"),
)

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>KeepAlive</key>
\t<true/>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>PATH</key>
\t\t<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
\t</dict>
\t<key>ExitTimeOut</key>
\t<integer>{exit_timeout}</integer>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProcessType</key>
\t<string>Interactive</string>
\t<key>ProgramArguments</key>
\t<array>
{arguments}\t</array>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>StandardErrorPath</key>
\t<string>{log}</string>
\t<key>StandardOutPath</key>
\t<string>{log}</string>
</dict>
</plist>
"""


def _arguments(argv):
    """The ProgramArguments array body: one indented <string> per argument."""
    return "".join(f"\t\t<string>{arg}</string>\n" for arg in argv)


def exit_timeout(lanes=None):
    """launchd's ExitTimeOut: the longest packet wall clock plus a minute.

    A draining tick loop must outlast the board tick in flight, whose packets each run up
    to their lane's ``wall_seconds``; ``lanes`` is the parsed operator lanes.json, and
    without one the wall cap stands in for the longest lane.
    """
    walls = [
        spec["wall_seconds"]
        for spec in (lanes or {}).get("lanes", {}).values()
        if isinstance(spec, dict) and isinstance(spec.get("wall_seconds"), int)
    ]
    return (max(walls) if walls else LANE_WALL_CAP) + 60


def render(label, python, script, log, timeout):
    """The plist for one runtime: label, interpreter, script, log paths and ExitTimeOut."""
    return PLIST_TEMPLATE.format(
        label=label, arguments=_arguments([python, script]), log=log, exit_timeout=timeout
    )


def bootstrap_command(plist_path):
    return f"launchctl bootstrap gui/{os.getuid()} {plist_path}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the local capacity launchd plists.")
    parser.add_argument("--out-dir", required=True, help="directory to write the plists into")
    parser.add_argument(
        "--python", default=sys.executable, help="interpreter that runs every runtime"
    )
    parser.add_argument(
        "--lanes",
        default=None,
        help="operator lanes.json; ExitTimeOut comes from its longest wall_seconds "
        "(default: the 3600 cap) plus a minute",
    )
    for name, script, log in RUNTIMES:
        key = name.replace("-", "_")
        script_flags = [f"--{name}-script"]
        log_flags = [f"--{name}-log"]
        if name == "capacity-loop":
            script_flags.append("--script")
            log_flags.append("--log")
        parser.add_argument(*script_flags, dest=f"{key}_script", default=str(DEFAULT_DIR / script))
        parser.add_argument(*log_flags, dest=f"{key}_log", default=str(DEFAULT_DIR / log))
    args = parser.parse_args(argv)
    timeout = exit_timeout(json.loads(Path(args.lanes).read_text()) if args.lanes else None)

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, _script, _log in RUNTIMES:
        key = name.replace("-", "_")
        label = f"{DOMAIN}.{name}"
        plist_path = out_dir / (label + ".plist")
        plist_path.write_text(
            render(
                label,
                args.python,
                getattr(args, f"{key}_script"),
                getattr(args, f"{key}_log"),
                timeout,
            )
        )
        print(f"wrote {plist_path}")
        print(bootstrap_command(plist_path))


if __name__ == "__main__":
    main()
