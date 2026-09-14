"""The packet lane: code runs the gates, the agent's own session is re-entered
with the failures, the loop is bounded, and the verdict never claims more than
the gates proved."""

import json
import os
import sys
from pathlib import Path

from inference_grid.lanes import packet
from inference_grid.lanes.packet import (
    CommandCodeAdapter,
    Gate,
    OpencodeAdapter,
    build_loop,
    external_skeletons,
    fix_prompt,
    run_gates,
)

PY = sys.executable


class ScriptedAgent(packet.Adapter):
    """A fake agent CLI: each run appends its prompt to a log and runs a script
    that makes the worktree pass its gate on the round the test chooses."""

    name = "scripted"

    def __init__(self, work, pass_on_round):
        self.work, self.pass_on_round = Path(work), pass_on_round
        self.calls = []

    def _argv(self, prompt, session):
        self.calls.append((session, prompt))
        round_no = len(self.calls)
        # The "agent" writes a marker the gate checks, and prints a session id
        # the way the real CLIs do.
        code = (
            "import sys, pathlib\n"
            f"pathlib.Path({str(self.work / 'gate-marker')!r}).write_text("
            f"'pass' if {round_no} >= {self.pass_on_round} else 'fail')\n"
            'print(\'{"type":"run_start","sessionId":"sess-42"}\')\n'
        )
        return [PY, "-c", code]

    def first(self, prompt):
        return self._argv(prompt, None)

    def resume(self, session_id, prompt):
        return self._argv(prompt, session_id)

    def session_id(self, native_jsonl):
        return packet._first_match(native_jsonl, r'"sessionId":"([^"]+)"')


def marker_gate(work, name="pytest"):
    code = f"import sys, pathlib; sys.exit(0 if pathlib.Path({str(work / 'gate-marker')!r}).read_text() == 'pass' else 1)"
    return Gate(name, [PY, "-c", "print('marker gate ran'); " + code])


def plain(argv):
    return argv  # no sandbox in tests


def test_gates_run_as_code_and_keep_their_output(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "gate-marker").write_text("fail")
    gates = [marker_gate(work), Gate("echo", [PY, "-c", "print('ok')"]),
             Gate("missing", ["/nonexistent/binary"])]
    results = run_gates(work, gates, dict(os.environ), tmp_path)
    assert [(r.name, r.ok, r.reason) for r in results] == [
        ("pytest", False, "exited"), ("echo", True, "exited"), ("missing", False, "could_not_start")]
    assert "marker gate ran" in results[0].tail
    assert (tmp_path / "gate-pytest.log").read_text().startswith("marker gate ran")


def test_the_loop_reenters_the_same_session_with_the_failure_and_stops_when_gates_pass(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    agent = ScriptedAgent(work, pass_on_round=2)
    verdict = build_loop(agent, plain, work, dict(os.environ), "the brief", [marker_gate(work)],
                         tmp_path / "attempt", wall_seconds=3600, max_rounds=3)
    assert verdict["reason"] == "gates_passed"
    assert [r["round"] for r in verdict["rounds"]] == [1, 2]
    assert [r["results"][0]["ok"] for r in verdict["rounds"]] == [False, True]
    assert verdict["session_id"] == "sess-42" and verdict["verified_in_lane"] is True
    # Round 2 was a resume of round 1's session, carrying the gate's tail.
    (first_session, first_prompt), (second_session, second_prompt) = agent.calls
    assert first_session is None and first_prompt == "the brief"
    assert second_session == "sess-42"
    assert "pytest — exited, exit 1" in second_prompt and "marker gate ran" in second_prompt
    assert "never amend" in second_prompt
    attempt = tmp_path / "attempt"
    assert (attempt / "prompt-2.txt").read_text() == second_prompt
    assert json.loads((attempt / "verdict.json").read_text())["reason"] == "gates_passed"


def test_rounds_are_bounded_and_the_verdict_says_the_gates_did_not_pass(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    agent = ScriptedAgent(work, pass_on_round=99)
    verdict = build_loop(agent, plain, work, dict(os.environ), "brief", [marker_gate(work)],
                         tmp_path / "attempt", wall_seconds=3600, max_rounds=2)
    assert verdict["reason"] == "rounds_exhausted"
    assert len(verdict["rounds"]) == 2 and len(agent.calls) == 2
    assert verdict["gates_passed"] is False and verdict["verified_in_lane"] is False


def test_an_operator_gate_means_the_lane_never_claims_verification(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    agent = ScriptedAgent(work, pass_on_round=1)
    verdict = build_loop(agent, plain, work, dict(os.environ), "brief", [marker_gate(work)],
                         tmp_path / "attempt", wall_seconds=600, operator_gates=["playwright"])
    assert verdict["gates_passed"] is True
    assert verdict["verified_in_lane"] is False
    assert verdict["operator_gates"] == ["playwright"]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_no_new_round_starts_inside_the_wall_margin(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    agent = ScriptedAgent(work, pass_on_round=99)
    clock = FakeClock()
    original = agent._argv

    def slow(prompt, session):
        clock.now += 500  # each agent run eats 500 "seconds"
        return original(prompt, session)

    agent._argv = slow
    verdict = build_loop(agent, plain, work, dict(os.environ), "brief", [marker_gate(work)],
                         tmp_path / "attempt", wall_seconds=900, max_rounds=5,
                         min_seconds_for_round=600, clock=clock)
    assert verdict["reason"] == "wall_deadline_before_round"
    assert len(verdict["rounds"]) == 1


def test_a_first_round_without_a_session_id_cannot_loop(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    agent = ScriptedAgent(work, pass_on_round=99)
    agent.session_id = lambda path: None
    verdict = build_loop(agent, plain, work, dict(os.environ), "brief", [marker_gate(work)],
                         tmp_path / "attempt", wall_seconds=600)
    assert verdict["reason"] == "no_session_to_resume" and len(verdict["rounds"]) == 1


def test_fix_prompt_names_every_failed_gate_and_only_those():
    from inference_grid.lanes.packet import GateResult

    text = fix_prompt([GateResult("pytest", False, 1, "3 failed", 1.0),
                       GateResult("build", True, 0, "built", 1.0),
                       GateResult("tsc", False, None, "", 1.0, "timeout")], 2, 3)
    assert "round 2 of 3" in text and "2 failed" in text
    assert "### pytest — exited, exit 1" in text and "### tsc — timeout" in text
    assert "### build" not in text


def test_adapters_start_and_resume_with_the_documented_flags(tmp_path):
    cc = CommandCodeAdapter("z-ai/glm-5.3-flash", mod_path=tmp_path / "e.mjs", session_name="lane")
    first = cc.first("do it")
    assert first[:3] == ["/opt/homebrew/bin/cmd", "--print", "do it"]
    assert "--no-session" not in first and "--name" in first and "--mod" in first
    resumed = cc.resume("abc", "fix it")
    assert resumed[2:5] == ["fix it", "--session", "abc"] and "--name" not in resumed
    oc = OpencodeAdapter("opencode-go/deepseek-v4.1-flash", tmp_path, title="t")
    assert oc.first("go")[:2] == ["/opt/homebrew/bin/opencode", "run"] and oc.first("go")[-1] == "go"
    assert oc.resume("ses_1", "go")[2:4] == ["--session", "ses_1"]
    log = tmp_path / "n.jsonl"
    log.write_text('{"type":"event","event":{"type":"run_start","sessionId":"1371"}}\n')
    assert cc.session_id(log) == "1371"
    log.write_text('{"type":"step_start","sessionID":"ses_f6"}\n')
    assert oc.session_id(log) == "ses_f6"


def test_external_skeletons_carry_the_verdict_s_verification_flag(tmp_path):
    attempt = tmp_path / "glm-20260914T000000"
    attempt.mkdir()
    verdict = {"verified_in_lane": False, "elapsed_s": 12, "operator_gates": ["playwright"],
               "rounds": [{"agent_returncode": 0}]}
    paths = external_skeletons(attempt, ["glm/t1 29bc6bc", "glm/work 069c477", "junk"],
                               task_prefix="ext-x", project="monarch", account="zai-account",
                               model="glm-5.3-flash", family="glm", argv=["cmd"], work=tmp_path,
                               verdict=verdict)
    assert [p.name for p in paths] == ["t1.json"]
    doc = json.loads(paths[0].read_text())
    assert doc["task"] == "ext-x-t1" and doc["spec"]["commit"] == "29bc6bc"
    assert doc["receipt"]["verified_in_lane"] is False
    assert doc["receipt"]["operator_gates"] == ["playwright"]
    assert doc["category"] == "FILL" and doc["accepted"] is None
