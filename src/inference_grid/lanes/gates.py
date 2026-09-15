"""The driver's gates as a tested module.

`scripts/run_lane.py` is the operator's driver; the gates it runs after each agent
round are code nodes that decide whether the round passed. They live here, with
tests, so a gate changes the way any other node changes. Each gate is built by a
small function returning a `packet.Gate`, and its check is module-level Python run
as `python -m inference_grid.lanes.gates <gate> <args>` inside the worktree, so the
driver writes no script file per attempt.

The pytest gate is baseline-aware: it runs the suite once at the base commit (in a
scratch worktree, the failing node ids cached per commit under the attempt store)
and then on the packet's branch, so a packet is judged on the failures it caused,
not the repository's pre-existing ones (see `run_pytest`); the driver no longer
asks the agent to capture that baseline by hand.

The checks never touch the repository; the pytest gate writes only its baseline
cache under the attempt store. The only value a check derives from the machine is
the operator's home directory, read at runtime with `Path.home()` and never written
down.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

from inference_grid.lanes.packet import Gate


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def _git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], capture_output=True, text=True).returncode == 0


def changed_python_files(base: str) -> list[str]:
    """The `.py` files this packet added, modified, renamed or left untracked."""
    diff = _git("diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    return [f for f in diff.split() + untracked.split() if f.endswith(".py")]


def run_scoped_ruff(base: str, mode: str) -> int:
    """ruff over the packet's own `.py` files: the repository is not format-clean.

    A repo-wide `ruff format --check` drives the agent into a fifty-file reformat
    sweep to get past a gate that is not about its packet; only the files the
    packet touched are checked, and no changes at all is a pass.
    """
    files = changed_python_files(base)
    if not files:
        print("no python files changed")
        return 0
    flags = ["format", "--check"] if mode == "format" else ["check"]
    return subprocess.run([sys.executable, "-m", "ruff", *flags, *files]).returncode


def _board_task_file(path: str) -> bool:
    parts = Path(path).parts
    return len(parts) >= 3 and parts[-3:-1] == ("grid", "board") and path.endswith(".json")


def run_home_paths(base: str) -> int:
    """Refuse a packet that bakes this machine's home directory into a changed file.

    The home directory is the runtime one, so the literal never sits in this source;
    history and files the packet did not touch are not its fault, and a
    `/home/example`-style fixture is some other machine's home, not a match.
    """
    home = str(Path.home())
    files = _git("diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD").split()
    # Board task files are the operator's: their gate argv names this machine's
    # interpreters and the board runner rewrites them on every settlement, so a board
    # kept in git lands a state commit with every packet. No lane authors them.
    files = [f for f in files if not _board_task_file(f)]
    found = _git("grep", "-n", "-F", home, "HEAD", "--", *files) if files else ""
    print(found or "no home paths in changed files")
    return 1 if found else 0


def run_commit(base: str, trailer: str) -> int:
    """Exactly one commit ahead of base, the trailer present, the tree clean."""
    count = len([line for line in _git("log", "--oneline", f"{base}..HEAD").splitlines() if line])
    message = _git("log", "-1", "--format=%B")
    dirty = _git("status", "--porcelain")
    problems = []
    if count != 1:
        problems.append(f"expected exactly one commit ahead of {base}, found {count}")
    if trailer not in message:
        problems.append(f"commit trailer missing: {trailer}")
    if dirty.strip():
        problems.append("working tree not clean:\n" + dirty)
    print("\n".join(problems) or "commit gate ok")
    return 1 if problems else 0


SUITE_FLAGS = ("-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider")


def failing_node_ids(output: str) -> list[str]:
    """The node ids pytest's short summary prints, in order.

    A summary line is `FAILED path::test[params] - message`; the node id is taken up
    to the ` - ` separator, so a parametrised id with spaces survives.
    """
    ids: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("FAILED "):
            continue
        node = line[len("FAILED ") :].split(" - ", 1)[0].strip()
        if node and node not in ids:
            ids.append(node)
    return ids


def run_suite(python: str, work: Path) -> str:
    """The whole suite in `work`, its output returned.

    No `-x`: comparing failures needs every node id, not the first one. `PYTHONPATH`
    is relative, so each worktree imports the package it holds.
    """
    proc = subprocess.run(
        [python, *SUITE_FLAGS],
        cwd=work,
        env=dict(os.environ, PYTHONPATH="src"),
        capture_output=True,
        text=True,
    )
    return proc.stdout + proc.stderr


Suite = Callable[[Path], str]


def baseline_failures(base: str, cache_dir, python: str, suite: Optional[Suite] = None) -> set[str]:
    """Failing node ids at `base`, cached per resolved commit under `cache_dir`.

    However many packets sit on one base, the suite runs there once: the scratch
    worktree is checked out at the base commit, pytest runs in it, and the node ids
    are read back from `<cache_dir>/pytest-baseline-<sha>.json` after that.
    """
    sha = _git("rev-parse", base).strip()
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"pytest-baseline-{sha}.json"
    try:
        return set(json.loads(cache.read_text()))
    except (OSError, ValueError):
        pass
    suite = suite or (lambda work: run_suite(python, work))
    scratch = Path(tempfile.mkdtemp(dir=cache_dir, prefix=f"base-{sha[:12]}-"))
    try:
        if not _git_ok("worktree", "add", "--detach", "--force", str(scratch), sha):
            raise RuntimeError(f"could not check out {base} ({sha}) for the pytest baseline")
        ids = failing_node_ids(suite(scratch))
    finally:
        _git_ok("worktree", "remove", "--force", str(scratch))
        shutil.rmtree(scratch, ignore_errors=True)
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(sorted(ids)))
    os.replace(tmp, cache)
    return set(ids)


def run_pytest(
    base: str, cache_dir, python: Optional[str] = None, suite: Optional[Suite] = None
) -> int:
    """Pass when every failure on the branch is one the base already had.

    Pre-existing failures are named on the `inherited:` line (the gate result keeps
    this output); a node id that failed at base and passes now is named `repaired:`;
    only a failure the base did not have fails the gate, listed by name.
    """
    python = python or sys.executable
    suite = suite or (lambda work: run_suite(python, work))
    inherited = baseline_failures(base, cache_dir, python, suite)
    on_branch = set(failing_node_ids(suite(Path.cwd())))
    print(f"inherited: {sorted(inherited & on_branch)}")
    print(f"repaired: {sorted(inherited - on_branch)}")
    new = sorted(on_branch - inherited)
    if new:
        print("new failures:")
        for node in new:
            print(f"  {node}")
        return 1
    print("no new failures")
    return 0


def pytest_gate(python: str, base: str, cache_dir: str) -> Gate:
    return Gate(
        "pytest",
        [python, "-m", "inference_grid.lanes.gates", "pytest", base, cache_dir],
        env={"PYTHONPATH": "src"},
        timeout=2400,
    )


def ruff_gate(python: str, base: str, mode: str) -> Gate:
    return Gate(
        f"ruff-{mode}",
        [python, "-m", "inference_grid.lanes.gates", "ruff", base, mode],
        env={"PYTHONPATH": "src"},
        timeout=300,
    )


def home_paths_gate(python: str, base: str) -> Gate:
    return Gate(
        "no-home-paths",
        [python, "-m", "inference_grid.lanes.gates", "home-paths", base],
        env={"PYTHONPATH": "src"},
        timeout=60,
    )


def commit_gate(python: str, base: str, trailer: str) -> Gate:
    return Gate(
        "commit",
        [python, "-m", "inference_grid.lanes.gates", "commit", base, trailer],
        env={"PYTHONPATH": "src"},
        timeout=60,
    )


def gates_for(python: str, base: str, trailer: str, cache_dir: str) -> list[Gate]:
    """The five gates every packet round runs, in order."""
    return [
        pytest_gate(python, base, cache_dir),
        ruff_gate(python, base, "format"),
        ruff_gate(python, base, "check"),
        home_paths_gate(python, base),
        commit_gate(python, base, trailer),
    ]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m inference_grid.lanes.gates <pytest|ruff|home-paths|commit> <args>")
        return 2
    gate, rest = argv[0], argv[1:]
    if gate == "pytest" and len(rest) == 2:
        return run_pytest(rest[0], rest[1])
    if gate == "ruff" and len(rest) == 2:
        return run_scoped_ruff(rest[0], rest[1])
    if gate == "home-paths" and len(rest) == 1:
        return run_home_paths(rest[0])
    if gate == "commit" and len(rest) == 2:
        return run_commit(rest[0], rest[1])
    print(f"unknown or incomplete gate invocation: {gate}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
