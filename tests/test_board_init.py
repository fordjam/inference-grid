"""board-init: the board skeleton in a git repository, idempotently."""

import subprocess

import pytest

from inference_grid.board.new import SCHEMA_TEST, board_init


def git(project, *argv):
    subprocess.run(
        ["git", "-C", str(project), *argv], check=True, capture_output=True, text=True
    )


def make_repo(tmp_path):
    project = tmp_path / "yt-research-mcp"
    project.mkdir(parents=True)
    git(project, "init", "-q", "-b", "main")
    return project


def test_board_init_creates_the_skeleton_and_the_config(tmp_path):
    project = make_repo(tmp_path)
    created = board_init(project, board_name="yt-research", tier="T0")
    assert (project / "grid/board").is_dir()
    assert (project / "grid/briefs").is_dir()
    assert (project / "grid/tests/test_review_schema.py").read_text() == SCHEMA_TEST
    readme = (project / "grid/README.md").read_text()
    assert "Repository tier: T0" in readme
    assert "src/" in readme
    assert created["board_config"]["project_root"] == str(project)
    assert created["boards_path"].endswith("boards/yt-research.json")


def test_board_init_refuses_a_project_without_git(tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    with pytest.raises(ValueError, match="git repository"):
        board_init(bare)


def test_board_init_is_idempotent(tmp_path):
    project = make_repo(tmp_path)
    board_init(project)
    (project / "grid/README.md").write_text("operator's own rules\n")
    second = board_init(project)
    # Nothing created the second time; the operator's README is never overwritten.
    assert second["created"] == []
    assert (project / "grid/README.md").read_text() == "operator's own rules\n"
    assert (project / "grid/tests/test_review_schema.py").read_text() == SCHEMA_TEST


def test_board_init_accepts_prefixes_and_tier(tmp_path):
    project = make_repo(tmp_path)
    board_init(project, allowed_prefixes=["src/", "tests/"], tier="T1")
    readme = (project / "grid/README.md").read_text()
    assert "Repository tier: T1" in readme
    assert "api/" not in readme
