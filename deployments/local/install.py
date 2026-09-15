"""Render the launchd plist for the capacity loop.

Writes the plist for ``com.inference-grid.capacity-loop`` — the KeepAlive agent that runs
capacity_loop.py, replacing the three timer agents — into a directory the operator names,
and prints the ``launchctl bootstrap`` command. It never runs ``launchctl`` itself; loading
the agent is an operator step.

    python3 install.py --out-dir ~/Library/LaunchAgents \
        --python ~/.local/share/inference-grid/venv/bin/python \
        --script ~/.local/share/inference-grid-capacity/capacity_loop.py
"""

import argparse
import os
import sys
from pathlib import Path

DEFAULT_DIR = Path.home() / ".local/share/inference-grid-capacity"
LABEL = "com.inference-grid.capacity-loop"

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>KeepAlive</key>
\t<true/>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProcessType</key>
\t<string>Interactive</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>{python}</string>
\t\t<string>{script}</string>
\t</array>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>StandardErrorPath</key>
\t<string>{error_log}</string>
\t<key>StandardOutPath</key>
\t<string>{log}</string>
</dict>
</plist>
"""


def render(label, python, script, log):
    """The plist for one capacity-loop agent: label, interpreter, script, log paths."""
    return PLIST_TEMPLATE.format(label=label, python=python, script=script, log=log, error_log=log)


def bootstrap_command(plist_path):
    return f"launchctl bootstrap gui/{os.getuid()} {plist_path}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the capacity-loop launchd plist.")
    parser.add_argument("--out-dir", required=True, help="directory to write the plist into")
    parser.add_argument("--label", default=LABEL)
    parser.add_argument("--python", default=sys.executable, help="interpreter that runs the loop")
    parser.add_argument(
        "--script", default=str(DEFAULT_DIR / "capacity_loop.py"), help="capacity_loop.py path"
    )
    parser.add_argument(
        "--log", default=str(DEFAULT_DIR / "capacity-loop.log"), help="agent log path"
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    plist_path = out_dir / (args.label + ".plist")
    plist_path.write_text(render(args.label, args.python, args.script, args.log))
    print(f"wrote {plist_path}")
    print(bootstrap_command(plist_path))


if __name__ == "__main__":
    main()
