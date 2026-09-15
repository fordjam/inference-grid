"""The driver's gates as a tested module.

`scripts/run_lane.py` is the operator's driver; the gates it runs after each agent
round are code nodes that decide whether the round passed. They live here, with
tests, so a gate changes the way any other node changes. Each gate is built by a
small function returning a `packet.Gate`, and its check is module-level Python run
as `python -m inference_grid.lanes.gates <gate> <args>` inside the worktree, so the
driver writes no script file per attempt.

The checks are read-only: they inspect `git` and run `ruff`, never the repository.
The only value a check derives from the machine is the operator's home directory,
read at runtime with `Path.home()` and never written down.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from inference_grid.lanes.packet import Gate


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


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


def run_home_paths(base: str) -> int:
    """Refuse a packet that bakes this machine's home directory into a changed file.

    The home directory is the runtime one, so the literal never sits in this source;
    history and files the packet did not touch are not its fault, and a
    `/home/example`-style fixture is some other machine's home, not a match.
    """
    home = str(Path.home())
    files = _git("diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD").split()
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


def pytest_gate(python: str) -> Gate:
    return Gate(
        "pytest",
        [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"],
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


def gates_for(python: str, base: str, trailer: str) -> list[Gate]:
    """The five gates every packet round runs, in order."""
    return [
        pytest_gate(python),
        ruff_gate(python, base, "format"),
        ruff_gate(python, base, "check"),
        home_paths_gate(python, base),
        commit_gate(python, base, trailer),
    ]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m inference_grid.lanes.gates <ruff|home-paths|commit> <args>")
        return 2
    gate, rest = argv[0], argv[1:]
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
