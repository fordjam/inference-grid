"""Eval cases: one directory, one kind, loaded and validated.

An eval case is a directory holding a brief and, by kind, its grading material. A
`review` case is the calibration corpus's long-standing shape — brief.txt, diff.patch
and answer.json plus the changed files under their relative paths — and is graded on
what a reviewer found (recall of the seeded defects, false positives). A `packet` case
adds `reference/`: brief.txt and the repository the lane starts from (base/), plus the
hidden answer key — reference/reference.patch (the reference fix) and
reference/reference_tests/ (the reference suite). The lane sees the brief and the
repository at base, never reference/; `score.py` copies the reference tests over the
lane's tree and runs them.

`case.json` declares the kind (`{"kind": "review"}` or `{"kind": "packet"}`); it is
optional and defaults to review, so a corpus written before the kinds existed — the
operator's private one — still loads unchanged. Everything a lane could see passes the
board's input guard (credential names and content are refused), a review case's diff
must touch every file its answer names, and two staged files may not share a basename
(packets are flat: the runner copies inputs into one scratch directory by basename).
"""

import json
import re
from pathlib import Path, PurePosixPath

from ..branch_review import _safe_rel
from ..guard import check_input, check_name

# Case-id charset: ids become task ids `calib-<run_id>-<case>`, so both parts are constrained.
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
CASE_FILE = "case.json"
KINDS = ("review", "packet")
SEVERITY_WEIGHTS = {"high": 3, "medium": 2, "low": 1}
DEFECT_KEYS = {"id", "file", "must_mention", "severity", "note"}
REFERENCE = "reference"
REFERENCE_PATCH = "reference.patch"
REFERENCE_TESTS = "reference_tests"
# The file blocks of a unified diff, on both sides of each rename pair.
DIFF_PATH = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


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


def _sorted_case_dirs(corpus_dir):
    return sorted(p for p in corpus_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def _declared_kind(case, case_dir):
    """The case's kind from case.json; absent means review (the pre-kinds corpus)."""
    path = case_dir / CASE_FILE
    if not path.is_file():
        return "review"
    try:
        declared = json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"{case}: {CASE_FILE} is not valid JSON: {exc}") from exc
    if not isinstance(declared, dict) or set(declared) != {"kind"}:
        raise ValueError(f"{case}: {CASE_FILE} must hold exactly kind")
    if declared["kind"] not in KINDS:
        raise ValueError(f"{case}: kind must be one of {list(KINDS)}")
    return declared["kind"]


def _guard(case, label, data):
    problem = check_input(label, data)
    if problem:
        raise ValueError(f"{case}: {label}: {problem}")


def _load_review(case, case_dir):
    """A review case: exactly the calibration corpus's shape, plus the kind marker.

    Each case holds brief.txt, diff.patch and answer.json plus the changed files under
    their relative paths. The answer key is validated but never enters the staged file
    list (nor does case.json).
    """
    for required in ("brief.txt", "diff.patch", "answer.json"):
        if not (case_dir / required).is_file():
            raise ValueError(f"{case}: a case holds {required}")
    staged = []
    seen_basenames = set()
    for path in sorted(case_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(case_dir).as_posix()
        if relative in ("answer.json", "brief.txt", "diff.patch", CASE_FILE):
            continue
        if not _safe_rel(relative):
            raise ValueError(f"{case}: {relative} is not a safe relative path")
        if PurePosixPath(relative).name in seen_basenames:
            raise ValueError(f"{case}: {relative} shares a basename with another staged file")
        seen_basenames.add(PurePosixPath(relative).name)
        data = path.read_bytes()
        _guard(case, relative, data)
        staged.append((relative, data))
    brief = (case_dir / "brief.txt").read_bytes()
    patch = (case_dir / "diff.patch").read_bytes()
    _guard(case, "brief.txt", brief)
    _guard(case, "diff.patch", patch)
    try:
        answer = json.loads((case_dir / "answer.json").read_text())
    except ValueError as exc:
        raise ValueError(f"{case}: answer.json is not valid JSON: {exc}") from exc
    _validate_answer(case, answer, diff_paths(patch.decode("utf-8", "replace")))
    return {
        "case": case,
        "kind": "review",
        "brief": brief.decode("utf-8"),
        "files": staged,
        "diff": patch.decode("utf-8"),
        "answer": answer,
    }


def _tree(case, root, label):
    """Every file under root as (relative, bytes), guard-checked; a nested .git is skipped."""
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        posix = relative.as_posix()
        if not _safe_rel(posix):
            raise ValueError(f"{case}: {label}{posix} is not a safe relative path")
        denied = check_name(posix)
        if denied:
            raise ValueError(f"{case}: {label}{posix}: {denied}")
        data = path.read_bytes()
        _guard(case, label + posix, data)
        files.append((posix, data))
    return files


def _load_packet(case, case_dir):
    """A packet case: the brief and the repository at base, plus the hidden reference/.

    The reference tests are repo-relative: reference/reference_tests/tests/x.py is
    copied over the lane's tree as tests/x.py, so the lane's diff is judged on the same
    paths the suite runs at.
    """
    if not (case_dir / "brief.txt").is_file():
        raise ValueError(f"{case}: a packet case holds brief.txt")
    base_dir = case_dir / "base"
    reference_dir = case_dir / REFERENCE
    tests_dir = reference_dir / REFERENCE_TESTS
    patch_path = reference_dir / REFERENCE_PATCH
    for label, path in (
        ("base/", base_dir),
        ("reference/", reference_dir),
        ("reference/reference_tests/", tests_dir),
    ):
        if not path.is_dir():
            raise ValueError(f"{case}: a packet case holds {label}")
    if not patch_path.is_file():
        raise ValueError(f"{case}: a packet case holds reference/{REFERENCE_PATCH}")
    base = _tree(case, base_dir, "base/")
    if not base:
        raise ValueError(f"{case}: base/ holds no files")
    tests = _tree(case, tests_dir, "reference/reference_tests/")
    if not tests:
        raise ValueError(f"{case}: reference/reference_tests/ holds no files")
    if not patch_path.read_bytes().strip():
        raise ValueError(f"{case}: reference/{REFERENCE_PATCH} is empty")
    brief = (case_dir / "brief.txt").read_bytes()
    patch = patch_path.read_bytes()
    _guard(case, "brief.txt", brief)
    _guard(case, f"reference/{REFERENCE_PATCH}", patch)
    return {
        "case": case,
        "kind": "packet",
        "brief": brief.decode("utf-8"),
        "base": base,
        "reference_patch": patch.decode("utf-8"),
        "reference_tests": tests,
    }


def load_case(case_dir):
    """Load and validate one eval case directory; returns a case record.

    A review record carries `brief`, `files`, `diff` and `answer`; a packet record
    carries `brief`, `base`, `reference_patch` and `reference_tests`. Reference material
    never appears in a review record, and never in a packet case's `base`.
    """
    case_dir = Path(case_dir)
    if not case_dir.is_dir():
        raise ValueError(f"eval case is not a directory: {case_dir}")
    case = case_dir.name
    if not NAME.fullmatch(case):
        raise ValueError(f"{case}: case names must match [a-z0-9-]")
    kind = _declared_kind(case, case_dir)
    if kind == "review":
        return _load_review(case, case_dir)
    return _load_packet(case, case_dir)


def load_corpus(corpus_dir):
    """Load and validate every case under corpus_dir; returns case records in name order.

    Both kinds load. `author_calibration` authors the review cases; a packet case needs
    the evals path (M5) and is reported as skipped rather than staged as a review.
    """
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.is_dir():
        raise ValueError(f"calibration corpus is not a directory: {corpus_dir}")
    cases = [load_case(case_dir) for case_dir in _sorted_case_dirs(corpus_dir)]
    if not cases:
        raise ValueError(f"calibration corpus holds no cases: {corpus_dir}")
    return cases
