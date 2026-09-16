"""review-branch: an independent_review task authored from a git range, offline."""

import json
import subprocess
from pathlib import Path, PurePosixPath

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
        branch_review.review_branch(board, project, {"repo": str(repo), "base": base, "tip": tip})
    assert not any(board.rglob("*.json"))  # nothing written


def test_two_staged_files_sharing_a_basename_are_disambiguated(tmp_path):
    """Review packets are flat, so spine/x.py and api/x.py would collide in the scratch
    directory and the runner would refuse the packet. The colliding copies are staged
    under path-qualified names; unique basenames keep their plain path."""
    repo, base, _ = reviewed_repo(tmp_path)
    (repo / "src/api").mkdir()
    (repo / "src/api/app.py").write_text("ROUTE = 1\n")
    git(repo, "add", "-A")
    commit(repo, "a second app.py", "Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    out = branch_review.review_branch(board, project, {"repo": str(repo), "base": base, "tip": tip})
    staged = out["tasks"][0]["staged"] if "tasks" in out else out["staged"]
    names = sorted(PurePosixPath(s).name for s in staged if s.endswith(".py"))
    assert names == ["new.py", "src__api__app.py", "src__app.py"]
    task = json.loads(next(board.glob("*.json")).read_text())
    assert len(task["inputs"]) == len(set(PurePosixPath(i).name for i in task["inputs"]))


def test_a_commit_split_honours_the_per_project_allow_list(tmp_path):
    """The range check took allowed_prefixes but the per-commit check fell back to the
    default list, so a project whose code lives under frontend/ could never split."""
    repo, base, _ = reviewed_repo(tmp_path)
    (repo / "frontend").mkdir()
    (repo / "frontend/app.ts").write_text("export const V = 1;\n")
    git(repo, "add", "-A")
    commit(repo, "add the frontend", "Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    out = branch_review.review_branch(
        board,
        project,
        {"repo": str(repo), "base": f"{tip}^", "tip": tip},
        split="commit",
        allowed_prefixes=["src/", "frontend/"],
    )
    assert len(out["tasks"]) == 1


def test_review_branch_refuses_credential_looking_paths(tmp_path):
    repo, base, _ = reviewed_repo(tmp_path)
    (repo / "src/auth.json").write_text("{}\n")
    git(repo, "add", "-A")
    commit(repo, "add auth")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="credential"):
        branch_review.review_branch(board, project, {"repo": str(repo), "base": base, "tip": tip})


def test_review_branch_refuses_mixed_trailer_families(tmp_path):
    repo, base, tip = reviewed_repo(
        tmp_path, second_trailer="Co-Authored-By: Claude <noreply@anthropic.com>"
    )
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="mixed"):
        branch_review.review_branch(board, project, {"repo": str(repo), "base": base, "tip": tip})
    assert not any(board.rglob("*.json"))


def test_review_branch_refuses_an_existing_task(tmp_path):
    repo, base, tip = reviewed_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    branch_review.review_branch(board, project, {"repo": str(repo), "base": base, "tip": tip})
    with pytest.raises(FileExistsError):
        branch_review.review_branch(board, project, {"repo": str(repo), "base": base, "tip": tip})


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
    # An explicit budget wins over the sized default, and the summary records what was chosen.
    assert created["budget"] == task["budget"]
    assert created["staged_bytes"] > 0


def test_sized_thinking_tokens_tracks_the_packet():
    assert branch_review.sized_thinking_tokens(16_000) == 6000  # small packet: the floor
    assert branch_review.sized_thinking_tokens(60_000) == 15000  # cap 49 000 on the go lane
    assert branch_review.sized_thinking_tokens(120_000) == 24000  # the proven ceiling


def test_three_packet_sizes_ask_for_three_budgets(tmp_path):
    """The default thinking budget follows the staged bytes, not a constant: the go lane
    derives max_tokens from thinking_tokens (three times it plus its content allowance),
    so the fixed 6 000 once capped every request at 22 000 whatever the packet weighed —
    the 46 KB vix-rs packet needed three attempts on that cap."""
    budgets = []
    # Files over the 16 KB per-file cap ride in the patch alone, so the large case uses
    # five smaller files: staged ~50 KB plus the ~50 KB patch, under the 120 KB budget.
    for index, files_per_group, size in ((0, 2, 100), (1, 2, 9_000), (2, 5, 10_000)):
        repo, base, tip, _ = layered_repo(
            tmp_path / f"run{index}", groups=1, files_per_group=files_per_group, size=size
        )
        board, project = board_and_project(tmp_path / f"run{index}")
        created = branch_review.review_branch(
            board, project, {"repo": str(repo), "base": base, "tip": tip}
        )
        task = json.loads(Path(created["task"]).read_text())
        assert created["staged_bytes"] > 0
        assert created["budget"] == task["budget"]
        assert task["budget"]["thinking_tokens"] == branch_review.sized_thinking_tokens(
            created["staged_bytes"]
        )
        budgets.append(task["budget"]["thinking_tokens"])
    assert budgets[0] == branch_review.MIN_THINKING_TOKENS
    assert budgets[-1] == branch_review.MAX_THINKING_TOKENS
    assert budgets == sorted(set(budgets))  # three distinct, rising with the packet


def test_a_split_sizes_each_packet_on_its_own_bytes(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path, groups=2, files_per_group=1, size=15_000)
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, split="commit"
    )
    assert len(created["tasks"]) == 2
    for entry in created["tasks"]:
        task = json.loads(Path(entry["task"]).read_text())
        assert task["budget"]["thinking_tokens"] == branch_review.sized_thinking_tokens(
            entry["staged_bytes"]
        )
    # Sized per packet, not per range: the whole range's bytes would ask for more.
    whole = sum(entry["staged_bytes"] for entry in created["tasks"])
    assert created["tasks"][0]["budget"]["thinking_tokens"] < branch_review.sized_thinking_tokens(
        whole
    )


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
    big = repo / "src/big.py"
    big.write_text(
        "BIG = [\n" + "".join(f'    "line {i} pad pad pad",\n' for i in range(1200)) + "]\n"
    )
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
    staged_first = next(p for p in by_id[first_task_id]["staged"] if p.endswith("src/app.py"))
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


def test_exclude_commits_skips_the_listed_commits(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path, groups=3)
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board,
        project,
        {"repo": str(repo), "base": base, "tip": tip},
        max_input_bytes=2000,
        split="commit",
        exclude_commits=[hashes[1][:7]],
    )
    assert [t["id"] for t in created["tasks"]] == [
        f"review-factory-frontend-{hashes[0][:7]}",
        f"review-factory-frontend-{hashes[2][:7]}",
    ]
    assert created["excluded"] == [{"commit": hashes[1][:7], "note": "excluded by operator"}]


def write_verify_merge_task(board, branch, target, state="passed"):
    """A verify_merge task on the board, the record the branch-scope review gates on."""
    board.mkdir(parents=True, exist_ok=True)
    task = {
        "id": "verify-merge-1",
        "category": "verify_merge",
        "brief": "grid/briefs/verify-merge-1.txt",
        "inputs": ["grid/briefs/verify-merge-1.txt"],
        "tests": ["out.json"],
        "artifacts": ["out.json"],
        "lanes": ["code-node"],
        "author_family": None,
        "budget": {"wall_seconds": 600, "output_bytes": 100000, "thinking_tokens": None},
        "state": state,
        "blocked_reason": None if state == "passed" else "verify_merge: conflict",
        "spec": {
            "branch": branch,
            "target": target,
            "gates": [{"name": "noop", "argv": ["true"]}],
        },
    }
    (board / "verify-merge-1.json").write_text(json.dumps(task, indent=1) + "\n")
    return task


def test_branch_scope_authors_one_packet_for_the_merged_tree(tmp_path):
    repo, base, tip = reviewed_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    write_verify_merge_task(board, tip, base)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip}, scope="branch"
    )
    assert created["id"] == f"review-factory-frontend-branch-{tip[:7]}"
    assert created["scope"] == "branch" and created["commits"] == 2
    task = json.loads((board / f"{created['id']}.json").read_text())
    assert task["category"] == "independent_review" and task["author_family"] == "glm"
    assert any("src/app.py" in path for path in task["inputs"])
    brief = (project / f"grid/briefs/{created['id']}.txt").read_text()
    assert "target's current tree" in brief
    assert "not the branch's history" in brief


def merged_repo(tmp_path):
    """target and branch each change a different region of one file, so the merge combines
    them: staging the merge result is observably not staging the branch tip."""
    repo = tmp_path / "factory-frontend"
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src/app.py").write_text("one\ntwo\nthree\nfour\nfive\n")
    git(repo, "add", "-A")
    commit(repo, "base")
    base = rev(repo, "HEAD")
    git(repo, "checkout", "-q", "-b", "feature", base)
    (repo / "src/app.py").write_text("one-changed\ntwo\nthree\nfour\nfive\n")
    git(repo, "add", "-A")
    commit(repo, "branch edits the top", "Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    branch = rev(repo, "HEAD")
    git(repo, "checkout", "-q", "main")
    (repo / "src/app.py").write_text("one\ntwo\nthree\nfour\nfive-changed\n")
    git(repo, "add", "-A")
    commit(repo, "target edits the bottom")
    target = rev(repo, "HEAD")
    return repo, target, branch


def test_branch_scope_stages_the_merged_tree_not_the_branch_tip(tmp_path):
    repo, target, branch = merged_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    write_verify_merge_task(board, branch, target)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": target, "tip": branch}, scope="branch"
    )
    staged = next(p for p in created["staged"] if p.endswith("src/app.py"))
    text = (project / staged).read_text()
    assert "one-changed" in text and "five-changed" in text  # the merge, not either side
    tip = subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:src/app.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "five-changed" not in tip  # the branch alone never carried the target's edit
    patch = (project / f"grid/board/review/{created['id']}/diff.patch").read_text()
    assert "one-changed" in patch and "five-changed" not in patch


def test_branch_scope_needs_a_passed_verify_merge_task(tmp_path):
    repo, target, branch = merged_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="passed verify_merge"):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": target, "tip": branch}, scope="branch"
        )
    write_verify_merge_task(board, branch, target, state="blocked")
    with pytest.raises(ValueError, match="passed verify_merge"):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": target, "tip": branch}, scope="branch"
        )
    assert not any(board.glob("review-*.json"))  # nothing authored


def test_scope_is_accepted_in_the_spec_and_an_unknown_scope_refuses(tmp_path):
    repo, target, branch = merged_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    write_verify_merge_task(board, branch, target)
    created = branch_review.review_branch(
        board,
        project,
        {"repo": str(repo), "base": target, "tip": branch, "scope": "branch"},
    )
    assert created["scope"] == "branch"
    with pytest.raises(ValueError, match="scope must be"):
        branch_review.review_branch(
            board,
            project / "grid/board2",
            {"repo": str(repo), "base": target, "tip": branch},
            scope="sideways",
        )


def test_exclude_commits_refuses_single_mode_and_unknown_spec_keys(tmp_path):
    repo, base, tip, hashes = layered_repo(tmp_path, groups=2)
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="exclude_commits names commit"):
        branch_review.review_branch(
            board,
            project,
            {"repo": str(repo), "base": base, "tip": tip},
            exclude_commits=[hashes[0][:7]],
        )
    with pytest.raises(ValueError, match="accepts only repo, base, tip") as excinfo:
        branch_review.review_branch(
            board,
            project,
            {"repo": str(repo), "base": base, "tip": tip, "max_input_bytes": 999},
        )
    assert "max_input_bytes" in str(excinfo.value)
    with pytest.raises(ValueError, match="sha prefixes"):
        branch_review.review_branch(
            board,
            project,
            {"repo": str(repo), "base": base, "tip": tip},
            exclude_commits=["not a sha"],
        )
    assert not (board / "review").exists() or not any((board / "review").rglob("*"))


def one_commit_two_groups_repo(tmp_path, src_size=900, tests_size=900):
    """A repo whose whole range is one commit touching src/ and tests/."""
    repo = tmp_path / "factory-frontend"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src/seed.py").write_text("SEED = 0\n")
    git(repo, "add", "-A")
    commit(repo, "base")
    base = rev(repo, "HEAD")
    (repo / "src/app.py").write_text("VALUE = " + "x" * src_size + "\n")
    (repo / "tests/test_app.py").write_text("EXPECTED = " + "y" * tests_size + "\n")
    git(repo, "add", "-A")
    commit(repo, "add the app and its test", trailer="Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>")
    return repo, base, rev(repo, "HEAD")


def test_a_single_over_budget_commit_splits_by_top_level_directory(tmp_path):
    """The range a whole-repository review needs is often one commit, so the per-commit
    split cannot help: the commit splits again, one task per top-level directory, each
    packet measured against the budget before anything is written."""
    repo, base, tip = one_commit_two_groups_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board,
        project,
        {"repo": str(repo), "base": base, "tip": tip},
        split="commit",
        max_input_bytes=2500,
    )
    assert [entry["id"] for entry in created["tasks"]] == [
        f"review-factory-frontend-{tip[:7]}-src",
        f"review-factory-frontend-{tip[:7]}-tests",
    ]
    for entry in created["tasks"]:
        assert 0 < entry["staged_bytes"] <= 2500
        task = json.loads(Path(entry["task"]).read_text())
        assert task["state"] == "ready"
    # Each packet stages and diffs only its own group.
    src_task = created["tasks"][0]
    assert any(p.endswith("src/app.py") for p in src_task["staged"])
    assert not any(p.startswith("grid/board/review") and "tests/" in p for p in src_task["staged"])
    src_patch = (project / f"grid/board/review/{src_task['id']}/diff.patch").read_text()
    assert "VALUE = " in src_patch and "EXPECTED = " not in src_patch
    # The brief names the group under review and lists the sibling reviews.
    brief = Path(src_task["brief"]).read_text()
    assert "restricted to the src files" in brief
    assert f"review-factory-frontend-{tip[:7]}-tests" in brief


def test_paths_restrict_staging_and_the_patch(tmp_path):
    repo, base, _ = reviewed_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs/notes.md").write_text("# notes\n")
    git(repo, "add", "-A")
    commit(repo, "note the docs")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    created = branch_review.review_branch(
        board, project, {"repo": str(repo), "base": base, "tip": tip, "paths": ["src/"]}
    )
    task = json.loads(Path(created["task"]).read_text())
    assert all("src/" in p for p in task["inputs"][1:-1])
    assert not any("docs/notes.md" in p for p in task["inputs"])
    patch = (project / f"grid/board/review/{created['id']}/diff.patch").read_text()
    assert "VALUE = 2" in patch and "notes.md" not in patch
    brief = Path(created["brief"]).read_text()
    assert "src/" in brief and "covers only them" in brief


def test_a_label_lets_two_path_filtered_reviews_of_one_tip_coexist(tmp_path):
    """The id is review-<repo>-<tip>; two filtered packets of the same range collided on it
    (COT's four engine commits, reviewed as code and as report). A label suffixes the id."""
    repo, base, _ = reviewed_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs/notes.md").write_text("# notes\n")
    git(repo, "add", "-A")
    commit(repo, "note the docs")
    tip = rev(repo, "HEAD")
    board, project = board_and_project(tmp_path)
    spec = {"repo": str(repo), "base": base, "tip": tip}
    code = branch_review.review_branch(
        board, project, {**spec, "paths": ["src/"], "label": "code"})
    docs = branch_review.review_branch(
        board, project, {**spec, "paths": ["docs/"], "label": "docs"})
    assert code["id"].endswith("-code") and docs["id"].endswith("-docs")
    assert code["id"][: -len("-code")] == docs["id"][: -len("-docs")]
    assert (board / (code["id"] + ".json")).exists() and (board / (docs["id"] + ".json")).exists()
    with pytest.raises(ValueError, match="label must be"):
        branch_review.review_branch(
            board, project, {**spec, "paths": ["src/"], "label": "Code Group!"})
    with pytest.raises(ValueError, match="only meaningful with a paths filter"):
        branch_review.review_branch(board, project, {**spec, "label": "whole"})


def test_paths_entries_are_validated(tmp_path):
    repo, base, tip = reviewed_repo(tmp_path)
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="safe relative path"):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": base, "tip": tip, "paths": ["../up"]}
        )
    with pytest.raises(ValueError, match="no changed files under the requested path filter"):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": base, "tip": tip, "paths": ["web/"]}
        )
    with pytest.raises(ValueError, match="non-empty path prefixes"):
        branch_review.review_branch(
            board, project, {"repo": str(repo), "base": base, "tip": tip, "paths": ["src/", ""]}
        )


def test_an_over_budget_directory_is_refused_with_its_byte_count(tmp_path):
    """A directory that still exceeds the budget after the split is refused with its
    size, never forced; nothing is written."""
    repo, base, tip = one_commit_two_groups_repo(tmp_path, src_size=1400, tests_size=40)
    board, project = board_and_project(tmp_path)
    with pytest.raises(ValueError, match="exceeds the 2500-byte budget") as excinfo:
        branch_review.review_branch(
            board,
            project,
            {"repo": str(repo), "base": base, "tip": tip},
            split="commit",
            max_input_bytes=2500,
        )
    message = str(excinfo.value)
    assert "src (" in message and "bytes:" in message
    assert not any(board.glob("review-*.json"))
    assert not (board / "review").exists() or not any((board / "review").rglob("*"))
    assert not any((project / "grid/briefs").glob("review-factory-frontend-*"))
