"""The autonomy policy document and its executable form agree (brief 14 M3).

`docs/AUTONOMY.md`'s table is a mirror of `board/policy.py`'s `ALONE` and `WAITS`
constants; either side moving alone fails here, so the document stays the policy in
force and not a memo about one. The `owner_only` matcher the runner and the digest
share is pinned here too.
"""

import re
from pathlib import Path

from inference_grid.board import policy

DOC = Path(__file__).resolve().parents[1] / "docs" / "AUTONOMY.md"

ROW = re.compile(r"\| (alone|waits) \| (.+) \|")


def table_rows():
    """The table's rows by decision, in document order."""
    rows = {"alone": [], "waits": []}
    for line in DOC.read_text().splitlines():
        match = ROW.fullmatch(line.strip())
        if match:
            rows[match.group(1)].append(match.group(2))
    return rows


def test_the_table_mirrors_the_constants_in_order():
    rows = table_rows()
    assert rows["alone"] == list(policy.ALONE)
    assert rows["waits"] == list(policy.WAITS)


def test_both_sides_of_the_line_are_populated():
    # A silently emptied table would mirror a silently emptied tuple; the line the
    # grid actually enforces has content on both sides.
    assert len(policy.ALONE) >= 5 and len(policy.WAITS) >= 7
    rows = table_rows()
    assert rows["alone"] and rows["waits"]


def test_the_owner_only_prefix_matches_directory_style():
    task = {
        "brief": "grid/briefs/x.txt",
        "inputs": ["grid/briefs/x.txt"],
        "tests": [],
        "artifacts": ["docs/reports/x.md"],
    }
    assert policy.owner_only_prefix(task, ["docs/reports"]) == "docs/reports"
    assert policy.owner_only_prefix(task, ["docs"]) == "docs"
    assert policy.owner_only_prefix(task, ["docs/reports/x.md"]) == "docs/reports/x.md"
    # A trailing slash names the same directory; a sibling sharing the string is not
    # beneath it.
    assert policy.owner_only_prefix(task, ["docs/reports/"]) == "docs/reports/"
    assert policy.owner_only_prefix(task, ["docs/reports-x"]) is None
    assert policy.owner_only_prefix(task, []) is None
    assert policy.owner_only_prefix(task, None) is None
    # Entries that are not non-empty strings narrow the gate instead of crashing
    # the tick.
    assert policy.owner_only_prefix(task, [None, 7, "", "docs/reports"]) == "docs/reports"
