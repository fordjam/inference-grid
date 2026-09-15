"""The driver's gates: each check against a temp git repo, no network, no real home."""

import os
import subprocess
import sys
from pathlib import Path

from inference_grid.lanes import gates

TRAILER = "Co-Authored-By: Tester <tester@example.test>"
OTHER_TRAILER = "Co-Authored-By: Other <other@example.test>"


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


def _repo(path, base_files=None):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.test")
    _git(path, "config", "user.name", "test")
    for name, text in (base_files or {}).items():
        (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", "base")
    _git(path, "branch", "base")
    return path


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)


def test_scoped_ruff_sees_only_changed_and_untracked_python(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo", {"legacy.py": "legacy=1\n"})
    monkeypatch.chdir(repo)

    # Nothing changed: a pass, and the repo's existing unformatted files are not consulted.
    assert gates.run_scoped_ruff("base", "format") == 0
    assert "no python files changed" in capsys.readouterr().out

    # A changed .py file is in scope, even though the repo holds unformatted ones already.
    (repo / "changed.py").write_text("changed=1\n")
    _commit(repo, "changed")
    assert gates.run_scoped_ruff("base", "format") == 1

    # An untracked .py file is in scope too.
    (repo / "untracked.py").write_text("untracked=1\n")
    assert gates.run_scoped_ruff("base", "format") == 1

    # The check mode sees a changed lint error as well.
    (repo / "untracked.py").unlink()
    (repo / "lint.py").write_text("import os\n")
    _commit(repo, "lint")
    assert gates.run_scoped_ruff("base", "check") == 1


def test_home_paths_gate_matches_the_runtime_home_in_changed_files_only(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path / "repo", {"history.md": f"{Path.home()}/old\n"})
    home = str(Path.home())
    monkeypatch.chdir(repo)

    # The home path is in a file this packet did not touch: not the packet's fault.
    assert gates.run_home_paths("base") == 0
    assert "no home paths in changed files" in capsys.readouterr().out

    # A changed file that bakes this machine's home in fails and names the match.
    (repo / "leak.md").write_text(f"{home}/projects\n")
    _commit(repo, "leak")
    assert gates.run_home_paths("base") == 1
    assert home in capsys.readouterr().out

    # A /home/example-style fixture is some other machine's home, so it is not a match.
    (repo / "leak.md").write_text("/home/example/private\n")
    _commit(repo, "fixture")
    assert gates.run_home_paths("base") == 0

    # The gate's own source carries no literal home either.
    (repo / "gates_source.py").write_text(Path(gates.__file__).read_text())
    _commit(repo, "source")
    assert gates.run_home_paths("base") == 0


def test_commit_gate_refuses_count_trailer_and_dirty_tree(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    assert gates.run_commit("base", TRAILER) == 1
    assert "expected exactly one commit ahead of base, found 0" in capsys.readouterr().out

    (repo / "a.txt").write_text("a\n")
    _commit(repo, "one")
    assert gates.run_commit("base", TRAILER) == 1
    assert "trailer missing" in capsys.readouterr().out

    _git(repo, "commit", "-q", "--amend", "-m", f"one\n\n{TRAILER}")
    (repo / "dirty.txt").write_text("x\n")
    assert gates.run_commit("base", TRAILER) == 1
    assert "not clean" in capsys.readouterr().out

    (repo / "dirty.txt").unlink()
    assert gates.run_commit("base", TRAILER) == 0
    assert "commit gate ok" in capsys.readouterr().out


def test_commit_gate_trailer_is_a_parameter(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n")
    _commit(repo, f"why\n\n{OTHER_TRAILER}")
    monkeypatch.chdir(repo)

    assert gates.run_commit("base", OTHER_TRAILER) == 0
    assert gates.run_commit("base", TRAILER) == 1


def test_gates_for_builds_the_five_module_invoked_gates():
    built = gates.gates_for("/py", "origin/glm/work", "TRAILER", "/cache")
    assert [g.name for g in built] == [
        "pytest",
        "ruff-format",
        "ruff-check",
        "no-home-paths",
        "commit",
    ]
    assert built[0].argv == [
        "/py",
        "-m",
        "inference_grid.lanes.gates",
        "pytest",
        "origin/glm/work",
        "/cache",
    ]
    assert built[1].argv == [
        "/py",
        "-m",
        "inference_grid.lanes.gates",
        "ruff",
        "origin/glm/work",
        "format",
    ]
    assert built[2].argv[-1] == "check"
    assert built[3].argv == [
        "/py",
        "-m",
        "inference_grid.lanes.gates",
        "home-paths",
        "origin/glm/work",
    ]
    assert built[4].argv == [
        "/py",
        "-m",
        "inference_grid.lanes.gates",
        "commit",
        "origin/glm/work",
        "TRAILER",
    ]
    assert all(g.env == {"PYTHONPATH": "src"} for g in built)


def test_the_module_is_the_gate_command(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n")
    _commit(repo, f"why\n\n{TRAILER}")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "inference_grid.lanes.gates", "commit", "base", TRAILER],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and "commit gate ok" in proc.stdout


def _failing_suite(base_output, branch_output):
    """A fake suite: the branch (the cwd) answers one way, the scratch worktree another."""

    def suite(work):
        return branch_output if Path(work) == Path.cwd() else base_output

    return suite


def test_failing_node_ids_takes_the_id_before_the_message():
    output = "1 failed\nFAILED tests/a.py::test_x[param with space] - AssertionError: no\n"
    assert gates.failing_node_ids(output) == ["tests/a.py::test_x[param with space]"]
    assert gates.failing_node_ids("3 passed\n") == []


def test_pytest_gate_names_inherited_failures_and_fails_only_on_new_ones(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path / "repo", {"a.txt": "a\n"})
    monkeypatch.chdir(repo)
    base = "FAILED tests/test_old.py::test_sandbox - PermissionError\n1 failed\n"
    branch = base + "FAILED tests/test_new.py::test_packet - AssertionError\n2 failed\n"

    assert (
        gates.run_pytest("base", tmp_path / "cache", "py", suite=_failing_suite(base, branch)) == 1
    )
    out = capsys.readouterr().out
    assert "inherited: ['tests/test_old.py::test_sandbox']" in out
    assert "new failures:" in out and "tests/test_new.py::test_packet" in out


def test_pytest_gate_passes_when_every_failure_is_inherited(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo", {"a.txt": "a\n"})
    monkeypatch.chdir(repo)
    output = "FAILED tests/test_old.py::test_sandbox - PermissionError\n1 failed\n"

    assert (
        gates.run_pytest("base", tmp_path / "cache", "py", suite=_failing_suite(output, output))
        == 0
    )
    out = capsys.readouterr().out
    assert "inherited: ['tests/test_old.py::test_sandbox']" in out
    assert "no new failures" in out


def test_pytest_gate_reports_an_inherited_failure_that_now_passes(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo", {"a.txt": "a\n"})
    monkeypatch.chdir(repo)
    base = "FAILED tests/test_old.py::test_sandbox - PermissionError\n1 failed\n"

    assert (
        gates.run_pytest("base", tmp_path / "cache", "py", suite=_failing_suite(base, "1 passed\n"))
        == 0
    )
    out = capsys.readouterr().out
    assert "repaired: ['tests/test_old.py::test_sandbox']" in out


def test_the_baseline_is_cached_per_base_commit(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", {"a.txt": "a\n"})
    monkeypatch.chdir(repo)
    cache = tmp_path / "cache"
    runs = {"base": 0, "branch": 0}

    def suite(work):
        runs["branch" if Path(work) == Path.cwd() else "base"] += 1
        return "FAILED tests/test_old.py::test_sandbox - PermissionError\n"

    assert gates.run_pytest("base", cache, "py", suite=suite) == 0
    assert gates.run_pytest("base", cache, "py", suite=suite) == 0
    assert runs == {"base": 1, "branch": 2}
    assert len(list(cache.glob("pytest-baseline-*.json"))) == 1
