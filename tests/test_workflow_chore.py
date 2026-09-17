"""The chore workflow: code owns scout/build/gates/review/land-request, each
phase a typed Envelope logged to the ledger's events table. Fake build/review
agents stand in for the real lanes (wiring them up is a later row); the gate
phase runs a real pytest against a fixture repo so the "gates" node is
exercised for real, not mocked."""

import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import select

from inference_grid.ledger import Ledger, events
from inference_grid.workflows import chore

PY = sys.executable


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _fixture_repo(tmp_path):
    """A tiny real git repo with one buggy module and one test that pins it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "thing.py").write_text("def thing():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_thing.py").write_text(
        "from thing import thing\n\n\ndef test_thing():\n    assert thing() == 2\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _brief(repo, chore_id="chore-fixture-1"):
    return {
        "id": chore_id,
        "repo": str(repo),
        "paths": ["thing.py"],
        "description": "fix thing() to return 2",
        "gates": [
            {
                "name": "pytest",
                "argv": [PY, "-m", "pytest", "-q", "tests/test_thing.py"],
                "env": {"PYTHONPATH": "."},
            }
        ],
    }


def fixing_build_agent(brief, envelopes):
    repo = brief["repo"]
    (open(os.path.join(repo, "thing.py"), "w")).write("def thing():\n    return 2\n")
    return chore.Envelope(
        "build", "success", "fixed thing() to return 2", data={"branch": "packet/fixture-1"}
    )


def broken_build_agent(brief, envelopes):
    """Leaves the bug in place: the gates phase must fail for real."""
    return chore.Envelope(
        "build",
        "success",
        "declared done but did not touch the file",
        data={"branch": "packet/fixture-1"},
    )


def approving_review_agent(brief, envelopes):
    return chore.Envelope(
        "review",
        "success",
        "approved: the fix matches the described bug",
        data={"family": "other-family", "verdict": "approved"},
    )


@pytest.fixture
def grid(tmp_path):
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    return ledger


def _phase_events(ledger, workflow_id):
    with ledger.engine.connect() as con:
        rows = list(
            con.execute(
                select(events).where(events.c.kind == "workflow_phase").order_by(events.c.at)
            ).mappings()
        )
    return [r for r in rows if r["detail"]["workflow_id"] == workflow_id]


def test_chore_end_to_end_lands(tmp_path, grid):
    repo = _fixture_repo(tmp_path)
    brief = _brief(repo)
    report_dir = tmp_path / "reports"

    envelopes = chore.run_chore(
        brief,
        repo=repo,
        build_agent=fixing_build_agent,
        review_agent=approving_review_agent,
        report_dir=report_dir,
        ledger=grid,
    )

    assert list(envelopes) == ["scout", "build", "gates", "review", "land_request"]
    assert envelopes["scout"].data["files"] == {"thing.py": True}
    assert envelopes["gates"].status == "success"
    assert envelopes["review"].data["verdict"] == "approved"
    assert envelopes["land_request"].data["ready_to_land"] is True

    land_path = report_dir / f"{brief['id']}.md"
    assert land_path == report_dir / "chore-fixture-1.md"
    text = land_path.read_text()
    assert "chore-fixture-1" in text
    assert "Approve?" in text
    assert "Status: done" in text

    logged = _phase_events(grid, brief["id"])
    assert [row["detail"]["envelope"]["phase"] for row in logged] == [
        "scout",
        "build",
        "gates",
        "review",
        "land_request",
    ]
    assert all(row["detail"]["workflow"] == "chore" for row in logged)


def test_chore_gate_failure_blocks_the_land_request(tmp_path, grid):
    repo = _fixture_repo(tmp_path)
    brief = _brief(repo, chore_id="chore-fixture-2")
    report_dir = tmp_path / "reports"

    envelopes = chore.run_chore(
        brief,
        repo=repo,
        build_agent=broken_build_agent,
        review_agent=approving_review_agent,
        report_dir=report_dir,
        ledger=grid,
    )

    assert envelopes["gates"].status == "fail"
    assert envelopes["land_request"].data["ready_to_land"] is False
    text = (report_dir / "chore-fixture-2.md").read_text()
    assert "blocked (gates failed)" in text


def test_agent_callable_must_return_its_own_phase(tmp_path, grid):
    repo = _fixture_repo(tmp_path)
    brief = _brief(repo, chore_id="chore-fixture-3")

    def wrong_phase_agent(brief, envelopes):
        return chore.Envelope("scout", "success", "wrong phase entirely")

    with pytest.raises(ValueError, match="build_agent must return a build envelope"):
        chore.run_chore(
            brief,
            repo=repo,
            build_agent=wrong_phase_agent,
            review_agent=approving_review_agent,
            report_dir=tmp_path / "reports",
            ledger=grid,
        )


def test_run_chore_without_a_ledger_still_runs(tmp_path):
    """A ledger is optional (`ledger=None` is the default); phases still run."""
    repo = _fixture_repo(tmp_path)
    brief = _brief(repo, chore_id="chore-fixture-4")
    envelopes = chore.run_chore(
        brief,
        repo=repo,
        build_agent=fixing_build_agent,
        review_agent=approving_review_agent,
        report_dir=tmp_path / "reports",
    )
    assert envelopes["land_request"].data["ready_to_land"] is True


def test_load_brief_requires_the_named_keys(tmp_path):
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"id": "x"}))
    with pytest.raises(ValueError, match="missing required key"):
        chore.load_brief(path)


def test_dry_run_prints_the_phase_plan(tmp_path, capsys):
    repo = _fixture_repo(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(_brief(repo)))

    code = chore.main(["--dry-run", str(brief_path)])

    assert code == 0
    out = capsys.readouterr().out
    assert "chore chore-fixture-1: fix thing() to return 2" in out
    for name, kind, _ in chore.PHASES:
        assert f"{name} ({kind})" in out


def test_running_for_real_from_the_cli_is_not_yet_wired(tmp_path, capsys):
    repo = _fixture_repo(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(_brief(repo)))

    code = chore.main([str(brief_path)])

    assert code == 2
    assert "later row" in capsys.readouterr().err
