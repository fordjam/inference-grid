"""`inference-grid state-migrate --repo <path> --dry-run`: plan only, never move.

Every test works against a temp directory built to look like a product repo; nothing
here ever points at a real repo or ~/.local/share.
"""

import pytest

from inference_grid.board.new import BOARD_README_TEMPLATE
from inference_grid.state_migrate import board_gates, migration_plan, state_migrate


def make_repo(
    tmp_path, board=True, briefs=True, handoffs=(), reports=False, readme=None, name="product-repo", git=True
):
    repo = tmp_path / name
    repo.mkdir()
    if git:
        (repo / ".git").mkdir()
    if board:
        (repo / "grid" / "board" / "review").mkdir(parents=True)
        (repo / "grid" / "board" / "t1.json").write_text("{}")
        (repo / "grid" / "board" / "review" / "r1.json").write_text("{}")
    if briefs:
        (repo / "grid" / "briefs").mkdir(parents=True)
        (repo / "grid" / "briefs" / "t1.txt").write_text("brief\n")
    for name in handoffs:
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / name).write_text("handoff\n")
    if reports:
        (repo / "docs" / "reports").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / "reports" / "r1.md").write_text("report\n")
    if readme is not None:
        (repo / "grid").mkdir(exist_ok=True)
        (repo / "grid" / "README.md").write_text(readme)
    return repo


# Rendered from the real board_init template (board/new.py), not hand-copied, so a
# template edit that would silently change gate parsing shows up here too.
README = BOARD_README_TEMPLATE.format(
    project="product-repo", tier="T1", prefixes="src/, tests/, docs/"
)


def test_plan_lists_every_present_state_path_and_nothing_absent(tmp_path):
    repo = make_repo(
        tmp_path,
        board=True,
        briefs=True,
        handoffs=("handoff-glm-1.md", "handoff-glm-2.md"),
        reports=True,
    )
    plan = migration_plan(repo, state_root=tmp_path / "state")
    froms = sorted(m["from"] for m in plan["moves"])
    assert froms == sorted(
        str(p)
        for p in (
            repo / "grid" / "board",
            repo / "grid" / "briefs",
            repo / "docs" / "handoff-glm-1.md",
            repo / "docs" / "handoff-glm-2.md",
            repo / "docs" / "reports",
        )
    )
    # grid/board/review travels with grid/board — it is not a separate move entry.
    assert not any(m["from"].endswith("review") for m in plan["moves"])


def test_plan_only_lists_what_actually_exists(tmp_path):
    repo = make_repo(tmp_path, board=True, briefs=False, handoffs=(), reports=False)
    plan = migration_plan(repo, state_root=tmp_path / "state")
    assert len(plan["moves"]) == 1
    assert plan["moves"][0]["from"] == str(repo / "grid" / "board")


def test_destination_is_under_state_root_named_by_the_repo(tmp_path):
    repo = make_repo(tmp_path, board=True, briefs=False)
    plan = migration_plan(repo, state_root=tmp_path / "state")
    assert plan["state_root"] == str(tmp_path / "state" / "product-repo")
    assert plan["moves"][0]["to"] == str(tmp_path / "state" / "product-repo" / "grid" / "board")


def test_default_state_root_is_under_local_share(tmp_path):
    repo = make_repo(tmp_path, board=True, briefs=False)
    plan = migration_plan(repo)  # no state_root: the real default
    assert plan["state_root"].endswith(
        "/.local/share/inference-grid/state/product-repo"
    )


def test_board_gates_parsed_from_grid_readme(tmp_path):
    repo = make_repo(tmp_path, board=False, briefs=False, readme=README)
    tier, prefixes = board_gates(repo)
    assert tier == "T1"
    assert prefixes == ["src/", "tests/", "docs/"]


def test_board_gates_absent_without_a_readme(tmp_path):
    repo = make_repo(tmp_path, board=False, briefs=False)
    assert board_gates(repo) == (None, None)


def test_grid_json_carries_the_board_name_and_gates(tmp_path):
    repo = make_repo(tmp_path, board=True, briefs=False, readme=README)
    plan = migration_plan(repo, state_root=tmp_path / "state")
    assert plan["grid_json"] == {
        "board": "product-repo",
        "gates": {"tier": "T1", "allowed_prefixes": ["src/", "tests/", "docs/"]},
    }


def test_state_migrate_refuses_anything_but_dry_run(tmp_path):
    repo = make_repo(tmp_path, board=True, briefs=False)
    with pytest.raises(ValueError):
        state_migrate(repo, dry_run=False)
    with pytest.raises(ValueError):
        state_migrate(repo, dry_run=None)
    # dry_run=True (the only supported value) still works and touches nothing.
    plan = state_migrate(repo, dry_run=True, state_root=tmp_path / "state")
    assert plan["moves"]
    assert (repo / "grid" / "board").exists()  # nothing was actually moved


def test_cli_round_trips_through_main(tmp_path, monkeypatch):
    import io
    import sys

    from inference_grid import cli

    repo = make_repo(tmp_path, board=True, briefs=True)
    monkeypatch.setattr(
        sys, "argv", ["inference-grid", "state-migrate", "--repo", str(repo), "--dry-run"]
    )
    buffer = io.StringIO()
    real = sys.stdout
    sys.stdout = buffer
    try:
        cli.main()
    finally:
        sys.stdout = real
    payload = __import__("json").loads(buffer.getvalue())
    assert payload["repo"] == str(repo.resolve())
    assert len(payload["moves"]) == 2


def test_cli_refuses_without_dry_run(tmp_path, monkeypatch):
    import sys

    from inference_grid import cli

    repo = make_repo(tmp_path, board=True, briefs=False)
    monkeypatch.setattr(sys, "argv", ["inference-grid", "state-migrate", "--repo", str(repo)])
    with pytest.raises(ValueError):
        cli.main()


def test_plan_never_writes_anything(tmp_path):
    repo = make_repo(
        tmp_path, board=True, briefs=True, handoffs=("handoff-glm-1.md",), reports=True
    )
    before = sorted(str(p) for p in repo.rglob("*"))
    migration_plan(repo, state_root=tmp_path / "state")
    after = sorted(str(p) for p in repo.rglob("*"))
    assert before == after
    assert not (tmp_path / "state").exists()


def test_refuses_a_repo_path_that_does_not_exist(tmp_path):
    with pytest.raises(ValueError):
        migration_plan(tmp_path / "does-not-exist", state_root=tmp_path / "state")


def test_refuses_a_directory_that_is_not_a_git_repository(tmp_path):
    repo = make_repo(tmp_path, board=True, briefs=False, git=False)
    with pytest.raises(ValueError):
        migration_plan(repo, state_root=tmp_path / "state")


def test_refuses_a_file_path_masquerading_as_a_repo(tmp_path):
    not_a_repo = tmp_path / "README.md"
    not_a_repo.write_text("not a repo\n")
    with pytest.raises(ValueError):
        migration_plan(not_a_repo, state_root=tmp_path / "state")


def test_refuses_an_ambiguous_destination_that_already_exists(tmp_path):
    # Two repos sharing a basename must not silently collapse into one state/<name>/ —
    # the plan refuses once that destination already exists, rather than guessing it's
    # the same repo as before.
    repo = make_repo(tmp_path, board=True, briefs=False)
    state_root = tmp_path / "state"
    (state_root / "product-repo").mkdir(parents=True)
    with pytest.raises(ValueError):
        migration_plan(repo, state_root=state_root)
    # An explicit --board name is the operator's own disambiguation and is honored.
    plan = migration_plan(repo, state_root=state_root, board="product-repo-worktree")
    assert plan["state_root"] == str(state_root / "product-repo-worktree")
    assert plan["grid_json"]["board"] == "product-repo-worktree"


def test_cli_scopes_repo_dry_run_and_board_to_state_migrate_only(tmp_path, monkeypatch):
    import sys

    from inference_grid import cli

    monkeypatch.setattr(
        sys,
        "argv",
        ["inference-grid", "status", "--repo", str(tmp_path), "--database", "sqlite:///:memory:"],
    )
    with pytest.raises(SystemExit):
        cli.main()
