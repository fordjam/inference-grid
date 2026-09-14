"""inbox-integrate: the landing-to-HEAD checklist, written onto the inbox branch."""

import json
import subprocess
from pathlib import Path

import pytest

from inference_grid.board.integrate import inbox_integrate


def git(project, *argv):
    return subprocess.run(
        ["git", "-C", str(project), *argv], capture_output=True, text=True, check=True
    )


def commit(project, message):
    git(project, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message)


def landed_project(tmp_path):
    """A project whose grid/inbox branch carries one tree-task landing (as land_in_inbox writes it)."""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "tests/__init__.py").write_text("")
    (project / "grid/board").mkdir(parents=True)
    (project / "grid/briefs").mkdir(parents=True)
    (project / "src/mod.py").write_text("VALUE = 1\n")
    (project / "tests/test_mod2.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
        "import unittest\nimport mod2\n\nclass T(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(mod2.VALUE, 1)\n"
    )
    (project / "grid/briefs/copy-ok.txt").write_text("copy mod.py to src/mod2.py\n")
    task = dict(
        id="copy-ok",
        category="pure_function",
        brief="grid/briefs/copy-ok.txt",
        inputs=["grid/briefs/copy-ok.txt", "src/mod.py"],
        tests=["tests/test_mod2.py"],
        artifacts=["src/mod2.py"],
        lanes=["go"],
        author_family=None,
        budget={"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None},
        state="accepted",
        blocked_reason=None,
    )
    (project / "grid/board/copy-ok.json").write_text(json.dumps(task))
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    commit(project, "base")
    # The landing: a branch commit adding the artifact at its project path plus the record.
    git(project, "checkout", "-q", "-b", "grid/inbox")
    (project / "src/mod2.py").write_text("VALUE = 1\n")
    record = {
        "task": "copy-ok",
        "attempt": "aaaaaaaa-0000",
        "lane": "go",
        "author_family": "glm",
        "reviewer_family": "kimi",
        "review_task": "review-copy-ok",
        "receipt_digest": "d" * 64,
    }
    (project / "grid/inbox").mkdir()
    (project / "grid/inbox/copy-ok.json").write_text(json.dumps(record))
    git(project, "add", "-A")
    commit(project, "grid: accept copy-ok (go, attempt aaaaaaaa-0000)")
    git(project, "checkout", "-q", "main")
    return project


def test_checklist_reports_a_clean_apply_and_lives_on_the_branch(tmp_path, monkeypatch):
    project = landed_project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")

    result = inbox_integrate(project, "copy-ok")
    assert result["applies_cleanly"] is True and result["committed"] is True
    # The checklist is on the branch; the operator's checkout never saw grid/inbox.
    assert not (project / "grid/inbox").exists()
    text = git(project, "show", "grid/inbox:grid/inbox/copy-ok.integrate.md").stdout
    assert "Applies cleanly to the current HEAD: **yes**" in text
    assert "tests/test_mod2.py" in text  # the declared tests
    assert "review-copy-ok" in text and "kimi" in text
    assert "src/mod2.py" in text  # in the diffstat and the commands
    assert "git apply --check grid-inbox-copy-ok.patch" in text


def test_apply_check_travels_through_the_run_seam(tmp_path, monkeypatch):
    project = landed_project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    from inference_grid.board import integrate

    seen = []
    real = integrate.run

    def spy(argv):
        seen.append(argv)
        return real(argv)

    monkeypatch.setattr(integrate, "run", spy)
    inbox_integrate(project, "copy-ok")
    assert any("apply" in argv and "--check" in argv for argv in seen)
    assert any("diff" in argv and "--stat" in argv for argv in seen)


def test_a_conflicting_head_reports_the_conflict(tmp_path, monkeypatch):
    project = landed_project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    # HEAD moves forward with a conflicting version of the landed artifact.
    (project / "src/mod2.py").write_text("VALUE = 999\n")
    git(project, "add", "-A")
    commit(project, "operator changed src/mod2.py")

    result = inbox_integrate(project, "copy-ok")
    assert result["applies_cleanly"] is False and result["committed"] is True
    text = git(project, "show", "grid/inbox:grid/inbox/copy-ok.integrate.md").stdout
    assert "Applies cleanly to the current HEAD: **no**" in text
    assert "patch failed" in text or "already exists" in text


def test_apply_creates_the_integrate_branch_with_tests_green(tmp_path, monkeypatch):
    project = landed_project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    result = inbox_integrate(project, "copy-ok", dry_run=False)
    assert result["applies_cleanly"] is True and result["tests_passed"] is True
    assert result["branch"] == "integrate/copy-ok"
    # The branch carries the landed artifact; the operator's checkout is untouched.
    landed = subprocess.run(
        ["git", "-C", str(project), "show", "integrate/copy-ok:src/mod2.py"],
        capture_output=True, text=True,
    )
    assert landed.stdout == "VALUE = 1\n"
    assert not (project / "src/mod2.py").exists()
    assert not list(Path.home().glob("*.integrate*"))


def test_apply_refuses_a_conflicting_head_and_creates_nothing(tmp_path, monkeypatch):
    project = landed_project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    (project / "src/mod2.py").write_text("VALUE = 999\n")
    git(project, "add", "-A")
    commit(project, "operator changed src/mod2.py")
    with pytest.raises(ValueError, match="conflicts with HEAD"):
        inbox_integrate(project, "copy-ok", dry_run=False)
    branches = subprocess.run(
        ["git", "-C", str(project), "branch", "--list", "integrate/copy-ok"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branches == ""


def test_a_task_without_a_landing_record_is_refused(tmp_path):
    project = landed_project(tmp_path)
    with pytest.raises(ValueError, match="no landing record"):
        inbox_integrate(project, "never-landed")
