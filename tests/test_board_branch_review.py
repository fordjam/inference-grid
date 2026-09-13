"""review-branch: an independent_review task authored from a git range, offline."""

import json
import subprocess
from pathlib import Path

import pytest

from inference_grid.board import branch_review, runner
from inference_grid.board.new import SCHEMA_TEST


def git(repo, *argv):
    subprocess.run(
        ["git", "-C", str(repo)] + list(argv), check=True, capture_output=True, text=True
    )


def commit(repo, message, trailer=None):
    args = ["commit", "-q", "-m", message]
    if trailer:
        args += ["-m", trailer]
    git(repo, "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args)


def rev(repo, ref):
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref], check=True, capture_output=True, text=True
    ).stdout.strip()


def reviewed_repo(tmp_path, second_trailer="Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>"):
    """A fake project repo with a reviewed range: two commits, the first GLM-trailed."""
    repo = tmp_path / "factory-frontend"
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src/app.py").write_text("VALUE = 1\n")
    git(repo, "add", "-A")
    commit(repo, "base")
    base = rev(repo, "HEAD")
    (repo / "src/app.py").write_text("VALUE = 2\n")
    git(repo, "add", "-A")
    commit(repo, "raise VALUE", "Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    (repo / "src/new.py").write_text("NEW = 3\n")
    git(repo, "add", "-A")
    commit(repo, "add NEW", second_trailer)
    return repo, base, rev(repo, "HEAD")


def board_and_project(tmp_path):
    # The real convention: the board lives inside the project at grid/board.
    project = tmp_path / "project"
    board = project / "grid/board"
    project.mkdir(parents=True)
    return board, project


def test_review_branch_stages_tip_tree_diff_and_writes_the_task(tmp_path):
    repo, base, tip = reviewed_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}
    )
    task_id = f"review-factory-frontend-{tip[:7]}"
    assert created["id"] == task_id
    assert created["author_family"] == "glm" and created["commits"] == 2
    task = json.loads((board / f"{task_id}.json").read_text())
    assert task["category"] == "independent_review"
    assert task["author_family"] == "glm"
    assert task["lanes"] == ["go"]
    assert task["budget"] == runner.REVIEW_BUDGET
    assert task["artifacts"] == ["reply.txt"]
    assert task["tests"] == ["grid/tests/test_review_schema.py"]
    assert task["brief"] == f"grid/briefs/{task_id}.txt"
    assert task["inputs"] == [
        task["brief"],
        f"grid/board/review/{task_id}/src/app.py",
        f"grid/board/review/{task_id}/src/new.py",
        f"grid/board/review/{task_id}/diff.patch",
    ]
    # The tip versions are staged, not the base versions.
    assert (project / f"grid/board/review/{task_id}/src/app.py").read_text() == "VALUE = 2\n"
    patch = (project / f"grid/board/review/{task_id}/diff.patch").read_text()
    assert "-VALUE = 1" in patch and "+VALUE = 2" in patch and "+NEW = 3" in patch
    brief = (project / f"grid/briefs/{task_id}.txt").read_text()
    assert f"{base}..{tip}" in brief
    assert "raise VALUE" in brief and "add NEW" in brief
    assert "quote the diff" in brief
    assert "advisory" in brief and "not a finding" in brief  # handoff-5 B3 policy
    assert (project / "grid/tests/test_review_schema.py").read_text() == SCHEMA_TEST


def test_every_repository_read_travels_through_the_run_seam(tmp_path, monkeypatch):
    repo, base, tip = reviewed_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    seen = []
    real = branch_review.run

    def spy(argv):
        seen.append(argv)
        return real(argv)

    monkeypatch.setattr(branch_review, "run", spy)
    branch_review.review_branch(board, project, {"repo": str(repo), "base": base, "tip": tip})
    assert ["git", "-C", str(repo), "diff", "--name-only", f"{base}..{tip}"] in seen
    assert any(argv[3] == "log" for argv in seen)
    assert any(argv[3] == "show" for argv in seen)


def test_review_branch_refuses_paths_outside_the_allow_list(tmp_path):
    repo, base, _ = reviewed_repo(tmp_path)
    (repo / "README.md").write_text("root-level file\n")
    git(repo, "add", "-A")
    commit(repo, "touch the readme")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="allow-list"):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": base, "tip": tip}
        )
    assert not any(board.rglob("*.json"))  # nothing written


def test_review_branch_refuses_credential_looking_paths(tmp_path):
    repo, base, _ = reviewed_repo(tmp_path)
    (repo / "src/auth.json").write_text("{}\n")
    git(repo, "add", "-A")
    commit(repo, "add auth")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="credential"):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": base, "tip": tip}
        )


def test_review_branch_refuses_mixed_trailer_families(tmp_path):
    repo, base, tip = reviewed_repo(
        tmp_path, second_trailer="Co-Authored-By: Claude <noreply@anthropic.com>"
    )
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="mixed"):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": base, "tip": tip}
        )
    assert not any(board.rglob("*.json"))


def test_review_branch_refuses_an_existing_task(tmp_path):
    repo, base, tip = reviewed_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    branch_review.review_branch(board, project, {"repo": str(repo), "base": base, "tip": tip})
    with pytest.raises(FileExistsError):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": base, "tip": tip}
        )


def test_review_branch_overrides_lanes_and_budget(tmp_path):
    repo, base, tip = reviewed_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board,
        project,
        {"repo": str(repo), "base": base, "tip": tip},
        lanes=["go-kimi", "go-deepseek"],
        budget={"wall_seconds": 900, "output_bytes": 2000000, "thinking_tokens": 12000},
    )
    task = json.loads(Path(created["task"]).read_text())
    assert task["lanes"] == ["go-kimi", "go-deepseek"]
    assert task["budget"]["thinking_tokens"] == 12000
