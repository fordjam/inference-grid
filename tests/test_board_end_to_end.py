"""Offline end to end: a work task passes, its review approves, acceptance lands on grid/inbox.

The landing has never run outside unit tests; this drives the whole chain — real git repo,
fake lane adapters, temp ledger, temp home for the inbox worktree — and then checks what the
operator will see: the branch, the committed record, an untouched checkout, and no duplicate
commit when the acceptance is asked for twice.
"""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from inference_grid.board import runner
from inference_grid.ledger import Ledger


def git(project, *args):
    return subprocess.run(
        ["git", "-C", str(project), *args], capture_output=True, text=True, check=True
    )


def make_task(tid, brief_name, state="ready"):
    return dict(
        id=tid,
        category="pure_function",
        brief=brief_name,
        inputs=[brief_name, "mod.py"],
        tests=["test_mod2.py"],
        artifacts=["mod2.py"],
        lanes=["go"],
        author_family=None,
        budget={"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None},
        state=state,
        blocked_reason=None,
    )


def test_work_review_accept_land_end_to_end(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "grid/board").mkdir(parents=True)
    (project / "mod.py").write_text("VALUE = 1\n")
    (project / "brief.txt").write_text("copy mod.py to mod2.py\n")
    (project / "test_mod2.py").write_text(
        "import unittest\nimport mod2\n\nclass T(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(mod2.VALUE, 1)\n"
    )
    (project / "grid/tests").mkdir()
    (project / "grid/tests/test_review_schema.py").write_text(
        "import json, unittest\nfrom pathlib import Path\n\nclass T(unittest.TestCase):\n"
        "    def test_verdict(self):\n"
        "        self.assertIn(json.loads(Path('reply.txt').read_text())['verdict'], ('approved', 'rejected'))\n"
    )
    board = project / "grid/board"
    (board / "copy-ok.json").write_text(json.dumps(make_task("copy-ok", "brief.txt")))
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(
        project,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "base",
    )
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    # The inbox worktree must land under a temp home, never the operator's.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import hashlib, json, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "inputs = Path(request['input_directory']); out = Path(request['output_directory'])\n"
        "names = json.loads((inputs / 'expected.json').read_text())\n"
        "reject = 'brief-reject' in (inputs / 'brief.txt').read_text()\n"
        "artifacts = []\n"
        "for name in names:\n"
        "    if name == 'reply.txt':\n"
        "        review = {'verdict': 'rejected' if reject else 'approved', 'findings': [],"
        " 'checked': ['schema', 'verdict', 'findings']}\n"
        "        data = (json.dumps(review) + '\\n').encode()\n"
        "    else:\n"
        "        data = (inputs / 'mod.py').read_bytes()\n"
        "    (out / name).write_bytes(data)\n"
        "    artifacts.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest()})\n"
        "print(json.dumps({'status': 'completed', 'finish_reason': 'stop',"
        " 'actual_model': request['model'], 'manifest_sha256': request['manifest_sha256'],"
        " 'artifacts': artifacts}))\n"
    )
    monkeypatch.setattr(runner, "RUNNER", [sys.executable, str(adapter)])

    url = "sqlite:///" + str(tmp_path / "ledger.sqlite")
    ledger = Ledger(url)
    ledger.initialize()
    accounts_by_lane = {}
    lanes = {
        "go": {
            "provider": "opencode",
            "family": "glm",
            "model": "glm-5.3-flash",
            "kind": "go_http",
            "credential_path": None,
            "executable": None,
            "plan_units": {},
            "window": None,
            "max_concurrency": 1,
            "wall_seconds": 60,
            "categories": ["pure_function", "independent_review"],
        },
        "kimi": {
            "provider": "moonshot",
            "family": "kimi",
            "model": "kimi-k3",
            "kind": "go_http",
            "credential_path": None,
            "executable": None,
            "plan_units": {},
            "window": None,
            "max_concurrency": 1,
            "wall_seconds": 600,
            "categories": ["independent_review"],
        },
    }
    lanes_path = tmp_path / "lanes.json"
    lanes_path.write_text(json.dumps({"lanes": lanes}))
    now = time.time()
    for provider, model in (("go", "glm-5.3-flash"), ("kimi", "kimi-k3")):
        alias = provider + "-alias-" + uuid.uuid4().hex[:6]
        ledger.configure_account(
            alias, 1, {"five_hour": 10, "weekly": 20}, now + 600, [model], [alias]
        )
        ledger.record_lane(
            provider,
            dict(
                provider=provider,
                auth="ok",
                quota_observed_at=now - 5,
                quota_freshness_seconds=900,
                used_percent_max=1.0,
                admission_limit_percent=80,
                cooldown_until=None,
                qualification="qualified",
                blocked_until=None,
                blocker=None,
            ),
        )
        accounts_by_lane[provider] = alias

    # Tick 1: the work task passes and its review task is created.
    first = runner.tick(
        board, project, ledger, lanes, lanes_path, accounts_by_lane, tmp_path / "packets", now=now
    )
    assert first[0]["task"] == "copy-ok" and first[0]["result"] == "passed"
    assert (board / "review-copy-ok.json").is_file()
    # The board's own state files are the operator's tracked content; commit them so the
    # landing itself is what gets measured against a clean tree.
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "board: copy-ok passed, review queued")

    # Tick 2: the cross-family review approves; the runner accepts and lands the artifacts.
    review_task = json.loads((board / "review-copy-ok.json").read_text())
    assert "kimi" in review_task["lanes"]
    operator_head = git(project, "rev-parse", "HEAD").stdout.strip()
    second = runner.tick(
        board,
        project,
        ledger,
        lanes,
        lanes_path,
        accounts_by_lane,
        tmp_path / "packets",
        now=now + 1,
    )
    assert second[0]["result"].startswith("passed") and "accepted" in second[0]["result"], second

    # The inbox branch exists in the project repo with exactly one landing commit on top
    # of the operator's head.
    assert git(project, "branch", "--list", "grid/inbox").stdout.strip()
    landed_commits = git(
        project, "rev-list", "--count", f"{operator_head}..grid/inbox"
    ).stdout.strip()
    assert landed_commits == "1"
    landed = git(project, "show", "grid/inbox:grid/inbox/copy-ok/mod2.py").stdout
    assert landed == "VALUE = 1\n"
    record = json.loads(git(project, "show", "grid/inbox:grid/inbox/copy-ok.json").stdout)
    link = json.loads((board / "review/copy-ok/source.json").read_text())
    assert record["task"] == "copy-ok"
    assert record["attempt"] == link["attempt"]
    assert record["reviewer_family"] == "kimi"
    assert record["author_family"] == "glm"
    assert record["receipt_digest"] == link["receipt_digest"]
    assert record["review_task"] == "review-copy-ok"
    # The landing wrote nothing into the operator's checkout: after the operator commits
    # the board's own state changes (the daily routine), the tree is clean and no
    # grid/inbox directory ever appeared in it.
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "board: copy-ok accepted")
    assert git(project, "status", "--porcelain").stdout.strip() == ""
    assert not (project / "grid/inbox").exists()
    assert json.loads((board / "copy-ok.json").read_text())["state"] == "accepted"

    # Asking for the same acceptance again is refused by the ledger and lands nothing.
    again = runner.accept_reviewed(
        board,
        project,
        ledger,
        lanes,
        "kimi",
        json.loads((board / "review-copy-ok.json").read_text()),
        "passed",
    )
    assert "accept refused" in again, again
    assert git(project, "rev-list", "--count", f"{operator_head}..grid/inbox").stdout.strip() == "1"
