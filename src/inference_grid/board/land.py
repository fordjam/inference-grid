"""board/land.py: landing as a code node — a passed packet branch joins its base.

Until now a packet task that settled `passed` stopped there: its branch sat at
`packet/<task-id>` in the project repository and the operator merged it by hand.
This module closes that loop with the same machinery the review uses to prove a
merge: `verify_merge` (B1) runs first in a scratch worktree, and only a mergeable
branch with green gates reaches the real landing.

The landing itself never happens in the operator checkout. A scratch worktree of
the base (detached at the base head) takes `git merge --no-ff`, so the merge
commit always keeps both parents; conflicts confined to the two independent-row
documents (`docs/CONTRIBUTIONS.md`, `docs/LANES.md`) are resolved by keeping both
sides via git's union merge driver, and any other conflict — or a gate failing on
the merged tree, which only the real merge can show — aborts and blocks the task.
`how` records what the landing was: `ff` when the base head was already an
ancestor of the branch (the merge commit's tree is exactly the branch's tree),
`union` for a union-resolved merge, `merge` otherwise.

The base ref then moves to the merge commit. When the base is checked out in a
worktree other than the project root the landing refuses — a ref move there
would desync someone else's checkout — and when the project root itself holds
the base it must be clean, because the landing syncs it with `reset --hard`
after the ref move; the board owns that checkout, and only a clean one can be
moved without losing work.

Landings serialize on a lock directory under `packets_root`, one per base.
Settling writes `landed: {base_head, merge_commit, how}` onto the task and the
`landed` state (validated in `board/packet_task.py`, since the provider-authored
`board/task.py` refuses both), and records a `packet_landed` ledger event when a
ledger is at hand.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..lanes.packet import Gate, run_gates
from .packet_task import validate_board_task
from .verify_merge import verify_merge

# The only files a union merge may resolve: rows in these two documents are
# independent, so keeping both sides is always correct.
UNION_PATHS = ("docs/CONTRIBUTIONS.md", "docs/LANES.md")

UNION_ATTRIBUTES = "".join(path + " merge=union\n" for path in UNION_PATHS)

LOCK_NAME = "land-locks"


def _git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            "git " + " ".join(args) + ": " + proc.stderr.decode("utf-8", "replace").strip()
        )
    return proc


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


def _report(task, base, branch, **fields):
    out = {
        "task": task,
        "base": base,
        "branch": branch,
        "landed": False,
        "would_land": False,
        "how": None,
        "merge_commit": None,
        "base_head": None,
        "reason": "",
        "conflicts": [],
        "gates": [],
        "dry_run": False,
    }
    out.update(fields)
    return out


def _settle(path, task, **changes):
    """Write the task file through the packet validator (runner.save_task's contract)."""
    updated = validate_board_task(dict(task, **changes))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(updated, indent=1) + "\n")
    tmp.replace(path)


def _holder_of(project_root, base):
    """The resolved path of the worktree holding `base`, or None."""
    proc = _git(project_root, "worktree", "list", "--porcelain")
    holder = None
    for block in proc.stdout.decode().split("\n\n"):
        lines = dict(line.split(" ", 1) for line in block.strip().splitlines() if " " in line)
        if lines.get("branch") == "refs/heads/" + base:
            holder = Path(lines["worktree"]).resolve()
    return holder


def _lock_dir(packets_root, base):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    return Path(packets_root) / LOCK_NAME / safe


def _acquire_lock(path):
    """Create the lock directory, stealing it only when its owner is provably dead."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError:
        try:
            pid = int((path / "pid").read_text())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            pass
        else:
            return False
        shutil.rmtree(path, ignore_errors=True)
        try:
            path.mkdir()
        except FileExistsError:
            return False
    (path / "pid").write_text(str(os.getpid()) + "\n")
    return True


def _release_lock(path):
    try:
        if (path / "pid").read_text().strip() == str(os.getpid()):
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _union_conflicts(conflicts):
    return bool(conflicts) and all(c in UNION_PATHS for c in conflicts)


def _blocked_on_conflicts(conflicts):
    if conflicts:
        return "merge conflicts: " + ", ".join(sorted(conflicts))
    return "merge did not complete"


def _merge_and_commit(project_root, branch, base, base_head, branch_head, gates, work_dir):
    """The real landing: --no-ff merge in a scratch worktree of the base, gates, commit.

    Returns (how, merge_commit, gate dicts). Union conflicts are retried with git's
    union merge driver; anything else, and any gate failing on the merged tree, raises
    RuntimeError with the reason after the worktree is gone and the base untouched.
    """
    merge_base = _git(project_root, "merge-base", base_head, branch_head).stdout.decode().strip()
    how = "ff" if merge_base == base_head else "merge"
    scratch = Path(tempfile.mkdtemp(prefix="land-", dir=work_dir))
    worktree = scratch / "wt"
    added = False
    try:
        _git(project_root, "worktree", "add", "--quiet", "--detach", str(worktree), base_head)
        added = True
        merge = _git(worktree, "merge", "--no-ff", "--no-commit", branch_head, check=False)
        conflicts = (
            _git(worktree, "diff", "--name-only", "--diff-filter=U", check=False)
            .stdout.decode("utf-8", "replace")
            .split()
        )
        if merge.returncode != 0:
            _git(worktree, "merge", "--abort", check=False)
            if not _union_conflicts(conflicts):
                raise RuntimeError(_blocked_on_conflicts(conflicts))
            # Rows are independent in the union documents: keep both sides.
            (worktree / ".gitattributes").write_text(UNION_ATTRIBUTES)
            retry = _git(worktree, "merge", "--no-ff", "--no-commit", branch_head, check=False)
            (worktree / ".gitattributes").unlink()
            if retry.returncode != 0:
                _git(worktree, "merge", "--abort", check=False)
                raise RuntimeError(_blocked_on_conflicts(conflicts))
            how = "union"
        log_dir = scratch / "gate-logs"
        log_dir.mkdir()
        results = run_gates(worktree, _gate_nodes(gates), dict(os.environ), log_dir)
        failed = [r for r in results if not r.ok]
        if failed:
            _git(worktree, "merge", "--abort", check=False)
            raise RuntimeError(
                (
                    f"gate {failed[0].name} failed on the merged tree "
                    f"(exit {failed[0].returncode}): {failed[0].tail.strip()}"
                )[:300]
            )
        _git(worktree, "commit", "-q", "-m", f"land: merge {branch} into {base}")
        merge_commit = _git(worktree, "rev-parse", "HEAD").stdout.decode().strip()
        return how, merge_commit, [r.as_dict() for r in results]
    finally:
        if added:
            _git(project_root, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(scratch, ignore_errors=True)


def land(
    board_dir,
    project_root,
    task,
    base=None,
    gates=None,
    packets_root=None,
    ledger=None,
    dry_run=False,
):
    """Land `packet/<task>` into `base`; returns the report the CLI and tick print.

    Settles the task `landed` on success and `blocked` on a conflict or a gate that
    fails on the merged tree; a refusal (wrong state, lock held, base held by another
    checkout, ...) leaves the task untouched for the next tick to retry.
    """
    task_id = task
    branch = "packet/" + task_id
    board_dir = Path(board_dir)
    path = board_dir / (task_id + ".json")
    if not path.is_file():
        return _report(task_id, base, branch, reason="no task file " + task_id + ".json")
    try:
        raw = json.loads(path.read_text())
    except ValueError as exc:
        return _report(task_id, base, branch, reason="task file unreadable: " + str(exc)[:160])
    if raw.get("category") != "packet":
        return _report(task_id, base, branch, reason="task is not a packet task")
    if raw.get("state") != "passed":
        return _report(
            task_id,
            base,
            branch,
            reason=f"task state is {raw.get('state')!r}; landing settles passed, not it",
        )
    spec = raw["spec"]
    base = base or spec["base"]
    if base != spec["base"]:
        return _report(
            task_id,
            base,
            branch,
            reason=f"base {base} does not match the task's declared base {spec['base']}",
        )
    gates = gates if gates is not None else spec["gates"]
    if (
        not isinstance(gates, list)
        or not gates
        or not all(isinstance(g, dict) and {"name", "argv"} <= set(g) for g in gates)
    ):
        return _report(task_id, base, branch, reason="gates must be the task's declared list")

    project_root = Path(project_root).resolve()
    try:
        base_head = (
            _git(project_root, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
            .stdout.decode()
            .strip()
        )
    except RuntimeError:
        return _report(
            task_id, base, branch, reason=f"base {base} is not a local branch of the project"
        )
    try:
        branch_head = (
            _git(project_root, "rev-parse", "--verify", branch + "^{commit}")
            .stdout.decode()
            .strip()
        )
    except RuntimeError:
        return _report(task_id, base, branch, reason=f"branch {branch} does not exist")
    holder = _holder_of(project_root, base)
    if holder is not None and holder != project_root:
        return _report(
            task_id,
            base,
            branch,
            reason=f"base is checked out at {holder}; landing moves only the project's own checkout",
        )
    if (
        holder == project_root
        and _git(project_root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    ):
        # Only tracked changes would be clobbered by the checkout sync; untracked files
        # (the board's own task files, typically) are never touched by reset --hard.
        return _report(
            task_id,
            base,
            branch,
            reason="base is checked out at the project root with local changes",
        )

    if dry_run:
        # The same scratch root as a landing: a project's own test guards may treat
        # the OS temp root specially (monarch's isolation guard allows it wholesale),
        # so a probe under $TMPDIR would not report what the landing will see.
        land_dir = Path(packets_root) / task_id / "land" if packets_root is not None else None
        try:
            probe = verify_merge(project_root, branch, base, gates, work_dir=land_dir)
        except Exception as exc:
            return _report(
                task_id, base, branch, reason="verify_merge: " + str(exc)[:200], dry_run=True
            )
        unionable = _union_conflicts(probe["conflicts"])
        failed = [g for g in probe["gates"] if not g["ok"]]
        would = bool(probe["mergeable"] or unionable) and not failed
        how = (
            "ff"
            if probe["target_head"]
            == _git(project_root, "merge-base", probe["target_head"], probe["branch_head"])
            .stdout.decode()
            .strip()
            else "merge"
        )
        if unionable:
            how = "union"
        return _report(
            task_id,
            base,
            branch,
            dry_run=True,
            would_land=would,
            how=how,
            base_head=probe["target_head"],
            reason="" if would else "merge is not landable",
            conflicts=probe["conflicts"],
            gates=probe["gates"],
        )

    lock = _lock_dir(packets_root if packets_root is not None else board_dir, base)
    if not _acquire_lock(lock):
        return _report(
            task_id, base, branch, reason="another landing holds the lock for base " + base
        )
    try:
        land_dir = Path(packets_root) / task_id / "land" if packets_root is not None else None
        try:
            probe = verify_merge(project_root, branch, base, gates, work_dir=land_dir)
        except Exception as exc:
            reason = ("verify_merge: " + str(exc))[:300]
            _settle(path, raw, state="blocked", blocked_reason=reason)
            return _report(task_id, base, branch, reason=reason)
        if not probe["mergeable"] and not _union_conflicts(probe["conflicts"]):
            reason = _blocked_on_conflicts(probe["conflicts"])[:300]
            _settle(path, raw, state="blocked", blocked_reason=reason)
            return _report(task_id, base, branch, reason=reason, conflicts=probe["conflicts"])
        failed = [g for g in probe["gates"] if not g["ok"]]
        if failed:
            reason = (
                f"gate {failed[0]['name']} failed (exit {failed[0]['returncode']}): "
                + failed[0]["tail"].strip()
            )[:300]
            _settle(path, raw, state="blocked", blocked_reason=reason)
            return _report(task_id, base, branch, reason=reason, gates=probe["gates"])
        try:
            how, merge_commit, gate_dicts = _merge_and_commit(
                project_root, branch, base, base_head, branch_head, gates, land_dir
            )
        except RuntimeError as exc:
            reason = str(exc)[:300]
            _settle(path, raw, state="blocked", blocked_reason=reason)
            return _report(task_id, base, branch, reason=reason)
        _git(project_root, "update-ref", "refs/heads/" + base, merge_commit)
        if holder == project_root:
            # The board's own checkout holds the base and was clean: sync it to the merge.
            _git(project_root, "reset", "--hard", "-q")
        landed = {"base_head": base_head, "merge_commit": merge_commit, "how": how}
        _settle(path, raw, state="landed", blocked_reason=None, landed=landed)
        if ledger is not None:
            with ledger.tx() as con:
                ledger.event(
                    con,
                    None,
                    "packet_landed",
                    task=task_id,
                    base=base,
                    base_head=base_head,
                    merge_commit=merge_commit,
                    how=how,
                )
        return _report(
            task_id,
            base,
            branch,
            landed=True,
            how=how,
            merge_commit=merge_commit,
            base_head=base_head,
            gates=gate_dicts,
        )
    finally:
        _release_lock(lock)
