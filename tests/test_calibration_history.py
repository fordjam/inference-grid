"""Eval cases from the board's own history (P1): a landed packet becomes a packet case."""

import json
import subprocess
from pathlib import Path

import pytest

from inference_grid.board.calibration.case import load_case
from inference_grid.board.calibration.history import (
    packet_case_from_landed,
    review_case_from_staging,
)


def git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def landed_repo(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "docs").mkdir()
    (repo / "data").mkdir()
    (repo / "grid/briefs").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "src/pkg/__init__.py").write_text("")
    (repo / "src/pkg/mod.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests/test_mod.py").write_text(
        "from pkg.mod import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    (repo / "docs/notes.md").write_text("# notes\n")
    (repo / "data/big.csv").write_text("1,2,3\n" * 100)
    (repo / "tests/test_guard.py").write_text("KEY = '-----BEGIN RSA PRIVATE KEY-----'\n")
    (repo / "grid/briefs/packet-x1.txt").write_text(
        "## 1. Hard rules\nnone\n\n---\n\n#### X1. Add subtract\nAdd `subtract(a, b)` to mod.py with a test.\n\n---\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "packet/packet-x1")
    (repo / "src/pkg/mod.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
    )
    (repo / "tests/test_mod.py").write_text(
        "from pkg.mod import add, subtract\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_subtract():\n    assert subtract(3, 1) == 2\n"
    )
    (repo / "data/big.csv").write_text("9,9,9\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "brief 1, X1: subtract")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "land", "packet/packet-x1")
    merge = git(repo, "rev-parse", "HEAD")
    board = repo / "grid/board"
    board.mkdir()
    (board / "packet-x1.json").write_text(
        json.dumps(
            {
                "id": "packet-x1",
                "category": "packet",
                "brief": "grid/briefs/packet-x1.txt",
                "inputs": ["grid/briefs/packet-x1.txt"],
                "tests": [],
                "artifacts": ["docs/reports/packet-x1.md"],
                "lanes": ["goat"],
                "author_family": None,
                "budget": {"wall_seconds": 600, "output_bytes": 100000, "thinking_tokens": None},
                "state": "landed",
                "blocked_reason": None,
                "failover": True,
                "spec": {
                    "brief": "grid/briefs/packet-x1.txt",
                    "packet_id": "X1",
                    "gates": [],
                    "base": "main",
                    "max_rounds": 3,
                },
                "landed": {"base_head": base, "merge_commit": merge, "how": "merge"},
            }
        )
    )
    return repo, board, base, merge


def test_a_landed_packet_becomes_a_packet_case(tmp_path):
    repo, board, base, merge = landed_repo(tmp_path)
    corpus = tmp_path / "corpus"
    out = packet_case_from_landed(repo, board, "packet-x1", corpus)
    case_dir = Path(out["path"])
    assert out["case"] == "proj-packet-x1" and out["kind"] == "packet"
    record = load_case(case_dir)
    # The brief is the packet's own section; the base is the tree at base_head under the
    # reviewable prefixes — data/ never, and the guard-refused test is omitted and named.
    assert "Add `subtract(a, b)`" in record["brief"]
    base_files = dict(record["base"])
    assert "src/pkg/mod.py" in base_files and b"subtract" not in base_files["src/pkg/mod.py"]
    assert not any(p.startswith("data/") for p in base_files)
    assert "tests/test_guard.py" not in base_files
    assert [o["path"] for o in out["omitted_from_base"]] == ["tests/test_guard.py"]
    # The reference is the landed diff (data/ excluded) and the tests the range changed.
    assert (
        "def subtract" in record["reference_patch"] and "big.csv" not in record["reference_patch"]
    )
    tests = dict(record["reference_tests"])
    assert list(tests) == ["tests/test_mod.py"] and b"test_subtract" in tests["tests/test_mod.py"]
    source = json.loads((case_dir / "source.json").read_text())
    assert source["base_head"] == base and source["merge_commit"] == merge
    # Second authoring refuses; nothing half-written is left behind.
    with pytest.raises(FileExistsError):
        packet_case_from_landed(repo, board, "packet-x1", corpus)
    assert sorted(p.name for p in corpus.iterdir()) == ["proj-packet-x1"]


def test_a_landed_packet_without_tests_is_refused(tmp_path):
    repo, board, base, merge = landed_repo(tmp_path)
    card = json.loads((board / "packet-x1.json").read_text())
    (board / "packet-x1.json").write_text(json.dumps(dict(card, state="passed")))
    with pytest.raises(ValueError, match="not a landed packet"):
        packet_case_from_landed(repo, board, "packet-x1", tmp_path / "c")
    (board / "packet-x1.json").write_text(json.dumps(card))
    with pytest.raises(ValueError, match="reference suite"):
        packet_case_from_landed(repo, board, "packet-x1", tmp_path / "c", prefixes=("src/",))
    assert not (tmp_path / "c").exists() or not list((tmp_path / "c").iterdir())


def test_a_staged_review_becomes_a_review_case(tmp_path):
    board = tmp_path / "grid/board"
    stage = board / "review/review-x-abc1234"
    (stage / "src").mkdir(parents=True)
    (board.parent / "briefs").mkdir()
    (stage / "src/mod.py").write_text("def f(x):\n    return x + 1\n")
    (stage / "diff.patch").write_text(
        "diff --git a/src/mod.py b/src/mod.py\n--- a/src/mod.py\n+++ b/src/mod.py\n@@ -1,2 +1,2 @@\n def f(x):\n-    return x\n+    return x + 1\n"
    )
    (stage / "source.json").write_text("{}")
    (board.parent / "briefs/review-x-abc1234.txt").write_text("review this\n")
    answer = {
        "defects": [
            {
                "id": "off-by-one",
                "file": "src/mod.py",
                "must_mention": ["x + 1"],
                "severity": "high",
                "note": "f adds one where it should not",
            }
        ],
        "clean": False,
    }
    out = review_case_from_staging(board, "review-x-abc1234", tmp_path / "corpus", answer)
    record = load_case(Path(out["path"]))
    assert record["kind"] == "review" and "src/mod.py" in dict(record["files"])
    assert record["answer"]["defects"][0]["file"] == "src/mod.py"
    assert json.loads((Path(out["path"]) / "source.json").read_text()) == {
        "review": "review-x-abc1234"
    }
    with pytest.raises(ValueError, match="answer key"):
        review_case_from_staging(board, "review-x-abc1234", tmp_path / "corpus2", {})
