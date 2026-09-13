"""Author an independent_review task from a git range: `board-new --json {review_branch: ...}`.

Cross-family review of a branch in another repository without hand-authoring: the module
reads the range through one injectable `run` seam (changed paths, commit list with
trailers, tip contents, the diff), refuses credential paths (board.guard) and anything
outside a small allow-list, stages the tip versions plus a generated diff.patch under
the board's review staging, and writes the task, brief and schema test like every other
review. author_family comes from the commits' Co-Authored-By trailers (one mapping
constant): mixed families refuse, no trailer means claude. Everything here is read-only
over the reviewed repository.
"""

import json
import re
import subprocess
from pathlib import Path, PurePosixPath

from .guard import check_input, check_name
from .new import SCHEMA_TEST
from .runner import REVIEW_BUDGET, review_brief_text
from .task import validate_task

# Paths a review may stage, relative to the repository root; the coordinator extends it.
ALLOWED_PREFIXES = ("src/", "api/", "web/", "tests/", "tools/", "docs/", "scripts/", "grid/")

# Co-Authored-By trailer name → the family select_lane uses for cross-family exclusion.
TRAILER_FAMILIES = {
    "GLM-5.3-Flash": "glm",
    "Claude": "claude",
    "Codex": "openai",
    "GPT": "openai",
    "Kimi": "kimi",
    "DeepSeek": "deepseek",
}


def run(argv):
    """The git seam: every repository read goes through here, so tests can fake it.

    Output is decoded tolerantly; a `git show` of a non-UTF-8 file yields replacement
    characters, which the staging step refuses like any other binary input.
    """
    done = subprocess.run(argv, capture_output=True, timeout=120)
    return (
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


def _safe_rel(path):
    relative = PurePosixPath(path)
    return not relative.is_absolute() and all(
        part not in ("", ".", "..") for part in relative.parts
    )


def parse_commits(log):
    """Commit records from the seam's `git log --format=%H%n%an%n%s%n%(trailers)` output."""
    commits = []
    current = None
    section = 0
    for line in log.splitlines():
        if re.fullmatch(r"[0-9a-f]{40}", line):
            current = {"hash": line, "author": "", "subject": "", "trailers": []}
            commits.append(current)
            section = 0
            continue
        if current is None:
            continue
        if section == 0:
            current["author"] = line
            section = 1
        elif section == 1:
            current["subject"] = line
            section = 2
        elif line.strip() and ":" in line:
            current["trailers"].append(line.strip())
    return commits


def trailer_family(trailer):
    """The mapped family of one Co-Authored-By trailer, or None when unmapped."""
    value = trailer.split(":", 1)[1].strip() if ":" in trailer else ""
    for name, family in TRAILER_FAMILIES.items():
        if value.startswith(name):
            return family
    return None


def commits_family(commits):
    """The range's author family from its trailers; claude when none is recorded."""
    families = {}
    for commit in commits:
        for trailer in commit["trailers"]:
            if not trailer.lower().startswith("co-authored-by:"):
                continue
            family = trailer_family(trailer)
            if family is not None:
                families[family] = families.get(family, 0) + 1
    if len(families) > 1:
        raise ValueError(
            "the range carries mixed Co-Authored-By families: " + ", ".join(sorted(families))
        )
    return next(iter(families), "claude")


def review_branch(board_dir, project_root, spec, lanes=None, budget=None):
    """Author one independent_review task judging a git range; returns the created paths."""
    if not isinstance(spec, dict) or not all(
        isinstance(spec.get(key), str) and spec.get(key) for key in ("repo", "base", "tip")
    ):
        raise ValueError("review_branch needs repo, base and tip")
    repo, base, tip = spec["repo"], spec["base"], spec["tip"]

    code, changed, err = run(["git", "-C", repo, "diff", "--name-only", f"{base}..{tip}"])
    if code != 0:
        raise ValueError("git diff refused the range: " + (err or "unknown")[:200])
    paths = [line for line in changed.splitlines() if line.strip()]
    if not paths:
        raise ValueError("the range changes no files; nothing to review")
    for path in paths:
        if not _safe_rel(path):
            raise ValueError(f"{path} is not a safe relative path")
        if not path.startswith(ALLOWED_PREFIXES):
            raise ValueError(
                f"{path} is outside the review allow-list: " + ", ".join(ALLOWED_PREFIXES)
            )
        denied = check_name(path)
        if denied:
            raise ValueError(f"{path}: {denied}")

    code, log, err = run(
        ["git", "-C", repo, "log", "--format=%H%n%an%n%s%n%(trailers)", f"{base}..{tip}"]
    )
    if code != 0:
        raise ValueError("git log refused the range: " + (err or "unknown")[:200])
    commits = parse_commits(log)
    family = commits_family(commits)

    repo_name = re.sub(r"[^a-z0-9-]+", "-", PurePosixPath(repo).name.lower()).strip("-")
    short = re.sub(r"[^a-z0-9-]+", "", tip.lower())[:7]
    task_id = f"review-{repo_name}-{short}"[:60]
    board_dir = Path(board_dir)
    project_root = Path(project_root)
    task_file = board_dir / (task_id + ".json")
    if task_file.exists():
        raise FileExistsError(f"task file already exists: {task_file}")
    stage = board_dir / "review" / task_id
    if stage.exists() and any(stage.rglob("*")):
        raise FileExistsError(f"review staging already exists: {stage}")

    staged = []
    contents = {}
    for path in paths:
        code, content, err = run(["git", "-C", repo, "show", f"{tip}:{path}"])
        if code != 0:
            raise ValueError(f"git show refused {path}: " + (err or "unknown")[:200])
        if "\x00" in content or "\ufffd" in content:
            raise ValueError(f"{path}: binary input is not staged")
        problem = check_input(path, content.encode())
        if problem:
            raise ValueError(f"{path}: {problem}")
        contents[path] = content.encode()
        staged.append(f"grid/board/review/{task_id}/{path}")
    code, diff, err = run(["git", "-C", repo, "diff", f"{base}..{tip}"])
    if code != 0:
        raise ValueError("git diff refused the range: " + (err or "unknown")[:200])
    problem = check_input("diff.patch", diff.encode())
    if problem:
        raise ValueError(f"diff.patch: {problem}")
    staged.append(f"grid/board/review/{task_id}/diff.patch")

    brief_rel = f"grid/briefs/{task_id}.txt"
    subjects = "; ".join(commit["subject"] for commit in commits if commit["subject"])
    context = (
        f"The work under review is the git range {base}..{tip} ({len(commits)} commit(s): "
        f"{subjects}). The changed files are staged at their repository paths beside this "
        "brief, and the full diff is staged as diff.patch; every finding must quote the "
        "diff or the staged file next to the requirement it violates."
    )
    task = {
        "id": task_id,
        "category": "independent_review",
        "brief": brief_rel,
        "inputs": [brief_rel] + staged,
        "tests": ["grid/tests/test_review_schema.py"],
        "artifacts": ["reply.txt"],
        "lanes": lanes or ["go"],
        "author_family": family,
        "budget": budget or dict(REVIEW_BUDGET),
        "state": "ready",
        "blocked_reason": None,
    }
    task = validate_task(task)

    stage.mkdir(parents=True, exist_ok=True)
    for path, data in contents.items():
        target = stage / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (stage / "diff.patch").write_text(diff)
    schema_test = board_dir.parent / "tests" / "test_review_schema.py"
    if not schema_test.exists():
        schema_test.parent.mkdir(parents=True, exist_ok=True)
        schema_test.write_text(SCHEMA_TEST)
    brief_path = project_root / brief_rel
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(review_brief_text(task, context))
    task_file.write_text(json.dumps(task, indent=1) + "\n")
    return {
        "id": task_id,
        "task": str(task_file),
        "brief": str(brief_path),
        "staged": staged,
        "author_family": family,
        "commits": len(commits),
    }
