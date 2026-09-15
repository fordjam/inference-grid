"""verify-merge: a code node that proves a branch merges into its target before any review.

A per-commit diff cannot see what a re-run on the target would break; only
`git merge && the gates` can. The node merges `branch` into `target` in a
scratch `git worktree` (never the operator's checkout), records the
conflicting paths and stops when the merge conflicts, otherwise runs each
declared gate as a code node (lanes/packet.py::run_gates) on the merge
result, then aborts the merge and removes the worktree either way.

Board tasks with category "verify_merge" settle through the same node with
no lane, no ledger attempt and no quota: a gate, not a model call.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..lanes.packet import Gate, run_gates
from .task import validate_task

REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+-]*")


def _git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            "git " + " ".join(args) + ": " + proc.stderr.decode("utf-8", "replace").strip()
        )
    return proc


def _gate_nodes(spec_gates):
    return [
        Gate(g["name"], list(g["argv"]), cwd=g.get("cwd", "."), timeout=g.get("timeout", 1800))
        for g in spec_gates
    ]


def verify_merge(repo, branch, target, gates, work_dir=None):
    """Merge branch into target in a scratch worktree, run the gates, clean up.

    Returns {mergeable, conflicts, gates, branch_head, target_head, elapsed_s}.
    A conflict records the conflicting paths and stops before any gate. The
    worktree is registered inside a scratch directory under `work_dir` (the
    system temp dir when None) and never left behind, and the operator
    checkout's HEAD and index are never touched.
    """
    started = time.monotonic()
    repo = Path(repo).resolve()
    if work_dir is not None:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
    branch_head = _git(repo, "rev-parse", "--verify", branch + "^{commit}").stdout.decode().strip()
    target_head = _git(repo, "rev-parse", "--verify", target + "^{commit}").stdout.decode().strip()
    scratch = Path(tempfile.mkdtemp(prefix="verify-merge-", dir=work_dir))
    worktree = scratch / "wt"
    added = False
    try:
        _git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), branch_head)
        added = True
        try:
            merge = _git(worktree, "merge", "--no-commit", "--no-ff", target_head, check=False)
            mergeable = merge.returncode == 0
            conflicts, gate_results = [], []
            if mergeable:
                log_dir = scratch / "gate-logs"
                log_dir.mkdir()
                gate_results = [
                    {"name": r.name, "ok": r.ok, "returncode": r.returncode, "tail": r.tail}
                    for r in run_gates(worktree, _gate_nodes(gates), dict(os.environ), log_dir)
                ]
            else:
                conflicts = (
                    _git(worktree, "diff", "--name-only", "--diff-filter=U", check=False)
                    .stdout.decode("utf-8", "replace")
                    .split()
                )
        finally:
            _git(worktree, "merge", "--abort", check=False)
    finally:
        if added:
            _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(scratch, ignore_errors=True)
    return {
        "mergeable": mergeable,
        "conflicts": conflicts,
        "gates": gate_results,
        "branch_head": branch_head,
        "target_head": target_head,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def verify_merge_cli(spec):
    """The `inference-grid verify-merge --json {repo, branch, target, gates, out}` entry."""
    report = verify_merge(
        spec["repo"],
        spec["branch"],
        spec["target"],
        spec.get("gates") or [],
        work_dir=spec.get("work_dir"),
    )
    out = spec.get("out")
    if out:
        Path(out).write_text(json.dumps(report, indent=2) + "\n")
    return report


def validate_verify_merge_task(raw):
    """The verify_merge shape: the standard task keys plus spec {branch, target, gates}.

    board/task.py is provider-authored (integrated unmodified), so the shared key
    checks run on a copy whose category is one it accepts; the code node runs
    without a lane and produces no artifacts, so inputs, artifacts and lanes may
    be empty here — the placeholders exist only to satisfy the shared checks and
    never reach the task file. Everything verify_merge-specific is checked below.
    """

    def err(k, m):
        raise ValueError(k + ": " + m)

    if not isinstance(raw, dict):
        err("task", "expected a dict")
    if raw.get("category") != "verify_merge":
        err("category", "expected verify_merge")
    if "spec" not in raw:
        err("task", "missing spec key")
    core = {k: v for k, v in raw.items() if k != "spec"}
    core["category"] = "pure_function"
    if not isinstance(core.get("brief"), str) or not core["brief"]:
        err("brief", "must be a non-empty str")
    core["inputs"] = [core["brief"]]
    core["artifacts"] = core["artifacts"] or ["out.json"]
    core["lanes"] = core["lanes"] or ["code-node"]
    validate_task(core)
    spec = raw["spec"]
    if not isinstance(spec, dict):
        err("spec", "expected a dict")
    required = {"branch", "target", "gates"}
    missing = required - set(spec)
    unknown = set(spec) - required
    if missing:
        err("spec", "missing keys: " + ", ".join(sorted(missing)))
    if unknown:
        err("spec", "unknown keys: " + ", ".join(sorted(unknown)))

    def ref(name, value):
        if (
            not isinstance(value, str)
            or not REF.fullmatch(value)
            or ".." in value
            or len(value) > 80
        ):
            err("spec", name + " must be a branch or ref name in the project repository")
        return value

    ref("branch", spec["branch"])
    ref("target", spec["target"])
    gates = spec["gates"]
    if not isinstance(gates, list) or not gates:
        err("spec", "gates must be a non-empty list")
    for gate in gates:
        if not isinstance(gate, dict) or not {"name", "argv"} <= set(gate):
            err("spec", "each gate needs a name and argv")
        unknown = set(gate) - {"name", "argv", "cwd", "timeout"}
        if unknown:
            err("spec", "unknown gate keys: " + ", ".join(sorted(unknown)))
        if not isinstance(gate["name"], str) or not gate["name"] or len(gate["name"]) > 80:
            err("spec", "gate name must be a non-empty str of at most 80 chars")
        argv = gate["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            err("spec", "gate argv must be a non-empty list of strs")
        if "cwd" in gate and (not isinstance(gate["cwd"], str) or not gate["cwd"]):
            err("spec", "gate cwd must be a non-empty str")
        if "timeout" in gate and (
            isinstance(gate["timeout"], bool)
            or not isinstance(gate["timeout"], int)
            or not 1 <= gate["timeout"] <= 3600
        ):
            err("spec", "gate timeout must be an int in 1..3600")
    out = {
        k: (dict(v) if k == "budget" else list(v) if isinstance(v, list) else v)
        for k, v in raw.items()
        if k != "spec"
    }
    out["spec"] = {
        "branch": spec["branch"],
        "target": spec["target"],
        "gates": [dict(g) for g in gates],
    }
    return out
