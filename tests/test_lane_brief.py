"""The brief module: packet sections, hard rules, prompt assembly and the commit gate.

The cases moved from tests/test_run_lane.py when the helpers left scripts/run_lane.py
for inference_grid.lanes.brief; the board's packet tasks (board/packet_task.py) now
compose the same prompts, so the contract is tested at its new home.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from inference_grid.lanes import brief

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
    a1 = brief.packet_text(BRIEF, "A1")
    assert a1.startswith("#### A1. First thing") and "Second thing" not in a1
    a2 = brief.packet_text(BRIEF, "A2")
    assert a2.strip().endswith("Only `docs/BOARD.md`.") and "Phase B" not in a2
    assert brief.packet_text(BRIEF, "B1").strip().endswith("Nothing.")
    with pytest.raises(KeyError):
        brief.packet_text(BRIEF, "Z9")


def test_paths_and_rules_and_prompt():
    assert brief.mentioned_paths(brief.packet_text(BRIEF, "A1")) == [
        "src/inference_grid/watch.py",
        "tests/test_watch.py",
    ]
    rules = brief.hard_rules(RULES)
    assert rules.startswith("## 1. Hard rules") and "Orientation" not in rules
    prompt = brief.compose_prompt(
        rules,
        "## Orientation\n",
        "#### A1. x\n",
        branch="glm/a1-x",
        base="origin/glm/work",
        python="/py",
        report_name="r.md",
    )
    assert "PYTHONPATH=src /py -m pytest" in prompt and brief.TRAILER in prompt
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
    script.write_text(brief.commit_gate_script("base"))

    def run():
        return subprocess.run(
            [sys.executable, str(script)],
            cwd=repo,
            capture_output=True,
            text=True,
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
        f"why\n\n{brief.TRAILER}",
    )
    (repo / "dirty.txt").write_text("x")
    out = run()
    assert out.returncode == 1 and "not clean" in out.stdout
    (repo / "dirty.txt").unlink()
    assert run().returncode == 0


def test_run_lane_script_still_exposes_the_moved_helpers():
    """The operator's driver imports the same functions, so its behaviour is unchanged."""
    script_dir = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(script_dir))
    try:
        import run_lane
    finally:
        sys.path.remove(str(script_dir))
    for name in (
        "PACKET_HEADING",
        "TRAILER",
        "packet_text",
        "hard_rules",
        "mentioned_paths",
        "compose_prompt",
        "commit_gate_script",
    ):
        assert getattr(run_lane, name) is getattr(brief, name), name


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
