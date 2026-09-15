"""The versioned lane driver: brief parsing, prompt assembly and the commit gate."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_lane  # noqa: E402

BRIEF = """# Brief

## Phase A

#### A1. First thing
Touch `src/inference_grid/watch.py` and `tests/test_watch.py`.
- Size: small.

#### A2. Second thing
Only `docs/BOARD.md`.

## Phase B

#### B1. Third thing
Nothing.
"""

RULES = "# Umbrella\n\n## 1. Hard rules\n\n- never read secrets\n\n---\n\n## 2. Orientation\n"


def test_packet_text_is_bounded_by_the_next_heading_or_phase():
    a1 = run_lane.packet_text(BRIEF, "A1")
    assert a1.startswith("#### A1. First thing") and "Second thing" not in a1
    a2 = run_lane.packet_text(BRIEF, "A2")
    assert a2.strip().endswith("Only `docs/BOARD.md`.") and "Phase B" not in a2
    assert run_lane.packet_text(BRIEF, "B1").strip().endswith("Nothing.")
    with pytest.raises(KeyError):
        run_lane.packet_text(BRIEF, "Z9")


def test_paths_and_rules_and_prompt():
    assert run_lane.mentioned_paths(run_lane.packet_text(BRIEF, "A1")) == [
        "src/inference_grid/watch.py",
        "tests/test_watch.py",
    ]
    rules = run_lane.hard_rules(RULES)
    assert rules.startswith("## 1. Hard rules") and "Orientation" not in rules
    prompt = run_lane.compose_prompt(
        rules,
        "## Orientation\n",
        "#### A1. x\n",
        branch="glm/a1-x",
        base="origin/glm/work",
        python="/py",
        report_name="r.md",
    )
    assert "PYTHONPATH=src /py -m pytest" in prompt and run_lane.TRAILER in prompt
    assert prompt.index("Hard rules") < prompt.index("Orientation") < prompt.index("#### A1")


def test_commit_gate_requires_one_trailered_commit_and_a_clean_tree(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    def g(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    g("init", "-q", "-b", "main")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    g("branch", "base")
    script = tmp_path / "gate.py"
    script.write_text(run_lane.commit_gate_script("base"))

    def run():
        return subprocess.run(
            [sys.executable, str(script)], cwd=repo, capture_output=True, text=True
        )

    out = run()
    assert out.returncode == 1 and "exactly one commit" in out.stdout
    g(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "no trailer",
    )
    out = run()
    assert out.returncode == 1 and "trailer missing" in out.stdout
    g(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--amend",
        "--allow-empty",
        "-m",
        f"why\n\n{run_lane.TRAILER}",
    )
    (repo / "dirty.txt").write_text("x")
    out = run()
    assert out.returncode == 1 and "not clean" in out.stdout
    (repo / "dirty.txt").unlink()
    assert run().returncode == 0


def test_cline_adapter_and_transcript_compaction(tmp_path):
    from inference_grid.lanes.packet import ClineAdapter, compact_transcripts

    a = ClineAdapter(
        "z-ai/glm-5.3-flash", work=tmp_path, data_dir=tmp_path / "st", binary="/x/cline"
    )
    first = a.first("do it")
    assert first[:2] == ["/x/cline", "do it"] and "--auto-approve" in first and "--json" in first
    assert a.resume("s1", "fix")[:2] == ["/x/cline", "fix"]  # fresh session: --id refuses a prompt
    native = tmp_path / "native-1.jsonl"
    native.write_text(
        '{"sessionId":"abc"}\n{"type":"thinking_delta","d":"x"}\n{"type":"tool_completed"}\n'
    )
    assert a.session_id(native) == "abc"
    assert compact_transcripts(tmp_path) == {"native-1.jsonl": 1}
    assert native.read_text().count("\n") == 2 and "thinking_delta" not in native.read_text()
