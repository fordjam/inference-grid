"""Author independent_review tasks from a git range: `board-new --json {review_branch: ...}`.

Cross-family review of a branch in another repository without hand-authoring: the module
reads the range through one injectable `run` seam (changed paths, commit list with
trailers, tip contents, the diffs), refuses credential paths (board.guard) and anything
outside a small allow-list, and stages tip versions plus the diff patch under the board's
review staging. Packets are budgeted (the working reviewer takes ~24k tokens proven, so a
91-file / 976 KB packet is refused, not dispatched): generated and locked content is
never staged, files over the per-file cap are represented by their hunks in the patch
alone, and a range over budget is split into one task per commit — each packet measured
before anything is written. author_family comes from the commits' Co-Authored-By trailers
(one mapping constant): mixed families refuse, no trailer means claude. Everything here
is read-only over the reviewed repository.
"""

import fnmatch
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

from .guard import check_input, check_name
from .new import SCHEMA_TEST
from .runner import REVIEW_BUDGET, review_brief_text
from .task import validate_task

# Paths a review may stage, relative to the repository root; the coordinator extends it.
ALLOWED_PREFIXES = (".gitignore", "requirements-test.txt", "requirements.txt", "pyproject.toml", "src/", "api/", "web/", "tests/", "tools/", "docs/", "scripts/", "grid/")

# Co-Authored-By trailer name → the family select_lane uses for cross-family exclusion.
TRAILER_FAMILIES = {
    "GLM-5.3-Flash": "glm",
    "Claude": "claude",
    "Codex": "openai",
    "GPT": "openai",
    "Kimi": "kimi",
    "DeepSeek": "deepseek",
}

# The packet budget: staged bytes (files under the per-file cap plus the diff), sized to
# roughly 30k tokens — about what the only proven reviewer (kimi-k3 at high) can take.
MAX_INPUT_BYTES = 120_000
# A changed file above this is represented by its hunks in the patch alone.
MAX_FILE_BYTES = 16_000
# Generated or locked content is never staged; the brief names what was omitted.
EXCLUDED_NAMES = ("package-lock.json", "uv.lock")
EXCLUDED_PARTS = ("fixtures", "dist")
EXCLUDED_GLOBS = ("*.min.*",)


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


def _generated(path, attrs):
    """True for content that is never staged: fixtures, locks, minified bundles, dist,
    and anything git attributes mark linguist-generated or -diff."""
    name = PurePosixPath(path).name
    if name in EXCLUDED_NAMES or any(part in EXCLUDED_PARTS for part in PurePosixPath(path).parts):
        return True
    if any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_GLOBS):
        return True
    marked = attrs.get(path, {})
    return marked.get("linguist-generated") in ("set", "true") or marked.get("diff") in (
        "unset",
        "false",
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


def _check_paths(paths):
    """The allow-list and credential-name gate, shared by the range and each commit."""
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


def _check_attr(repo, paths):
    """Which paths git attributes mark generated or -diff (the two attrs, not -a's noise)."""
    if not paths:
        return {}
    code, out, err = run(["git", "-C", repo, "check-attr", "linguist-generated", "diff", "--", *paths])
    if code != 0:
        raise ValueError("git check-attr refused: " + (err or "unknown")[:200])
    attrs = {}
    for line in out.splitlines():
        parts = line.split(": ")
        if len(parts) == 3:
            attrs.setdefault(parts[0], {})[parts[1]] = parts[2]
    return attrs


def _fetch(repo, tip, cache, path):
    """The tip version of one path, fetched once; binary content is refused."""
    if path not in cache:
        code, content, err = run(["git", "-C", repo, "show", f"{tip}:{path}"])
        if code != 0:
            raise ValueError(f"git show refused {path}: " + (err or "unknown")[:200])
        if "\x00" in content or "\ufffd" in content:
            raise ValueError(f"{path}: binary input is not staged")
        cache[path] = content
    return cache[path]


def _classify(repo, tip, cache, attrs, paths):
    """Split one packet's paths into staged contents, oversized, omitted — and the bytes.

    Nothing is written here: the caller measures the whole packet against the budget
    before any file lands on the board.
    """
    staged, oversized, omitted = [], [], []
    total = 0
    for path in paths:
        if _generated(path, attrs):
            omitted.append(path)
            continue
        content = _fetch(repo, tip, cache, path)
        problem = check_input(path, content.encode())
        if problem:
            raise ValueError(f"{path}: {problem}")
        data = content.encode()
        if len(data) > MAX_FILE_BYTES:
            oversized.append(path)
            continue
        staged.append((path, data))
        total += len(data)
    return staged, oversized, omitted, total


def _packet_notes(oversized, omitted):
    """The brief sentences naming what was omitted or reduced to hunks."""
    notes = ""
    if omitted:
        listed = ", ".join(omitted[:5]) + (", …" if len(omitted) > 5 else "")
        notes += (
            f" {len(omitted)} generated file(s) omitted from staging: {listed}; they were "
            "changed by this work but are not reviewable content."
        )
    if oversized:
        listed = ", ".join(oversized[:5]) + (", …" if len(oversized) > 5 else "")
        notes += (
            f" {len(oversized)} file(s) exceed the {MAX_FILE_BYTES}-byte per-file cap and are "
            f"represented by their hunks in diff.patch only: {listed}."
        )
    return notes


def _write_task(board_dir, project_root, task_id, family, lanes, budget, context, staged, patch):
    """Write one review task with its staging, brief and schema test; returns its dict."""
    board_dir = Path(board_dir)
    project_root = Path(project_root)
    task_file = board_dir / (task_id + ".json")
    if task_file.exists():
        raise FileExistsError(f"task file already exists: {task_file}")
    stage = board_dir / "review" / task_id
    if stage.exists() and any(stage.rglob("*")):
        raise FileExistsError(f"review staging already exists: {stage}")
    problem = check_input("diff.patch", patch.encode())
    if problem:
        raise ValueError(f"diff.patch: {problem}")
    task = {
        "id": task_id,
        "category": "independent_review",
        "brief": f"grid/briefs/{task_id}.txt",
        "inputs": [f"grid/briefs/{task_id}.txt"]
        + [f"grid/board/review/{task_id}/{relative}" for relative, _ in staged]
        + [f"grid/board/review/{task_id}/diff.patch"],
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
    for relative, data in staged:
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (stage / "diff.patch").write_text(patch)
    schema_test = board_dir.parent / "tests" / "test_review_schema.py"
    if not schema_test.exists():
        schema_test.parent.mkdir(parents=True, exist_ok=True)
        schema_test.write_text(SCHEMA_TEST)
    brief_path = project_root / task["brief"]
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(review_brief_text(task, context))
    task_file.write_text(json.dumps(task, indent=1) + "\n")
    return {
        "id": task_id,
        "task": str(task_file),
        "brief": str(brief_path),
        "staged": task["inputs"][1:],
        "author_family": family,
        "commits": 1,
    }


def review_branch(board_dir, project_root, spec, lanes=None, budget=None, max_input_bytes=None, split=None):
    """Author review task(s) judging a git range; one task, or one per commit when split.

    The whole-range packet is measured first: if it fits the byte budget a single task is
    written as before. Over budget, `split: "commit"` (also the fallback) authors one
    task per commit whose own packet fits, in range order; `split: "none"` refuses with
    the per-commit plan, and a commit whose packet alone exceeds the budget is refused
    with its file sizes.
    """
    if not isinstance(spec, dict) or not all(
        isinstance(spec.get(key), str) and spec.get(key) for key in ("repo", "base", "tip")
    ):
        raise ValueError("review_branch needs repo, base and tip")
    if split not in (None, "commit", "none"):
        raise ValueError("split must be 'commit' or 'none'")
    repo, base, tip = spec["repo"], spec["base"], spec["tip"]

    code, changed, err = run(["git", "-C", repo, "diff", "--name-only", f"{base}..{tip}"])
    if code != 0:
        raise ValueError("git diff refused the range: " + (err or "unknown")[:200])
    paths = [line for line in changed.splitlines() if line.strip()]
    if not paths:
        raise ValueError("the range changes no files; nothing to review")
    _check_paths(paths)

    code, log, err = run(
        ["git", "-C", repo, "log", "--format=%H%n%an%n%s%n%(trailers)", f"{base}..{tip}"]
    )
    if code != 0:
        raise ValueError("git log refused the range: " + (err or "unknown")[:200])
    commits = parse_commits(log)
    family = commits_family(commits)
    repo_name = re.sub(r"[^a-z0-9-]+", "-", PurePosixPath(repo).name.lower()).strip("-")
    limit = MAX_INPUT_BYTES if max_input_bytes is None else max_input_bytes
    board_dir = Path(board_dir)
    project_root = Path(project_root)
    cache, attrs = {}, _check_attr(repo, paths)

    def range_patch():
        code, diff, err = run(["git", "-C", repo, "diff", f"{base}..{tip}"])
        if code != 0:
            raise ValueError("git diff refused the range: " + (err or "unknown")[:200])
        return diff

    if split != "commit":
        staged, oversized, omitted, file_bytes = _classify(repo, tip, cache, attrs, paths)
        patch = range_patch()
        total = file_bytes + len(patch.encode())
        if total <= limit:
            subjects = "; ".join(commit["subject"] for commit in commits if commit["subject"])
            context = (
                f"The work under review is the git range {base}..{tip} ({len(commits)} "
                f"commit(s): {subjects}). The changed files are staged at their repository "
                "paths beside this brief, and the full diff is staged as diff.patch; every "
                "finding must quote the diff or the staged file next to the requirement it "
                "violates." + _packet_notes(oversized, omitted)
            )
            short = re.sub(r"[^a-z0-9-]+", "", tip.lower())[:7]
            created = _write_task(
                board_dir,
                project_root,
                f"review-{repo_name}-{short}"[:60],
                family,
                lanes,
                budget,
                context,
                staged,
                patch,
            )
            created["commits"] = len(commits)
            if omitted:
                created["omitted"] = omitted
            if oversized:
                created["oversized"] = oversized
            return created
        if split == "none":
            raise ValueError(
                f"the range packet is {total} bytes over the {limit}-byte budget; "
                "re-run per part with split: commit, whose plan is: "
                + _plan(repo_name, repo, tip, commits, cache, attrs)
            )

    # Split by commit: one packet per commit, in range order (oldest first), each
    # measured on its own.
    tasks = []
    for commit in reversed(commits):
        code, c_paths, err = run(
            ["git", "-C", repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit["hash"]]
        )
        if code != 0:
            raise ValueError("git diff-tree refused: " + (err or "unknown")[:200])
        commit_paths = [line for line in c_paths.splitlines() if line.strip()]
        _check_paths(commit_paths)
        staged, oversized, omitted, file_bytes = _classify(repo, tip, cache, attrs, commit_paths)
        code, patch, err = run(
            ["git", "-C", repo, "diff-tree", "-p", "--no-commit-id", "--root", commit["hash"]]
        )
        if code != 0:
            raise ValueError("git diff-tree refused: " + (err or "unknown")[:200])
        total = file_bytes + len(patch.encode())
        if total > limit:
            sizes = ", ".join(
                f"{relative} {len(data)} bytes" for relative, data in sorted(staged, key=lambda s: -len(s[1]))[:5]
            )
            raise ValueError(
                f"commit {commit['hash'][:7]} alone exceeds the {limit}-byte budget "
                f"({total} bytes: {sizes}); split the branch further"
            )
        code, body, _ = run(["git", "-C", repo, "log", "-1", "--format=%B", commit["hash"]])
        message = (body if code == 0 else commit["subject"]).strip()
        context = (
            f"The work under review is commit {commit['hash']} in {repo_name}, one part of a "
            f"split review of {base}..{tip}. The commit message is: {message}. The changed "
            "files are staged at their repository paths beside this brief, and the commit's "
            "diff is staged as diff.patch; every finding must quote the diff or the staged "
            "file next to the requirement it violates." + _packet_notes(oversized, omitted)
        )
        short = re.sub(r"[^a-z0-9-]+", "", commit["hash"].lower())[:7]
        tasks.append(
            _write_task(
                board_dir,
                project_root,
                f"review-{repo_name}-{short}"[:60],
                family,
                lanes,
                budget,
                context,
                staged,
                patch,
            )
        )
    if not tasks:
        raise ValueError("every commit packet was empty; nothing to review")
    return {"tasks": tasks}


def _plan(repo_name, repo, tip, commits, cache, attrs):
    """The per-commit split the refusal would make: id and byte size per packet, in order."""
    entries = []
    for commit in commits:
        code, c_paths, _ = run(
            [
                "git",
                "-C",
                repo,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "--root",
                commit["hash"],
            ]
        )
        total = 0
        for path in (line for line in c_paths.splitlines() if line.strip()):
            if not _generated(path, attrs):
                total += len(_fetch(repo, tip, cache, path).encode())
        code, patch, _ = run(
            ["git", "-C", repo, "diff-tree", "-p", "--no-commit-id", "--root", commit["hash"]]
        )
        total += len(patch.encode())
        entries.append(f"review-{repo_name}-{commit['hash'][:7]} ≈{total} bytes")
    return "; ".join(entries)
