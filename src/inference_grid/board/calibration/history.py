"""Eval cases from the board's own history: a landed packet, a confirmed review (P1).

The calibration corpus was hand-made. The board has produced better material every day:
a landed packet is a packet case ready-made — the brief the lane was given, the
repository at the base it built from, the landed diff as the reference patch and the
tests it added as the reference suite — and a review whose findings were confirmed (a
rejection that became a fix packet, or a defect found later) is a review case with a
true answer key. Replaying those on a new lane is the question "would this model have
done what the landed one did?", scored by the same `score.py` the nightly evals use.

`packet_case_from_landed` writes `<corpus>/<name>/` from a `landed` board card:
base/ from `git archive <base_head>` restricted to the reviewable prefixes (never data
or vendored trees), reference/reference.patch from `git diff base..merge`, and
reference/reference_tests/ from the test files the range added or changed, at their
paths. `review_case_from_staging` copies a staged review packet and writes the answer
key the operator supplies (or the review's own findings when they were confirmed).
Both run the case through `load_case` before keeping it, so a case that would refuse
at eval time refuses now, and nothing that fails the input guard is written.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .case import REFERENCE, REFERENCE_PATCH, REFERENCE_TESTS, load_case

DEFAULT_PREFIXES = (
    "src/",
    "tests/",
    "scripts/",
    "deployments/",
    "docs/",
    "pyproject.toml",
    "requirements.txt",
    "requirements-test.txt",
    "requirements.lock",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    ".gitignore",
)
# A lane's tree must never carry these however the prefixes are set.
NEVER = ("node_modules/", ".venv/", "venv/", "data/", "research/series/", ".git/")
MAX_BASE_BYTES = 64 * 1024 * 1024
TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "git " + " ".join(args) + ": " + proc.stderr.decode("utf-8", "replace").strip()[:300]
        )
    return proc.stdout


def _keep(path: str, prefixes: Iterable[str]) -> bool:
    if any(path.startswith(n) or ("/" + n) in path for n in NEVER):
        return False
    return any(path == p or path.startswith(p) for p in prefixes)


def _archive_tree(
    repo: Path, rev: str, prefixes: Iterable[str], dest: Path, omitted: List[Dict[str, str]]
) -> int:
    """Extract `rev`'s files under the prefixes into dest; returns bytes written.
    Files the input guard refuses are skipped and appended to `omitted`."""
    from ..guard import check_input

    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "tree.tar"
        tar_path.write_bytes(_git(repo, "archive", "--format=tar", rev))
        written = 0
        with tarfile.open(tar_path) as tar:
            for member in tar.getmembers():
                if not member.isfile() or not _keep(member.name, prefixes):
                    continue
                target = dest / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                if b"\x00" in data[:8192]:
                    continue  # binary files are never staged inputs (the guard refuses them)
                problem = check_input(member.name, data)
                if problem:
                    # The repo's own guard tests carry fake key headers; a file the input
                    # guard would refuse at eval time is left out of the tree and named.
                    omitted.append({"path": member.name, "reason": problem})
                    continue
                target.write_bytes(data)
                written += len(data)
                if written > MAX_BASE_BYTES:
                    raise ValueError(
                        f"base tree exceeds {MAX_BASE_BYTES} bytes under the given prefixes"
                    )
    return written


def packet_case_from_landed(
    project_root,
    board_dir,
    task_id,
    corpus_dir,
    name=None,
    prefixes=DEFAULT_PREFIXES,
    brief_text=None,
) -> Dict[str, Any]:
    """Write a packet eval case from a landed packet task; returns {case, path, ...}."""
    project_root, board_dir, corpus_dir = Path(project_root), Path(board_dir), Path(corpus_dir)
    card = json.loads((board_dir / f"{task_id}.json").read_text())
    if card.get("category") != "packet" or card.get("state") != "landed":
        raise ValueError(f"{task_id} is not a landed packet task")
    landed = card.get("landed") or {}
    base_head, merge = landed.get("base_head"), landed.get("merge_commit")
    if not base_head or not merge:
        raise ValueError(f"{task_id}: landed record lacks base_head/merge_commit")
    name = name or re.sub(r"[^a-z0-9-]+", "-", f"{project_root.name}-{task_id}".lower()).strip("-")
    case_dir = corpus_dir / name
    if case_dir.exists():
        raise FileExistsError(f"case already exists: {case_dir}")
    prefixes = tuple(prefixes)
    changed = (
        _git(project_root, "diff", "--name-only", "--diff-filter=AM", f"{base_head}..{merge}")
        .decode()
        .split()
    )
    test_files = [p for p in changed if TEST_FILE.search(p) and _keep(p, prefixes)]
    if not test_files:
        raise ValueError(
            f"{task_id}: the landed range added or changed no test files under the prefixes; "
            "a packet case needs a reference suite"
        )
    patch = _git(project_root, "diff", f"{base_head}..{merge}", "--", *prefixes)
    if not patch.strip():
        raise ValueError(f"{task_id}: the landed range changes nothing under the prefixes")
    if brief_text is None:
        from ...lanes.brief import packet_text

        brief_rel = card["brief"]
        try:
            brief_doc = _git(project_root, "show", f"{merge}:{brief_rel}").decode(
                "utf-8", "replace"
            )
        except RuntimeError:
            brief_doc = (project_root / brief_rel).read_text()
        brief_text = packet_text(brief_doc, card["spec"]["packet_id"])
    corpus_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".authoring-", dir=str(corpus_dir)))
    work = scratch / name  # load_case validates the directory NAME too
    work.mkdir()
    try:
        (work / "brief.txt").write_text(brief_text.strip() + "\n")
        (work / "case.json").write_text(json.dumps({"kind": "packet"}) + "\n")
        # Provenance beside the case, not in case.json (whose schema is exactly `kind`).
        (work / "source.json").write_text(
            json.dumps(
                {
                    "project": project_root.name,
                    "task": task_id,
                    "base_head": base_head,
                    "merge_commit": merge,
                    "how": landed.get("how"),
                },
                indent=1,
            )
            + "\n"
        )
        omitted: List[Dict[str, str]] = []
        base_bytes = _archive_tree(project_root, base_head, prefixes, work / "base", omitted)
        if omitted:
            src = json.loads((work / "source.json").read_text())
            src["omitted_from_base"] = omitted
            (work / "source.json").write_text(json.dumps(src, indent=1) + "\n")
        ref = work / REFERENCE
        ref.mkdir()
        (ref / REFERENCE_PATCH).write_bytes(patch)
        for rel in test_files:
            target = ref / REFERENCE_TESTS / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_git(project_root, "show", f"{merge}:{rel}"))
        record = load_case(work)  # refuses credential-looking content before anything is kept
        work.rename(case_dir)
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    shutil.rmtree(scratch, ignore_errors=True)
    return {
        "case": name,
        "path": str(case_dir),
        "kind": "packet",
        "base_bytes": base_bytes,
        "reference_tests": test_files,
        "patch_bytes": len(patch),
        "files_in_base": len(record["base"]),
        "omitted_from_base": omitted,
    }


def review_case_from_staging(
    board_dir, review_id, corpus_dir, answer: Dict[str, Any], name=None
) -> Dict[str, Any]:
    """Write a review eval case from a staged review packet and a confirmed answer key.

    `answer` is the calibration corpus's answer.json: `{"defects": [{id, file,
    must_mention, severity, note}, ...], "clean": bool}` — supplied by the operator,
    typically the findings of a rejected review that a fix packet then confirmed, with
    `file` a path the staged diff touches. The review's staging (brief.txt, diff.patch,
    the changed files) is copied as-is; `case.py` validates the key before the case is kept.
    """
    board_dir, corpus_dir = Path(board_dir), Path(corpus_dir)
    stage = board_dir / "review" / review_id
    if not (stage / "diff.patch").is_file():
        raise ValueError(f"{review_id}: no staged review packet at {stage}")
    if not isinstance(answer, dict) or not (answer.get("defects") or answer.get("clean")):
        raise ValueError("a review case needs an answer key: {defects: [...], clean: bool}")
    name = name or re.sub(r"[^a-z0-9-]+", "-", review_id.lower()).strip("-")
    case_dir = corpus_dir / name
    if case_dir.exists():
        raise FileExistsError(f"case already exists: {case_dir}")
    corpus_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".authoring-", dir=str(corpus_dir)))
    work = scratch / name
    work.mkdir()
    try:
        for item in stage.iterdir():
            if item.name in ("source.json", "review.json"):
                continue
            if item.is_dir():
                shutil.copytree(item, work / item.name)
            else:
                shutil.copy2(item, work / item.name)
        brief_src = board_dir.parent / "briefs" / f"{review_id}.txt"
        if not (work / "brief.txt").is_file() and brief_src.is_file():
            shutil.copy2(brief_src, work / "brief.txt")
        (work / "answer.json").write_text(json.dumps(answer, indent=1) + "\n")
        (work / "case.json").write_text(json.dumps({"kind": "review"}) + "\n")
        (work / "source.json").write_text(json.dumps({"review": review_id}, indent=1) + "\n")
        record = load_case(work)
        work.rename(case_dir)
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    shutil.rmtree(scratch, ignore_errors=True)
    return {
        "case": name,
        "path": str(case_dir),
        "kind": "review",
        "files": len(record.get("files") or {}),
        "answers": len(answer.get("defects") or []),
    }
