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


def commit(repo, message, trailer=None, body=None):
    args = ["commit", "-q", "-m", message]
    if body:
        args += ["-m", body]
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


def layered_repo(tmp_path, groups=3, files_per_group=2, size=300):
    """A repo whose range is `groups` commits of `files_per_group` new ~`size`-byte files."""
    repo = tmp_path / "factory-frontend"
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src/seed.py").write_text("SEED = 0\n")
    git(repo, "add", "-A")
    commit(repo, "base")
    base = rev(repo, "HEAD")
    hashes = []
    for i in range(groups):
        for j in range(files_per_group):
            (repo / f"src/f{i}_{j}.py").write_text("VALUE = " + "x" * size + f"\n#{i} {j}\n")
        git(repo, "add", "-A")
        commit(
            repo,
            f"change group {i}",
            trailer="Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>",
            body=f"body line for group {i}",
        )
        hashes.append(rev(repo, "HEAD"))
    return repo, base, hashes[-1], hashes


def test_generated_and_locked_content_is_excluded_and_named_in_the_brief(tmp_path):
    repo, base, tip = reviewed_repo(tmp_path)
    lock = {"name": "web/package-lock.json", "content": '{"lockfileVersion": 3}\n'}
    fixture = {"name": "src/fixtures/samples.json", "content": '{"sample": 1}\n'}
    (repo / "web").mkdir()
    for item in (lock, fixture):
        path = repo / item["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"])
    git(repo, "add", "-A")
    commit(repo, "add fixtures and a lockfile")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}
    )
    task = json.loads(Path(created["task"]).read_text())
    assert all(lock["name"] not in p for p in task["inputs"])
    assert all(fixture["name"] not in p for p in task["inputs"])
    assert created["omitted"] == [fixture["name"], lock["name"]]
    brief = Path(created["brief"]).read_text()
    assert "2 generated file(s) omitted" in brief
    assert fixture["name"] in brief and lock["name"] in brief


def test_oversized_files_are_represented_by_their_hunks_only(tmp_path):
    repo, base, tip = reviewed_repo(tmp_path)
    big = (repo / "src/big.py")
    big.write_text("BIG = [\n" + "".join(f'    "line {i} pad pad pad",\n' for i in range(1200)) + "]\n")
    assert big.stat().st_size > branch_review.MAX_FILE_BYTES
    git(repo, "add", "-A")
    commit(repo, "add a big table")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}
    )
    task = json.loads(Path(created["task"]).read_text())
    assert created["oversized"] == ["src/big.py"]
    assert all("src/big.py" not in p for p in task["inputs"] if p != task["inputs"][-1])
    patch = (project / f"grid/board/review/{created['id']}/diff.patch").read_text()
    assert "line 1199" in patch  # the hunks carry the file
    brief = Path(created["brief"]).read_text()
    assert "per-file cap" in brief and "src/big.py" in brief


def test_an_over_budget_range_refuses_naming_the_commit_split(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="plan is:") as excinfo:
        branch_review.review_branch(
            board,
            project,
            {"repo": str(repo), "base": base, "tip": tip},
            max_input_bytes=2000,
            split="none",
        )
    message = str(excinfo.value)
    assert "over the 2000-byte budget" in message
    for digest in hashes:
        assert f"review-factory-frontend-{digest[:7]}" in message


def test_commit_split_authors_one_task_per_commit_with_disjoint_inputs(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, max_input_bytes=2000
    )
    assert set(created) == {"tasks"}
    assert [t["id"] for t in created["tasks"]] == [
        f"review-factory-frontend-{digest[:7]}" for digest in hashes
    ]
    inputs = [set(t["staged"]) for t in created["tasks"]]
    assert inputs[0].isdisjoint(inputs[1]) and inputs[0].isdisjoint(inputs[2])
    for index, entry in enumerate(created["tasks"]):
        task = json.loads(Path(entry["task"]).read_text())
        assert task["state"] == "ready" and entry["commits"] == 1
        brief = Path(entry["brief"]).read_text()
        assert f"change group {index}" in brief
        assert f"body line for group {index}" in brief


def test_an_explicit_commit_split_applies_even_under_budget(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, split="commit"
    )
    assert [t["id"] for t in created["tasks"]] == [
        f"review-factory-frontend-{digest[:7]}" for digest in hashes
    ]


def test_a_commit_over_the_budget_alone_is_refused_with_file_sizes(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path, groups=2, files_per_group=2, size=600)
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="exceeds the 1500-byte budget") as excinfo:
        branch_review.review_branch(
            board,
            project,
            {"repo": str(repo), "base": base, "tip": tip},
            max_input_bytes=1500,
        )
    message = str(excinfo.value)
    assert "bytes:" in message and "src/f" in message  # the offending file sizes
    assert not (board / "review").exists()  # nothing was written before the refusal


def test_the_brief_opens_with_a_single_review_prefix(tmp_path):
    # review_brief_text prepends review- to the task id; a branch-review id already has
    # it, and the live briefs read "task review-review-ff-glm-r6-…".
    repo, base, tip = reviewed_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}
    )
    brief = Path(created["brief"]).read_text()
    assert brief.startswith(f"You are an independent reviewer for task {created['id']}. ")
    assert "task review-review-" not in brief


def test_fixture_hunks_are_filtered_out_of_the_patch_before_the_budget(tmp_path):
    # The fixture-bundle commit was refused at 127,916 bytes although its staged source
    # files totalled ~17 KB: the hunks of excluded paths stayed in diff.patch.
    repo, base, _ = reviewed_repo(tmp_path)
    bundle = repo / "tests" / "fixtures"
    bundle.mkdir(parents=True)
    (bundle / "samples.json").write_text('{"rows": "' + "y" * (200 * 1024) + '"}\n')
    (repo / "src/small.py").write_text("SMALL = 1\n")
    git(repo, "add", "-A")
    commit(repo, "add the fixture bundle and one small module")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board,
        project,
        {"repo": str(repo), "base": base, "tip": tip},
        max_input_bytes=10000,
    )
    assert created["omitted"] == ["tests/fixtures/samples.json"]
    patch = (project / f"grid/board/review/{created['id']}/diff.patch").read_text()
    assert "samples.json" not in patch  # the excluded hunks are gone
    assert "SMALL = 1" in patch
    assert len(patch.encode()) < 10000  # the packet fits where it used to blow up


def test_filter_patch_handles_both_seam_shapes():
    # Range output (git diff) and per-commit output (git diff-tree -p) are both blocks
    # starting with 'diff --git'; excluded paths drop, the rest survives verbatim.
    patch = (
        "diff --git a/tests/fixtures/x.json b/tests/fixtures/x.json\n"
        "--- a/tests/fixtures/x.json\n"
        "+++ b/tests/fixtures/x.json\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )
    filtered, dropped = branch_review._filter_patch(patch, {})
    assert dropped == ["tests/fixtures/x.json"]
    assert "fixtures" not in filtered
    assert "VALUE = 2" in filtered and "diff --git a/src/app.py b/src/app.py" in filtered


def test_a_mid_split_refusal_writes_nothing(tmp_path):
    # The split once wrote eight task files and then died on the first one's id. Every
    # part is planned before any part is written: a refusal mid-plan leaves the board
    # untouched.
    repo, base, tip, _ = layered_repo(tmp_path, groups=2, files_per_group=2, size=300)
    # Grow the second group's files so only its packet blows a 2000-byte budget.
    for j in range(2):
        (repo / f"src/f1_{j}.py").write_text("VALUE = " + "x" * 3000 + f"\n#1 {j}\n")
    git(repo, "add", "-A")
    commit(repo, "grow group 1", trailer="Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="exceeds the 2000-byte budget"):
        branch_review.review_branch(
            board,
            project,
            {"repo": str(repo), "base": base, "tip": tip},
            max_input_bytes=2000,
        )
    assert not (board / "review").exists() or not any((board / "review").rglob("*"))
    assert not any(board.glob("review-*.json"))
    assert not any((project / "grid/briefs").glob("review-factory-frontend-*"))


def test_a_rerun_skips_commits_that_already_have_tasks(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    first = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, max_input_bytes=2000
    )
    assert len(first["tasks"]) == 3 and "skipped" not in first
    # A re-run over the same range authors nothing and says what it skipped.
    again = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, max_input_bytes=2000
    )
    assert again["tasks"] == []
    assert [entry["id"] for entry in again["skipped"]] == [
        f"review-factory-frontend-{digest[:7]}" for digest in hashes
    ]
    # A partial board resumes: drop the middle task file and only it is re-authored.
    middle = board / f"review-factory-frontend-{hashes[1][:7]}.json"
    middle.unlink()
    resumed = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, max_input_bytes=2000
    )
    assert [t["id"] for t in resumed["tasks"]] == [f"review-factory-frontend-{hashes[1][:7]}"]
    assert len(resumed["skipped"]) == 2


def test_docs_only_commits_get_no_reviewer_unless_asked(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path, groups=2)
    (repo / "docs").mkdir()
    (repo / "docs/observations.md").write_text("# Baseline log\n\nNothing behavioural.\n")
    git(repo, "add", "-A")
    commit(repo, "note the baseline", trailer="Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, max_input_bytes=2000
    )
    assert len(created["tasks"]) == 2  # the two code groups
    assert created["docs_only"] == [
        {"commit": tip[:7], "note": "docs-only, not reviewed", "paths": ["docs/observations.md"]}
    ]
    # include_docs on a re-run skips the two authored code tasks and reviews only the
    # notes commit.
    included = branch_review.review_branch(
        board,
        project,
        {"repo": str(repo), "base": base, "tip": tip},
        max_input_bytes=2000,
        include_docs=True,
    )
    assert len(included["tasks"]) == 1 and "docs_only" not in included
    assert len(included["skipped"]) == 2


def test_split_packets_stage_files_as_of_their_commit(tmp_path):
    # review-ff-glm-r6-f383c9d was rejected on a ghost: the packet held the commit's own
    # hunks but the file contents at the range tip, two rounds later. Split packets now
    # stage git show <sha>:<path>; only the unsplit mode stages the tip.
    repo = tmp_path / "factory-frontend"
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src/seed.py").write_text("SEED = 0\n")
    git(repo, "add", "-A")
    commit(repo, "base")
    base = rev(repo, "HEAD")
    (repo / "src/app.py").write_text("VALUE = 1\n")
    git(repo, "add", "-A")
    commit(repo, "add app", trailer="Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    first = rev(repo, "HEAD")
    (repo / "src/app.py").write_text("VALUE = 2\n# pytestmark added later\n")
    git(repo, "add", "-A")
    commit(repo, "evolve app", trailer="Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)

    split = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, split="commit"
    )
    by_id = {entry["id"]: entry for entry in split["tasks"]}
    first_task_id = f"review-factory-frontend-{first[:7]}"
    staged_first = next(
        p for p in by_id[first_task_id]["staged"] if p.endswith("src/app.py")
    )
    at_commit = subprocess.run(
        ["git", "-C", str(repo), "show", f"{first}:src/app.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    staged_text = (project / staged_first).read_text()
    assert staged_text == at_commit == "VALUE = 1\n"
    assert "as of this commit" in Path(by_id[first_task_id]["brief"]).read_text()

    # The unsplit mode still stages the tip version (its id would collide with the
    # second split task's, so it lands on its own board).
    whole = branch_review.review_branch(
        project / "grid/board2", project, {"repo": str(repo), "base": base, "tip": tip}
    )
    staged_whole = next(p for p in whole["staged"] if p.endswith("src/app.py"))
    tip_text = (project / staged_whole).read_text()
    assert tip_text == "VALUE = 2\n# pytestmark added later\n"
