"""Render one launchd plist per local capacity runtime, all kept alive.

Four runtimes run the operator's capacity layer: ``capacity-loop`` (the KeepAlive
scheduler), ``capacity-feed``, ``capacity-web`` (the dashboard server) and ``tick-boards``
(the boards loop). Each gets its own plist in a directory the operator names, and the
``launchctl bootstrap`` command for each is printed. It never runs ``launchctl`` itself;
loading the agents is an operator step.

Every plist is the same shape — ``KeepAlive`` and ``RunAtLoad`` true, ``ProcessType:
Interactive``, a PATH that includes ``/opt/homebrew/bin`` (the CLIs the lanes spawn are
``#!/usr/bin/env node`` scripts, and launchd's default PATH cannot find node) — and never
``StartInterval``. launchd parks interval spawns for a GUI-session
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
import os
import sys
from pathlib import Path

DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
DOMAIN = "com.inference-grid"

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


def render(label, python, script, log):
    """The plist for one runtime: label, interpreter, script and log paths."""
    return PLIST_TEMPLATE.format(label=label, arguments=_arguments([python, script]), log=log)


def bootstrap_command(plist_path):
    return f"launchctl bootstrap gui/{os.getuid()} {plist_path}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the local capacity launchd plists.")
    parser.add_argument("--out-dir", required=True, help="directory to write the plists into")
    parser.add_argument(
        "--python", default=sys.executable, help="interpreter that runs every runtime"
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

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, _script, _log in RUNTIMES:
        key = name.replace("-", "_")
        label = f"{DOMAIN}.{name}"
        plist_path = out_dir / (label + ".plist")
        plist_path.write_text(
            render(label, args.python, getattr(args, f"{key}_script"), getattr(args, f"{key}_log"))
        )
        print(f"wrote {plist_path}")
        print(bootstrap_command(plist_path))


if __name__ == "__main__":
    main()
