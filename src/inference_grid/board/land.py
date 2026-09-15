"""land: a code node that lands a passed packet branch onto its base.

When a packet task settles `passed`, its branch sits at `packet/<task-id>` in the
project repository and nothing happens next — six of seventeen landings in briefs 14/15
needed the coordinator's hands. This node closes that: it proves the branch still merges
into the base with the task's own gates (board/verify_merge.py), then merges in a
worktree of the base — the worktree that owns the branch when one exists, a scratch
`git worktree` created for the landing otherwise — never the operator checkout. The two
append-only ledgers every packet touches (`docs/CONTRIBUTIONS.md`, `docs/LANES.md`) are
union-merged when they conflict (rows are independent); any other conflict blocks with
the file list. The gates run once more on the tree that will actually be committed, the
merge commit is kept on the base, the task gains `landed: {base_head, merge_commit,
how}` and settles `landed` — a terminal state after `passed`, validated here because
board/task.py is provider-authored — and the ledger records the event.

Landings are serialized per base with a lock directory under `packets_root`: one landing
at a time, a second arrival reports busy and leaves everything as it was.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from ..lanes.packet import Gate, run_gates
from .packet_task import validate_board_task
from .verify_merge import _git, verify_merge

# The only files a landing may union-merge: append-only ledgers whose rows are
# independent, so keeping both sides is always right.
UNION_FILES = ("docs/CONTRIBUTIONS.md", "docs/LANES.md")


def _gate_nodes(spec_gates):
    return [
        Gate(
            g["name"],
            list(g["argv"]),
            cwd=g.get("cwd", "."),
            timeout=g.get("timeout", 1800),
            env=dict(g.get("env") or {}),
        )
        for g in spec_gates
    ]


def _base_worktree(repo, base):
    """The worktree that has `base` checked out, if any."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout.decode()
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
        elif line == f"branch refs/heads/{base}":
            return path
    return None


def _safe_name(ref):
    return ref.replace("/", "-")


def _is_ancestor(repo, ancestor, descendant):
    return (
        _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0
    )


def _conflicted_paths(worktree):
    return (
        _git(worktree, "diff", "--name-only", "--diff-filter=U", check=False)
        .stdout.decode("utf-8", "replace")
        .split()
    )


def _union_resolve(worktree, path):
    """Resolve one conflicted path by keeping both sides, via git's own union merge."""
    stages = {}
    for stage, name in ((1, "base"), (2, "ours"), (3, "theirs")):
        proc = _git(worktree, "show", f":{stage}:{path}", check=False)
        stages[name] = proc.stdout if proc.returncode == 0 else b""
    scratch = Path(tempfile.mkdtemp(prefix="land-union-"))
    try:
        for name, data in stages.items():
            (scratch / name).write_bytes(data)
        # `git merge-file --union` concatenates both sides of every conflicting hunk;
        # 255 is its error code, anything else is a resolved union on stdout.
        proc = _git(scratch, "merge-file", "--union", "-p", "ours", "base", "theirs", check=False)
        if proc.returncode == 255:
            raise RuntimeError("git merge-file failed on " + path)
        (worktree / path).parent.mkdir(parents=True, exist_ok=True)
        (worktree / path).write_bytes(proc.stdout)
        _git(worktree, "add", "--", path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_gates(worktree, gates, log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    return [
        {"name": r.name, "ok": r.ok, "returncode": r.returncode, "tail": r.tail}
        for r in run_gates(worktree, _gate_nodes(gates), dict(os.environ), log_dir)
    ]


def _first_gate_failure(gate_results):
    failed = [g for g in gate_results if not g["ok"]]
    if not failed:
        return None
    return (
        f"gate {failed[0]['name']} failed (exit {failed[0]['returncode']}): "
        + failed[0]["tail"].strip()
    )[:300]


def _packet_attempt(ledger, task_id):
    """The newest completed ledger attempt for the board task, for the landing event."""
    rows = [
        r
        for r in ledger.status()
        if str(r.get("task", "")).startswith(task_id + "-") and r.get("state") == "completed"
    ]
    if not rows:
        return task_id
    return max(rows, key=lambda r: r.get("task", ""))["id"]


def land_packet(
    board_dir,
    project_root,
    task_id,
    base,
    gates,
    ledger,
    packets_root,
    dry_run=False,
):
    """Land `packet/<task-id>` onto `base`; returns the report the tick and CLI print.

    On success the task settles `landed` with `landed: {base_head, merge_commit, how}`;
    a landing that must not proceed (a conflict outside the union pair, a gate failure on
    the merged tree, a moved or dirty base) settles it `blocked` with the reason and
    leaves the base untouched. `dry_run` runs the read-only proof only — the verify
    merge with the gates — and reports what would land and why not, touching no task
    file, taking no lock and moving no ref.
    """
    report = {"task": task_id, "base": base}
    path = Path(board_dir) / (task_id + ".json")
    task = validate_board_task(json.loads(path.read_text()))
    if task["category"] != "packet":
        return dict(report, landed=False, reason="task is not a packet")
    if task["state"] != "passed":
        return dict(report, landed=False, reason=f"task state is {task['state']}, not passed")
    if task.get("landed"):
        return dict(report, landed=False, reason="already landed", landed_at=task["landed"])
    project_root = Path(project_root).resolve()
    branch = "packet/" + task_id
    try:
        branch_head = (
            _git(project_root, "rev-parse", "--verify", branch + "^{commit}")
            .stdout[:40]
            .decode()
            .strip()
        )
        base_head = (
            _git(project_root, "rev-parse", "--verify", base + "^{commit}")
            .stdout[:40]
            .decode()
            .strip()
        )
    except RuntimeError as exc:
        return dict(report, landed=False, reason=("ref not found: " + str(exc))[:200])

    # The proof: does the branch merge into the base, and do the gates hold on the
    # pristine merge? A conflict here is only survivable inside the union pair.
    verify = verify_merge(
        project_root, branch, base, gates, work_dir=Path(packets_root) / task_id / "land-verify"
    )
    if verify["mergeable"]:
        failed = _first_gate_failure(verify["gates"])
        if failed:
            return _block_or_report(report, path, task, dry_run, failed)
        how = "ff" if _is_ancestor(project_root, base_head, branch_head) else "merge"
    else:
        if not verify["conflicts"]:
            return _block_or_report(report, path, task, dry_run, "merge did not complete")
        if any(c not in UNION_FILES for c in verify["conflicts"]):
            return _block_or_report(
                report, path, task, dry_run, "merge conflicts: " + ", ".join(verify["conflicts"])
            )
        how = "union"
    report["how"] = how

    if dry_run:
        return dict(report, dry_run=True, would_land=True, branch_head=branch_head)

    lock = Path(packets_root) / ("land-" + _safe_name(base) + ".lock")
    try:
        lock.mkdir(parents=True)
    except FileExistsError:
        return dict(report, landed=False, reason="landing busy: another landing holds the lock")
    try:
        (lock / "pid").write_text(str(os.getpid()))
        return _land_locked(
            report,
            path,
            task,
            project_root,
            base,
            base_head,
            branch_head,
            gates,
            how,
            ledger,
            packets_root,
        )
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _block_or_report(report, path, task, dry_run, reason):
    """A landing that must not proceed: block the task, or report why not on a dry run."""
    if dry_run:
        return dict(report, dry_run=True, would_land=False, reason=reason)
    from .runner import save_task

    save_task(path, task, state="blocked", blocked_reason=("landing blocked: " + reason)[:300])
    return dict(report, landed=False, blocked=True, reason=reason)


def _land_locked(
    report,
    path,
    task,
    project_root,
    base,
    base_head,
    branch_head,
    gates,
    how,
    ledger,
    packets_root,
):
    """The merge itself, under the lock: worktree of the base, gates, commit, settle."""
    from .runner import save_task

    wt = _base_worktree(project_root, base)
    created = False
    scratch = None
    if wt is None:
        # No worktree owns the base branch: land through a scratch worktree claimed for
        # this landing and removed after.
        scratch = Path(tempfile.mkdtemp(prefix="land-" + _safe_name(base) + "-"))
        wt = scratch / "wt"
        _git(project_root, "worktree", "add", "--quiet", str(wt), base)
        created = True
    try:
        dirty = _git(wt, "status", "--porcelain").stdout.decode()
        if dirty.strip():
            return _block_or_report(report, path, task, False, "base worktree is dirty")
        wt_head = _git(wt, "rev-parse", "HEAD").stdout[:40].decode().strip()
        if wt_head != base_head:
            return _block_or_report(report, path, task, False, "base moved since the verify")

        if how == "ff":
            # The base is an ancestor of the branch: the tree the gates just proved is
            # bit-identical to the branch head, so the fast-forward keeps it exactly.
            _git(wt, "merge", "--ff-only", branch_head)
            merge_commit = _git(wt, "rev-parse", "HEAD").stdout[:40].decode().strip()
        else:
            merge = _git(wt, "merge", "--no-commit", "--no-ff", branch_head, check=False)
            try:
                if merge.returncode != 0:
                    conflicts = _conflicted_paths(wt)
                    if any(c not in UNION_FILES for c in conflicts):
                        return _block_or_report(
                            report, path, task, False, "merge conflicts: " + ", ".join(conflicts)
                        )
                    # The append-only ledgers: keep both sides, the rows are independent.
                    for c in conflicts:
                        _union_resolve(wt, c)
                gate_results = _run_gates(
                    wt, gates, Path(packets_root) / report["task"] / "land-gates"
                )
                failed = _first_gate_failure(gate_results)
                if failed:
                    return _block_or_report(report, path, task, False, failed)
                moved = (
                    _git(project_root, "rev-parse", "--verify", base + "^{commit}")
                    .stdout[:40]
                    .decode()
                    .strip()
                    != base_head
                )
                if moved:
                    return _block_or_report(
                        report, path, task, False, "base moved during the landing"
                    )
                _git(wt, "commit", "--no-edit")
                merge_commit = _git(wt, "rev-parse", "HEAD").stdout[:40].decode().strip()
            finally:
                # A kept commit ends the merge; for every blocked path this aborts it.
                _git(wt, "merge", "--abort", check=False)
    finally:
        if created:
            _git(project_root, "worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(scratch, ignore_errors=True)
    landed = {"base_head": base_head, "merge_commit": merge_commit, "how": how}
    save_task(path, task, state="landed", blocked_reason=None, landed=landed)
    with ledger.tx() as con:
        ledger.event(
            con,
            _packet_attempt(ledger, report["task"]),
            "landed",
            task=report["task"],
            base=base,
            **landed,
        )
    return dict(report, landed=True, **landed)


def land_cli(spec, ledger):
    """The `inference-grid land --json {board_dir, project_root, task, base, gates, ...}` entry."""
    return land_packet(
        spec["board_dir"],
        spec["project_root"],
        spec["task"],
        spec["base"],
        spec.get("gates") or [],
        ledger,
        spec.get("packets_root") or spec["board_dir"],
        dry_run=bool(spec.get("dry_run")),
    )
