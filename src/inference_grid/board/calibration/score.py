"""Scoring one eval case against a lane's attempt: one record for both kinds.

A review case is graded on what the reviewer found: recall of the seeded defects and the
findings that recall none (false positives), accepted when every defect is recalled and
none is spurious — the arithmetic `board/calibration.py` has always used, moved here. A
packet case is graded on what the lane built: every file in reference/reference_tests/ is
copied over the lane's tree at the packet branch head and run, accepted when each passes
and the lane's diff touches none of them, `repairs` counting the ones that fail.

Both kinds return the same record — `{kind, accepted, recalled, false_positives, repairs,
notes}` — so one outcome path (`record_outcome`, category `eval:<kind>`) writes the
scorecard row `(family, model, eval:<kind>)`.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from ..runner import parse_review

# The lane's tree inside a packet attempt: <attempt>/work is the scratch clone the loop
# branched and committed to.
LANE_TREE = "work"
# The reviewer's settled reply inside a review attempt.
REPLY = ("artifacts", "reply.txt")
# A reference test is one file, run on its own: exit 0 passes it, anything else repairs it.
REFERENCE_TIMEOUT_SECONDS = 120


def _names_file(location, file):
    """True when a finding's location names the defect's file: the relative path or its
    basename on word boundaries ("src/api/routes/views.py (handle_views)" matches both)."""
    location = str(location).replace("\\", "/").lower()
    file = str(file).lower()
    if file in location:
        return True
    base = re.escape(PurePosixPath(file).name)
    return re.search(r"(?<![a-z0-9._/-])" + base + r"(?![a-z0-9._-])", location) is not None


def _finding_text(finding):
    return " ".join(str(v) for v in finding.values() if isinstance(v, str)).lower()


def _score_findings(findings, answer):
    """Classify findings against one answer key; returns (recalled ids, false positives).

    A defect is recalled when some finding names its file AND the finding's text carries
    every must_mention string case-insensitively. A finding that recalls no defect is a
    false positive — on a clean case every finding is one.
    """
    findings = [f for f in findings if isinstance(f, dict)]
    matched = [False] * len(findings)
    recalled = []
    for defect in answer["defects"]:
        hit = False
        for index, finding in enumerate(findings):
            if _names_file(finding.get("location"), defect["file"]) and all(
                m.lower() in _finding_text(finding) for m in defect["must_mention"]
            ):
                matched[index] = hit = True
        if hit:
            recalled.append(defect["id"])
    return recalled, findings, matched.count(False)


def review_reply(attempt_dir):
    """The reviewer's reply path inside an attempt directory, or None without one."""
    if attempt_dir is None:
        return None
    return Path(attempt_dir).joinpath(*REPLY)


def _score_review(case, attempt_dir):
    answer = case["answer"]
    reply = review_reply(attempt_dir)
    found = reply is not None and reply.is_file()
    findings, note = [], ""
    if not found:
        note = "no settled reply found"
    else:
        try:
            review = parse_review(reply)
            findings = review.get("findings")
            if not isinstance(findings, list):
                findings = []
        except Exception as exc:
            note = f"reply unreadable: {exc}"[:200]
    recalled, findings, false_positives = _score_findings(findings, answer)
    total = len(answer["defects"])
    accepted = found and len(recalled) == total and false_positives == 0
    return {
        "kind": "review",
        "accepted": accepted,
        "recalled": len(recalled),
        "false_positives": false_positives,
        "repairs": 0,
        "notes": note,
    }


def _read_tree(root):
    """Every file under root as {relative: bytes}; a nested .git is skipped."""
    tree = {}
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        tree[relative.as_posix()] = path.read_bytes()
    return tree


def _touched(base, lane):
    """The paths where the lane's tree differs from the base tree (add, change, delete)."""
    touched = set(base) ^ set(lane)
    for relative in set(base) & set(lane):
        if base[relative] != lane[relative]:
            touched.add(relative)
    return touched


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def run_reference_test(argv, cwd=None, env=None):
    """The seam: run one reference test, returning its exit code (the real subprocess)."""
    done = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, timeout=REFERENCE_TIMEOUT_SECONDS
    )
    return done.returncode


def _score_packet(case, attempt_dir, run):
    tests = case["reference_tests"]
    base = dict(case["base"])
    root = None if attempt_dir is None else Path(attempt_dir) / LANE_TREE
    if root is None or not root.is_dir():
        # Nothing to grade: no lane tree means no reference test could have been repaired.
        return {
            "kind": "packet",
            "accepted": False,
            "recalled": 0,
            "false_positives": 0,
            "repairs": len(tests),
            "notes": "no lane tree found",
        }
    lane = _read_tree(root)
    test_paths = [relative for relative, _ in tests]
    edited = sorted(path for path in test_paths if path in _touched(base, lane))
    passed = repairs = 0
    with tempfile.TemporaryDirectory(prefix="eval-packet-") as scratch_name:
        scratch = Path(scratch_name)
        for relative, data in lane.items():
            _write(scratch / relative, data)
        for relative, data in tests:
            _write(scratch / relative, data)
        env = dict(os.environ, PYTHONPATH=str(scratch), PYTHONDONTWRITEBYTECODE="1")
        for relative in test_paths:
            if run([sys.executable, str(scratch / relative)], scratch, env) == 0:
                passed += 1
            else:
                repairs += 1
    notes = ""
    if edited:
        notes = "the lane's diff touches reference test file(s): " + ", ".join(edited)
    return {
        "kind": "packet",
        "accepted": repairs == 0 and not edited,
        "recalled": passed,
        "false_positives": len(edited),
        "repairs": repairs,
        "notes": notes,
    }


def score(case, attempt_dir, run=None):
    """Score one eval case against the lane's attempt; one record for both kinds.

    `attempt_dir` is the packet attempt directory: a review case reads
    artifacts/reply.txt from it, a packet case reads the lane's tree at work/ inside it
    and runs the reference tests there. `run` is the reference-test seam
    (`(argv, cwd, env) -> exit code`), the real subprocess by default.
    """
    kind = case.get("kind") if isinstance(case, dict) else None
    if kind == "review":
        return _score_review(case, attempt_dir)
    if kind == "packet":
        return _score_packet(case, attempt_dir, run or run_reference_test)
    raise ValueError("case kind must be one of ['review', 'packet']")


def record_outcome(ledger, attempt, record):
    """Record one eval outcome through the ledger's existing outcome path.

    The category is `eval:<kind>`, so the scorecard row is `(family, model, eval:<kind>)`
    — the identity the routing evidence and M5's nightly due-check read.
    """
    kind = record["kind"]
    note = record.get("notes") or (
        f"eval:{kind} accepted={record['accepted']} repairs={record['repairs']}"
    )
    return ledger.record_outcome(
        attempt,
        "eval:" + kind,
        record["accepted"],
        repairs=record["repairs"],
        note=note[:200],
    )
