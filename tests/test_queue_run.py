"""02-A9: deployments/local/queue_run.sh, exercised only through --dry-run -- this
row must never launch anything for real, so every test here stays on the preview
path and asserts on the printed commands.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deployments" / "local" / "queue_run.sh"


def run_dry(args, env_overrides=None):
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", *args], capture_output=True, text=True, env=env
    )


def test_dry_run_prints_the_gate_wait_and_claude_invocation(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("do the thing")
    result = run_dry(["rowA", str(prompt)])
    assert result.returncode == 0
    out = result.stdout
    assert "queue_gate.py" in out
    assert "--wait 7200" in out
    assert "caffeinate -i claude -p" in out
    assert "do the thing" in out
    assert "--model sonnet" in out
    assert "--dangerously-skip-permissions" in out
    assert "--max-turns 300" in out
    assert "--output-format text" in out
    assert "sleep 90" in out


def test_dry_run_handles_multiple_pairs_in_order(tmp_path):
    p1 = tmp_path / "p1.txt"
    p1.write_text("first prompt")
    p2 = tmp_path / "p2.txt"
    p2.write_text("second prompt")
    result = run_dry(["rowA", str(p1), "rowB", str(p2)])
    assert result.returncode == 0
    out = result.stdout
    assert out.index("first prompt") < out.index("second prompt")
    assert out.count("queue_gate.py") == 2
    assert out.count("sleep 90") == 2


def test_missing_or_odd_args_is_a_usage_error(tmp_path):
    assert run_dry([]).returncode == 2
    assert run_dry(["only-a-name"]).returncode == 2


def test_dry_run_never_writes_the_queue_log(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("x")
    fake_log_dir = tmp_path / "logs"
    result = run_dry(["rowA", str(prompt)], env_overrides={"QUEUE_LOG_DIR": str(fake_log_dir)})
    assert result.returncode == 0
    assert not fake_log_dir.exists()


def test_dry_run_respects_env_overrides_for_gate_wait_and_cooldown(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("x")
    result = run_dry(
        ["rowA", str(prompt)],
        env_overrides={"GATE_WAIT_SECONDS": "300", "COOLDOWN_SECONDS": "5"},
    )
    assert "--wait 300" in result.stdout
    assert "sleep 5" in result.stdout


def test_dry_run_uses_the_report_dir_and_dated_log_name(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("x")
    result = run_dry(["rowA", str(prompt)], env_overrides={"QUEUE_REPORT_DIR": str(tmp_path / "reports")})
    assert str(tmp_path / "reports") in result.stdout
    assert "-rowA.log" in result.stdout
