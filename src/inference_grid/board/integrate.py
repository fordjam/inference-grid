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
    if any("/" in artifact for artifact in task["artifacts"]):
        paths = list(task["artifacts"])
    else:
        paths = [
            "grid/inbox/" + task_id + "/" + PurePosixPath(artifact).name
            for artifact in task["artifacts"]
        ]
    return paths + ["grid/inbox/" + task_id + ".json"], task


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


def inbox_integrate(project_root, task_id, dry_run=True):
    """Write the integration checklist for one landed task onto the inbox branch."""
    if dry_run is False:
        raise ValueError("not implemented: only the dry-run checklist exists; integration is the operator's hand")
    project_root = Path(project_root)
    record_path = "grid/inbox/" + task_id + ".json"
    code, record_text, err = run(
        ["git", "-C", str(project_root), "show", "grid/inbox:" + record_path]
    )
    if code != 0:
        raise ValueError("no landing record at grid/inbox:" + record_path + ": " + (err or "")[:120])
    record = json.loads(record_text)
    paths, task = _landed_paths(project_root, task_id, record)

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
