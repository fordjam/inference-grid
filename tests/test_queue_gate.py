"""02-A9: the local queue runner's concurrency gate (deployments/local/queue_gate.py),
against fixture sysctl/df/pgrep/ps output -- no test ever shells out to the real
machine.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deployments" / "local"


def load(name):
    spec = importlib.util.spec_from_file_location("deploy_" + name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("queue_gate")

SWAP_LOW = "vm.swapusage: total = 3072.00M  used = 604.25M  free = 2467.75M  (encrypted)"
SWAP_HIGH = "vm.swapusage: total = 8192.00M  used = 6348.80M  free = 1843.20M  (encrypted)"
DF_HEALTHY = (
    "Filesystem 1G-blocks Used Avail Capacity iused ifree %iused Mounted on\n"
    "/dev/disk3s5 228 147 56 73% 2.8M 589M 0% /System/Volumes/Data"
)
DF_LOW = (
    "Filesystem 1G-blocks Used Avail Capacity iused ifree %iused Mounted on\n"
    "/dev/disk3s5 228 210 10 92% 2.8M 589M 0% /System/Volumes/Data"
)
DEFAULT_CONFIG = {"gate_swap_gb": 4.0, "gate_disk_gb": 20.0, "gate_max_runs": 1}


def fake_run(responses=None, ps_commands=None):
    """responses: {substring-of-argv: (returncode, stdout)}; unmatched argv falls back
    to (1, "") -- pgrep's own "nothing matched" exit, a legitimate empty reading.

    ps_commands: {pid_str: full_command_line} -- answers `ps -o command= -p <pid>`,
    the per-pid cross-check `_pytest_running` makes to tell a real pytest process
    apart from a headless run whose inline prompt text merely mentions "pytest".
    """
    responses = responses or {}
    ps_commands = ps_commands or {}

    def run(argv):
        if argv[:2] == ["ps", "-o"] and "-p" in argv:
            pid = argv[argv.index("-p") + 1]
            return (0, ps_commands.get(pid, ""))
        key = " ".join(argv)
        for pattern, result in responses.items():
            if pattern in key:
                return result
        return (1, "")

    return run


# --- read_state ---


def test_read_state_parses_swap_disk_pytest_and_runs():
    run = fake_run(
        {
            "vm.swapusage": (0, SWAP_LOW),
            "df -g": (0, DF_HEALTHY),
            "pgrep -f pytest": (1, ""),
            "claude -p": (0, "111\n"),
            "codex exec": (1, ""),
        }
    )
    state = gate.read_state(run)
    assert state["swap_gb"] == pytest.approx(0.59, abs=0.01)
    assert state["disk_free_gb"] == 56
    assert state["pytest_running"] is False
    assert state["running_runs"] == 1


def test_read_state_reports_none_on_a_failed_sysctl_or_df():
    run = fake_run({"vm.swapusage": (1, ""), "df -g": (1, "")})
    state = gate.read_state(run)
    assert state["swap_gb"] is None
    assert state["disk_free_gb"] is None


def test_read_state_reports_none_on_unparsable_output():
    run = fake_run({"vm.swapusage": (0, "garbage"), "df -g": (0, "also garbage")})
    state = gate.read_state(run)
    assert state["swap_gb"] is None
    assert state["disk_free_gb"] is None


def test_read_state_counts_multiple_matching_runs():
    run = fake_run(
        {
            "vm.swapusage": (0, SWAP_LOW),
            "df -g": (0, DF_HEALTHY),
            "claude -p": (0, "111\n222\n"),
            "codex exec": (0, "333\n"),
        }
    )
    assert gate.read_state(run)["running_runs"] == 3


def test_read_state_counts_a_caffeinate_wrapped_claude_launch():
    """queue_run.sh's real launch shape is `caffeinate -i claude -p ...`, never a bare
    `claude -p` -- an anchored `^claude -p` pgrep pattern would never match it
    (confirmed live in review); the unanchored substring match must still count it."""
    run = fake_run(
        {
            "vm.swapusage": (0, SWAP_LOW),
            "df -g": (0, DF_HEALTHY),
            "claude -p": (0, "111\n"),  # pgrep -f matches the full command line
        }
    )
    assert gate.read_state(run)["running_runs"] == 1


# --- _pytest_running: cross-checked against a headless run's own inline prompt text ---


def test_pytest_running_true_for_a_real_pytest_process():
    run = fake_run(
        {"pgrep -f pytest": (0, "555\n")},
        ps_commands={"555": "/path/.venv/bin/python -m pytest -q -x"},
    )
    assert gate._pytest_running(run) is True


def test_pytest_running_false_when_the_only_match_is_a_headless_runs_own_prompt():
    """The exact bug confirmed live in review: a running `caffeinate -i claude -p
    "<prompt mentioning pytest>" ...` process matches `pgrep -f pytest` on its own
    argv, with no pytest process anywhere -- it must not block the gate."""
    run = fake_run(
        {"pgrep -f pytest": (0, "777\n")},
        ps_commands={
            "777": (
                'caffeinate -i claude -p run pytest -q -x and report back '
                '--model sonnet --dangerously-skip-permissions --max-turns 300 --output-format text'
            )
        },
    )
    assert gate._pytest_running(run) is False


def test_pytest_running_false_when_the_only_match_is_a_bare_codex_exec_prompt():
    run = fake_run(
        {"pgrep -f pytest": (0, "888\n")},
        ps_commands={"888": "codex exec run pytest across the repo"},
    )
    assert gate._pytest_running(run) is False


def test_pytest_running_true_when_a_real_pytest_process_runs_alongside_a_headless_run():
    run = fake_run(
        {"pgrep -f pytest": (0, "777\n555\n")},
        ps_commands={
            "777": "caffeinate -i claude -p run pytest please --model sonnet",
            "555": "/path/.venv/bin/python -m pytest -q -x",
        },
    )
    assert gate._pytest_running(run) is True


def test_pytest_running_false_with_no_pgrep_matches():
    run = fake_run({"pgrep -f pytest": (1, "")})
    assert gate._pytest_running(run) is False


def test_pytest_running_ignores_a_pid_whose_ps_lookup_fails():
    run = fake_run({"pgrep -f pytest": (0, "999\n")}, ps_commands={})
    # ps_commands has no entry for "999", but fake_run's ps handler still returns
    # rc=0 with an empty string by default; simulate a genuine ps failure instead.

    def run_with_ps_failure(argv):
        if argv[:2] == ["ps", "-o"]:
            return (1, "")
        return run(argv)

    assert gate._pytest_running(run_with_ps_failure) is False


# --- evaluate ---


def test_evaluate_names_every_failing_condition_in_order():
    readings = {"swap_gb": 6.2, "disk_free_gb": 10, "pytest_running": True, "running_runs": 1}
    reasons = gate.evaluate(readings, DEFAULT_CONFIG)
    assert reasons == ["swap 6.2GB > 4GB", "disk 10.0GB < 20GB", "pytest running", "runs 1 >= 1"]


def test_evaluate_allows_when_nothing_fails():
    readings = {"swap_gb": 1.0, "disk_free_gb": 56, "pytest_running": False, "running_runs": 0}
    assert gate.evaluate(readings, DEFAULT_CONFIG) == []


def test_evaluate_unknown_readings_never_block():
    readings = {"swap_gb": None, "disk_free_gb": None, "pytest_running": False, "running_runs": 0}
    assert gate.evaluate(readings, DEFAULT_CONFIG) == []


def test_evaluate_runs_at_the_max_is_already_blocked():
    readings = {"swap_gb": 0, "disk_free_gb": 100, "pytest_running": False, "running_runs": 1}
    config = {"gate_swap_gb": 4.0, "gate_disk_gb": 20.0, "gate_max_runs": 2}
    assert gate.evaluate(readings, config) == []
    config["gate_max_runs"] = 1
    assert gate.evaluate(readings, config) == ["runs 1 >= 1"]


# --- load_gate_config ---


def test_load_gate_config_defaults_without_a_file(tmp_path):
    assert gate.load_gate_config(tmp_path / "absent.json") == DEFAULT_CONFIG


def test_load_gate_config_reads_recognized_keys_only(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"gate_swap_gb": 8, "gate_max_runs": 2, "unrelated": "x"}))
    assert gate.load_gate_config(path) == {"gate_swap_gb": 8.0, "gate_disk_gb": 20.0, "gate_max_runs": 2}


def test_load_gate_config_ignores_bad_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"gate_swap_gb": -1, "gate_disk_gb": "nope"}))
    assert gate.load_gate_config(path) == DEFAULT_CONFIG


def test_load_gate_config_tolerates_unreadable_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("not json")
    assert gate.load_gate_config(path) == DEFAULT_CONFIG


# --- main ---


def test_main_exits_zero_and_silent_when_allowed(tmp_path, capsys):
    run = fake_run({"vm.swapusage": (0, SWAP_LOW), "df -g": (0, DF_HEALTHY)})
    code = gate.main(["--config", str(tmp_path / "absent.json")], run=run)
    assert code == 0
    assert capsys.readouterr().out == ""


def test_main_exits_one_and_names_every_condition_when_blocked(tmp_path, capsys):
    run = fake_run(
        {
            "vm.swapusage": (0, SWAP_HIGH),
            "df -g": (0, DF_LOW),
            "pgrep -f pytest": (0, "222\n"),
            "claude -p": (0, "111\n"),
        },
        ps_commands={"222": "/path/.venv/bin/python -m pytest -q -x"},
    )
    code = gate.main(["--config", str(tmp_path / "absent.json")], run=run)
    out = capsys.readouterr().out
    assert code == 1
    assert out == "blocked: swap 6.2GB > 4GB; disk 10.0GB < 20GB; pytest running; runs 1 >= 1\n"


def test_main_json_prints_readings_config_and_blocked(tmp_path, capsys):
    run = fake_run({"vm.swapusage": (0, SWAP_LOW), "df -g": (0, DF_HEALTHY)})
    code = gate.main(["--json", "--config", str(tmp_path / "absent.json")], run=run)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked"] == []
    assert payload["config"] == DEFAULT_CONFIG
    assert payload["readings"]["swap_gb"] == pytest.approx(0.59, abs=0.01)


def test_main_reads_gate_swap_gb_from_the_config_file(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"gate_swap_gb": 0.3}))
    run = fake_run({"vm.swapusage": (0, SWAP_LOW), "df -g": (0, DF_HEALTHY)})
    code = gate.main(["--config", str(config_path)], run=run)
    assert code == 1
    assert "swap 0.6GB > 0GB" in capsys.readouterr().out


# --- wait_until_allowed ---


def make_clock():
    state = {"now": 0.0, "sleeps": []}

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds

    return state, sleep


def test_wait_polls_until_allowed_then_stops():
    swap_calls = {"n": 0}

    def run(argv):
        cmd = " ".join(argv)
        if "vm.swapusage" in cmd:
            swap_calls["n"] += 1
            return (0, SWAP_HIGH if swap_calls["n"] <= 2 else SWAP_LOW)
        if "df -g" in cmd:
            return (0, DF_HEALTHY)
        return (1, "")

    clock, sleep = make_clock()
    readings, reasons = gate.wait_until_allowed(
        DEFAULT_CONFIG, wait_seconds=600, run=run, sleep=sleep, clock=lambda: clock["now"]
    )
    assert reasons == []
    assert swap_calls["n"] == 3
    assert clock["sleeps"] == [60, 60]


def test_wait_gives_up_at_the_deadline():
    run = fake_run({"vm.swapusage": (0, SWAP_HIGH), "df -g": (0, DF_HEALTHY)})
    clock, sleep = make_clock()
    readings, reasons = gate.wait_until_allowed(
        DEFAULT_CONFIG, wait_seconds=150, run=run, sleep=sleep, clock=lambda: clock["now"]
    )
    assert reasons != []
    assert clock["sleeps"] == [60, 60, 60]


def test_wait_never_sleeps_when_already_allowed():
    run = fake_run({"vm.swapusage": (0, SWAP_LOW), "df -g": (0, DF_HEALTHY)})
    clock, sleep = make_clock()
    readings, reasons = gate.wait_until_allowed(
        DEFAULT_CONFIG, wait_seconds=600, run=run, sleep=sleep, clock=lambda: clock["now"]
    )
    assert reasons == []
    assert clock["sleeps"] == []


def test_main_wait_flag_exits_one_after_the_deadline(capsys):
    run = fake_run({"vm.swapusage": (0, SWAP_HIGH), "df -g": (0, DF_HEALTHY)})
    clock, sleep = make_clock()
    code = gate.main(["--wait", "1"], run=run, sleep=sleep, clock=lambda: clock["now"])
    assert code == 1
    assert capsys.readouterr().out.startswith("blocked: ")


# --- default_run / _pgrep_count: never raise on a non-zero pgrep exit ---


def test_pgrep_count_treats_nonzero_exit_as_zero_matches():
    run = fake_run({"pgrep -f pytest": (1, "")})
    assert gate._pgrep_count(run, "pytest") == 0


def test_default_run_reports_none_on_a_missing_binary():
    rc, out = gate.default_run(["definitely-not-a-real-binary-xyz"])
    assert rc is None
    assert out == ""
