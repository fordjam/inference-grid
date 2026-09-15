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
    built = gates.gates_for("/py", "origin/glm/work", "TRAILER")
    assert [g.name for g in built] == [
        "pytest",
        "ruff-format",
        "ruff-check",
        "no-home-paths",
        "commit",
    ]
    assert built[0].argv == ["/py", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"]
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
