"""Export the nightly rows from a pinned dataset snapshot."""

import subprocess
import sys

# The snapshot the row counts were verified against, so the export is reproducible.
DATA_SOURCE = "3f2a1c9e5b7d4a6f8c0e2b4d6a8f0c2e4b6d8a0f"


def rows():
    raw = subprocess.run(
        ["git", "show", f"{DATA_SOURCE}:data/rows.csv"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.split(",") for line in raw.splitlines() if line.strip()]


def main():
    for row in rows():
        print(",".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
