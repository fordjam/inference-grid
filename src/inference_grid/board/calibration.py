"""Reviewer calibration: a corpus of review packets with known answers.

The board routes independent_review tasks by an acceptance rate that means "the
reviewer produced a well-formed verdict", not "the verdict was right". A calibration
corpus measures the difference: each case is a directory holding exactly what a review
packet holds (a diff.patch, the changed files under their relative paths, a brief.txt)
plus an answer.json that is never staged into the packet, naming the defects a correct
reviewer must find (file, must_mention keywords, severity) or `clean: true` to measure
false positives. `author_calibration` writes the cases onto a board as ordinary
independent_review tasks; `score_calibration` compares the settled replies against the
answer keys and reports recall, false positives and weighted recall per lane.
"""

import json
import re
from pathlib import Path, PurePosixPath

from .guard import check_input, check_name
from .branch_review import _safe_rel

# Task-id charset: ids are `calib-<run_id>-<case>`, so both parts are constrained.
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
# The file blocks of a unified diff, on both sides of each rename pair.
DIFF_PATH = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)

AUTHOR_FAMILY = "calibration"
SEVERITY_WEIGHTS = {"high": 3, "medium": 2, "low": 1}
DEFECT_KEYS = {"id", "file", "must_mention", "severity", "note"}


def diff_paths(patch):
    """Every path a unified diff touches, both the a/ and b/ side of each block."""
    paths = set()
    for match in DIFF_PATH.finditer(patch):
        paths.update(match.groups())
    return paths


def _validate_answer(case, answer, patch_paths):
    """The answer key must be well-formed and only name files the diff touches."""
    if not isinstance(answer, dict) or set(answer) != {"defects", "clean"}:
        raise ValueError(f"{case}: answer.json must hold exactly defects and clean")
    clean = answer["clean"]
    defects = answer["defects"]
    if not isinstance(clean, bool) or not isinstance(defects, list):
        raise ValueError(f"{case}: answer.json clean must be bool, defects a list")
    if clean and defects:
        raise ValueError(f"{case}: a clean case must have zero defects")
    seen = set()
    for defect in defects:
        if not isinstance(defect, dict) or set(defect) != DEFECT_KEYS:
            raise ValueError(f"{case}: each defect holds exactly {sorted(DEFECT_KEYS)}")
        if not isinstance(defect["id"], str) or not defect["id"] or defect["id"] in seen:
            raise ValueError(f"{case}: defect ids must be distinct non-empty strings")
        seen.add(defect["id"])
        file = defect["file"]
        if not isinstance(file, str) or not _safe_rel(file):
            raise ValueError(f"{case}: defect file must be a safe relative path")
        denied = check_name(file)
        if denied:
            raise ValueError(f"{case}: {file}: {denied}")
        if file not in patch_paths:
            raise ValueError(f"{case}: defect names {file}, which the diff does not touch")
        mentions = defect["must_mention"]
        if (
            not isinstance(mentions, list)
            or not mentions
            or not all(isinstance(m, str) and m for m in mentions)
        ):
            raise ValueError(f"{case}: must_mention must be a non-empty list of strings")
        if defect["severity"] not in SEVERITY_WEIGHTS:
            raise ValueError(f"{case}: severity must be one of {sorted(SEVERITY_WEIGHTS)}")
        if not isinstance(defect["note"], str) or not defect["note"]:
            raise ValueError(f"{case}: defect note must be a non-empty string")


def load_corpus(corpus_dir):
    """Load and validate every case under corpus_dir; returns case records in name order.

    Each case is a directory holding brief.txt, diff.patch and answer.json plus the
    changed files under their relative paths. Everything that would be staged passes the
    board's input guard (credential names and content are refused), the diff must touch
    every file an answer names, and two staged files may not share a basename (packets
    are flat: the runner copies inputs into one scratch directory by basename). The
    answer key itself is validated but never enters the staged file list.
    """
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.is_dir():
        raise ValueError(f"calibration corpus is not a directory: {corpus_dir}")
    cases = []
    for case_dir in sorted(p for p in corpus_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        name = case_dir.name
        if not NAME.fullmatch(name):
            raise ValueError(f"{name}: case names must match [a-z0-9-]")
        for required in ("brief.txt", "diff.patch", "answer.json"):
            if not (case_dir / required).is_file():
                raise ValueError(f"{name}: a case holds {required}")
        staged = []
        seen_basenames = set()
        for path in sorted(case_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(case_dir).as_posix()
            if relative in ("answer.json", "brief.txt", "diff.patch"):
                continue
            if not _safe_rel(relative):
                raise ValueError(f"{name}: {relative} is not a safe relative path")
            if PurePosixPath(relative).name in seen_basenames:
                raise ValueError(f"{name}: {relative} shares a basename with another staged file")
            seen_basenames.add(PurePosixPath(relative).name)
            data = path.read_bytes()
            problem = check_input(relative, data)
            if problem:
                raise ValueError(f"{name}: {relative}: {problem}")
            staged.append((relative, data))
        brief = (case_dir / "brief.txt").read_bytes()
        patch = (case_dir / "diff.patch").read_bytes()
        for label, data in (("brief.txt", brief), ("diff.patch", patch)):
            problem = check_input(label, data)
            if problem:
                raise ValueError(f"{name}: {label}: {problem}")
        try:
            answer = json.loads((case_dir / "answer.json").read_text())
        except ValueError as exc:
            raise ValueError(f"{name}: answer.json is not valid JSON: {exc}") from exc
        _validate_answer(name, answer, diff_paths(patch.decode("utf-8", "replace")))
        cases.append(
            {
                "case": name,
                "brief": brief.decode("utf-8"),
                "files": staged,
                "diff": patch.decode("utf-8"),
                "answer": answer,
            }
        )
    if not cases:
        raise ValueError(f"calibration corpus holds no cases: {corpus_dir}")
    return cases
