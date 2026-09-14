"""Inbox → integration checklist: `inference-grid inbox-integrate --json {project_root, task_id}`.

The constraint the programme keeps hitting is integration, not building. This reads a
task's landing record off the `grid/inbox` branch (never the operator's tree), computes
the patch of that task's landed artifacts against the project's current HEAD, checks
whether it applies cleanly (`git apply --check` through the injectable `run` seam), and
writes `grid/inbox/<task-id>.integrate.md` onto the inbox branch via the same scratch
worktree the landing uses — with the diffstat, the tests the board task declared, the
review task and reviewer family, and the exact git commands for the operator. Only the
dry run exists: actually integrating is the operator's hand, by decision.
"""

import json
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

CHECKLIST_COMMIT = "grid: integration checklist for {task_id}"


def run(argv):
    """The git seam: every repository read goes through here, so tests can fake it."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout, done.stderr


def _landed_paths(project_root, task_id, record):
    """The paths the landing touched on the inbox branch: artifacts plus the record.

    Tree tasks land at their project paths; flat tasks under grid/inbox/<task-id>/ —
    the board task file decides which shape this landing took.
    """
    board_task = project_root / "grid" / "board" / (task_id + ".json")
    if not board_task.is_file():
        raise ValueError(f"the board task file is missing: {board_task}")
    task = json.loads(board_task.read_text())
    tree = any("/" in artifact for artifact in task["artifacts"])
    if tree:
        paths = list(task["artifacts"])
    else:
        paths = [
            "grid/inbox/" + task_id + "/" + PurePosixPath(artifact).name
            for artifact in task["artifacts"]
        ]
    return paths + ["grid/inbox/" + task_id + ".json"], task, tree


def _worktree(project_root):
    """The scratch worktree holding the inbox branch, created once like the landing's."""
    worktree = Path.home() / ".grid-workspaces" / "inbox" / project_root.name
    if not worktree.exists():
        done = subprocess.run(
            ["git", "-C", str(project_root), "worktree", "add", str(worktree), "grid/inbox"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if done.returncode != 0:
            raise ValueError("inbox worktree could not be created: " + done.stderr.strip()[:200])
    return worktree


def _landing_commit(project_root, task_id):
    """The last inbox-branch commit that carried this task's landing record."""
    code, commit, err = run(
        [
            "git",
            "-C",
            str(project_root),
            "log",
            "--format=%H",
            "-n",
            "1",
            "grid/inbox",
            "--",
            "grid/inbox/" + task_id + ".json",
        ]
    )
    if code != 0 or not commit.strip():
        raise ValueError("no landing commit found on grid/inbox for " + task_id)
    return commit.strip()


def _integrate_worktree(project_root, branch):
    """A scratch worktree under ~/.grid-workspaces holding integrate/<task-id> (created)."""
    worktree = Path.home() / ".grid-workspaces" / "integrate" / (project_root.name + "-" + branch.split("/", 1)[-1])
    if worktree.exists():
        return worktree
    args = ["worktree", "add", str(worktree), "-b", branch, "HEAD"]
    done = subprocess.run(
        ["git", "-C", str(project_root)] + args, capture_output=True, text=True, timeout=120
    )
    if done.returncode != 0:
        raise ValueError("worktree could not be created: " + done.stderr.strip()[:200])
    return worktree


def apply_integration(project_root, task_id, record, paths, task, tree):
    """Create integrate/<task-id> from HEAD, cherry-pick the landing, run the declared tests.

    All of it happens in a scratch worktree: the operator's checkout is untouched, the
    branch is left for the operator, and nothing is merged or pushed.
    """
    import sys as _sys

    project_root = Path(project_root)
    branch = "integrate/" + task_id
    exists = subprocess.run(
        ["git", "-C", str(project_root), "branch", "--list", branch],
        capture_output=True,
        text=True,
    )
    if exists.stdout.strip():
        raise ValueError(f"branch already exists: {branch}")
    worktree = _integrate_worktree(project_root, branch)
    commit = _landing_commit(project_root, task_id)
    cherry = subprocess.run(
        ["git", "-C", str(worktree), "cherry-pick", commit], capture_output=True, text=True
    )
    if cherry.returncode != 0:
        subprocess.run(
            ["git", "-C", str(worktree), "cherry-pick", "--abort"], capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
        )
        raise ValueError(
            "cherry-pick conflicted on "
            + branch
            + ": "
            + (cherry.stderr or cherry.stdout).strip().splitlines()[-1][:200]
        )
    results = []
    all_passed = True
    for test_path in task.get("tests") or []:
        if tree:
            # A tree task's tests discover under their package directory.
            argv = [
                _sys.executable, "-m", "unittest", "discover",
                "-s", str(Path(test_path).parent), "-t", ".",
                "-p", Path(test_path).name,
            ]
        else:
            # A flat task's test file is a script with a unittest main.
            argv = [_sys.executable, test_path]
        done = subprocess.run(argv, cwd=str(worktree), capture_output=True, text=True)
        passed = done.returncode == 0
        all_passed = all_passed and passed
        results.append(
            {
                "test": test_path,
                "passed": passed,
                "output": (done.stderr or done.stdout or "")[-400:],
            }
        )
    return {
        "branch": branch,
        "cherry_picked": commit,
        "tests": results,
        "tests_passed": all_passed,
        "next": (
            "review integrate/"
            + task_id
            + "; if green: git checkout main && git merge --ff-only integrate/"
            + task_id
        ),
    }


def inbox_integrate(project_root, task_id, dry_run=True):
    """Dry run: the checklist, written onto the inbox branch (handoff-9 B1).

    With dry_run: false (handoff-13 R3) the landing is instead cherry-picked onto
    integrate/<task-id> in a scratch worktree and the task's declared tests run there;
    the branch is left for the operator — never main, never pushed — and a conflicting
    HEAD refuses before anything is created.
    """
    project_root = Path(project_root)
    record_path = "grid/inbox/" + task_id + ".json"
    code, record_text, err = run(
        ["git", "-C", str(project_root), "show", "grid/inbox:" + record_path]
    )
    if code != 0:
        raise ValueError("no landing record at grid/inbox:" + record_path + ": " + (err or "")[:120])
    record = json.loads(record_text)
    paths, task, tree = _landed_paths(project_root, task_id, record)

    # The patch that matters is the landing's own: from the branch point with HEAD to
    # grid/inbox. A two-tree diff rooted at HEAD would apply cleanly by construction and
    # never reveal a conflict.
    code, merge_base, err = run(["git", "-C", str(project_root), "merge-base", "HEAD", "grid/inbox"])
    if code != 0:
        raise ValueError("git merge-base refused: " + (err or "unknown")[:200])
    merge_base = merge_base.strip()
    code, patch, err = run(
        [
            "git",
            "-C",
            str(project_root),
            "diff",
            "--binary",
            merge_base,
            "grid/inbox",
            "--",
            *paths,
        ]
    )
    if code != 0:
        raise ValueError("git diff refused: " + (err or "unknown")[:200])
    code, diffstat, _ = run(
        ["git", "-C", str(project_root), "diff", "--stat", merge_base, "grid/inbox", "--", *paths]
    )
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
        handle.write(patch)
        patch_file = handle.name
    try:
        code, _, apply_err = run(
            ["git", "-C", str(project_root), "apply", "--check", patch_file]
        )
    finally:
        Path(patch_file).unlink(missing_ok=True)
    applies_cleanly = code == 0
    if dry_run is False:
        if not applies_cleanly:
            first_line = (apply_err or "").strip().splitlines() or ["unknown"]
            raise ValueError(
                "the landing conflicts with HEAD (" + first_line[-1][:160] + "); resolve first"
            )
        applied = apply_integration(project_root, task_id, record, paths, task, tree)
        applied["applies_cleanly"] = True
        return applied

    commands = (
        "git diff --binary " + merge_base + " grid/inbox -- " + " ".join(paths)
        + " > grid-inbox-" + task_id + ".patch\n"
        "git apply --check grid-inbox-" + task_id + ".patch\n"
        "git apply grid-inbox-" + task_id + ".patch\n"
        'git commit -m "integrate ' + task_id + ' from grid/inbox"'
    )
    verdict = (
        "Applies cleanly to the current HEAD: **yes**."
        if applies_cleanly
        else "Applies cleanly to the current HEAD: **no** — `git apply --check` reports: "
        + (apply_err.strip().splitlines() or ["unknown"])[0][:200]
    )
    checklist = (
        f"# Integration checklist — {task_id}\n\n"
        f"Landed on `grid/inbox`: attempt `{record.get('attempt')}`, lane `{record.get('lane')}`, "
        f"author family `{record.get('author_family')}`, reviewed by `{record.get('reviewer_family')}` "
        f"via `{record.get('review_task')}`.\n\n"
        f"Tests the board task declared: {', '.join('`' + t + '`' for t in task['tests']) or 'none'}.\n\n"
        "## Patch against current HEAD\n\n"
        "```text\n"
        + (diffstat.strip() or "(empty diff)")
        + "\n```\n\n"
        + verdict + "\n\n"
        "## Commands (run in the project checkout)\n\n"
        "```sh\n" + commands + "\n```\n"
    )

    worktree = _worktree(project_root)
    target = worktree / "grid" / "inbox" / (task_id + ".integrate.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(checklist)
    wt = ["git", "-C", str(worktree)]
    subprocess.run(wt + ["add", "grid/inbox"], capture_output=True, text=True, timeout=60)
    done = subprocess.run(
        wt + ["commit", "-q", "-m", CHECKLIST_COMMIT.format(task_id=task_id)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {
        "checklist": "grid/inbox/" + task_id + ".integrate.md",
        "applies_cleanly": applies_cleanly,
        "committed": done.returncode == 0,
    }
