"""The packet lane: an agent builds, CODE runs the gates, the same session is
re-entered with the failures, bounded times.

This is the harness the hand-run repo lanes lacked. Until now a lane was one
shot — the agent ran its own tests as a step inside its own prompt and the
operator discovered afterwards what it could not verify. Here the gates
(pytest, tsc, the build) are code nodes the harness runs after the agent
exits; a failure is fed back into the SAME session as a fix prompt; the loop
is bounded by rounds and by wall clock. Gates the sandbox cannot run at all
(Playwright) are declared as operator gates: the harness does not pretend to
run them, the verdict names them, and the external-work skeletons it writes
default to `verified_in_lane: false` so the scorecard is told the truth.

Nothing here knows about a particular repository. The adapters know how to
start and resume one agent CLI; the sandbox wrapper is passed in, so tests
run the loop with plain subprocesses and fake agents.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

TAIL_CHARS = 6000


@dataclass(frozen=True)
class Gate:
    """One code node: a command the harness runs in the worktree after the agent."""

    name: str
    argv: Sequence[str]
    cwd: str = "."
    timeout: int = 1800
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class GateResult:
    name: str
    ok: bool
    returncode: Optional[int]
    tail: str
    elapsed_s: float
    reason: str = "exited"

    def as_dict(self):
        return {
            "name": self.name,
            "ok": self.ok,
            "returncode": self.returncode,
            "elapsed_s": round(self.elapsed_s, 1),
            "reason": self.reason,
            "tail": self.tail[-1500:],
        }


class Adapter:
    """How to start one agent CLI on a prompt and re-enter the session it made."""

    name = "adapter"

    def first(self, prompt: str) -> List[str]:
        raise NotImplementedError

    def resume(self, session_id: str, prompt: str) -> List[str]:
        raise NotImplementedError

    def session_id(self, native_jsonl: Path) -> Optional[str]:
        raise NotImplementedError


class CommandCodeAdapter(Adapter):
    """`cmd --print`, persisting the session so a later round can `--session` it."""

    name = "command_code"

    def __init__(
        self,
        model: str,
        mod_path: Optional[Path] = None,
        binary: str = "/opt/homebrew/bin/cmd",
        max_turns: int = 1200,
        session_name: Optional[str] = None,
    ):
        self.model, self.mod_path, self.binary = model, mod_path, binary
        self.max_turns, self.session_name = max_turns, session_name

    def _common(self) -> List[str]:
        argv = [self.binary, "--print"]
        return argv

    def _tail(self) -> List[str]:
        argv = [
            "--model",
            self.model,
            "--max-turns",
            str(self.max_turns),
            "--output-format",
            "json",
            "--skip-onboarding",
            "--no-auto-update",
            "--no-skills",
            "--yolo",
            "--tools-all",
        ]
        if self.mod_path is not None:
            argv += ["--mod", str(self.mod_path)]
        return argv

    def first(self, prompt: str) -> List[str]:
        argv = self._common() + [prompt] + self._tail()
        if self.session_name:
            argv += ["--name", self.session_name]
        return argv

    def resume(self, session_id: str, prompt: str) -> List[str]:
        return self._common() + [prompt, "--session", session_id] + self._tail()

    def session_id(self, native_jsonl: Path) -> Optional[str]:
        return _first_match(native_jsonl, r'"sessionId"\s*:\s*"([^"]+)"')


class ZcodeAdapter(Adapter):
    """The ZCode CLI headless, re-entering the session it persisted.

    Flags relied on — the invocation and its evidence are lanes/zcode.py's: `--prompt`
    runs headless, `--mode yolo` grants the tools, `--json` makes the final stdout
    document carry `sessionId`, `--no-color` keeps stdout parsable, `--cwd` pins the
    worktree, and a later round resumes with `--resume <sessionId>` plus a fresh
    `--prompt`. No model is passed: the CLI serves the model of its own session login.
    """

    name = "zcode"

    def __init__(
        self,
        work: Optional[Path] = None,
        binary: str = "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
        wrapper: Optional[str] = None,
        mode: str = "yolo",
    ):
        self.work, self.binary, self.wrapper, self.mode = work, binary, wrapper, mode

    def _head(self) -> List[str]:
        return [self.wrapper, self.binary] if self.wrapper else [self.binary]

    def _tail(self, prompt: str) -> List[str]:
        argv = ["--prompt", prompt, "--mode", self.mode, "--json", "--no-color"]
        if self.work is not None:
            argv += ["--cwd", str(self.work)]
        return argv

    def first(self, prompt: str) -> List[str]:
        return self._head() + self._tail(prompt)

    def resume(self, session_id: str, prompt: str) -> List[str]:
        return self._head() + ["--resume", session_id] + self._tail(prompt)

    def session_id(self, native_jsonl: Path) -> Optional[str]:
        try:
            raw = native_jsonl.read_bytes()
        except OSError:
            return None
        start = raw.find(b"{")
        if start < 0:
            return None
        try:
            document = json.loads(raw[start:])
        except ValueError:
            return None
        if isinstance(document, dict) and isinstance(document.get("sessionId"), str):
            return document["sessionId"]
        return None


class OpencodeAdapter(Adapter):
    """`opencode run --format json`; resumes with `--session`."""

    name = "opencode"

    def __init__(
        self,
        model: str,
        work: Path,
        binary: str = "/opt/homebrew/bin/opencode",
        title: Optional[str] = None,
        debug: bool = False,
    ):
        self.model, self.work, self.binary, self.title, self.debug = (
            model,
            work,
            binary,
            title,
            debug,
        )

    def _tail(self, prompt: str) -> List[str]:
        argv = ["--model", self.model, "--format", "json", "--auto"]
        if self.debug:
            argv += ["--print-logs", "--log-level", "DEBUG"]
        if self.title:
            argv += ["--title", self.title]
        return argv + ["--dir", str(self.work), prompt]

    def first(self, prompt: str) -> List[str]:
        return [self.binary, "run"] + self._tail(prompt)

    def resume(self, session_id: str, prompt: str) -> List[str]:
        return [self.binary, "run", "--session", session_id] + self._tail(prompt)

    def session_id(self, native_jsonl: Path) -> Optional[str]:
        return _first_match(native_jsonl, r'"sessionID"\s*:\s*"([^"]+)"')


class ClineAdapter(Adapter):
    """`cline --json --auto-approve true`; resumes with `--id <session>`.

    Isolated local state under `data_dir` so nothing the lane does reaches the operator's
    own Cline sessions; the provider's key is read by the CLI from its own login."""

    name = "cline"

    def __init__(
        self,
        model: str,
        work: Path,
        data_dir: Path,
        binary: str = "/opt/homebrew/bin/cline",
        provider: str = "cline",
        thinking: str = "high",
        retries: int = 8,
    ):
        self.model, self.work, self.data_dir, self.binary = model, work, data_dir, binary
        self.provider, self.thinking, self.retries = provider, thinking, retries

    def _tail(self) -> List[str]:
        return [
            "--provider",
            self.provider,
            "--model",
            self.model,
            "--json",
            "--auto-approve",
            "true",
            "--thinking",
            self.thinking,
            "--retries",
            str(self.retries),
            "--cwd",
            str(self.work),
            "--data-dir",
            str(self.data_dir),
        ]

    def first(self, prompt: str) -> List[str]:
        return [self.binary, prompt] + self._tail()

    def resume(self, session_id: str, prompt: str) -> List[str]:
        # cline 3.0.61 refuses a prompt with --id in JSON mode ("interactive mode is
        # unsupported"), so a later round is a fresh session on the fix prompt: the branch
        # and the gate output carry the context; the first session id stays in the verdict.
        return self.first(prompt)

    def session_id(self, native_jsonl: Path) -> Optional[str]:
        return _first_match(
            native_jsonl, r'"(?:sessionId|session_id|taskId|task_id)"\s*:\s*"([^"]+)"'
        )


DELTA_EVENTS = ("thinking_delta", "text_delta", "content_delta")


def compact_transcripts(attempt_dir: Path) -> Dict[str, int]:
    """Drop per-token delta events from native-*.jsonl in place; keep every other line.

    A streamed JSON transcript is one line per token, so a long session runs to gigabytes
    of `thinking_delta`. The session id, tool calls and terminal events survive; a count of
    what was dropped is written beside each file."""
    dropped = {}
    for native in sorted(Path(attempt_dir).glob("native-*.jsonl")):
        keep, gone = [], 0
        with native.open("rb") as fh:
            for raw in fh:
                if any(f'"{kind}"'.encode() in raw for kind in DELTA_EVENTS):
                    gone += 1
                    continue
                keep.append(raw)
        if gone:
            native.write_bytes(b"".join(keep))
            (native.with_suffix(".compacted.json")).write_text(
                json.dumps({"dropped_delta_events": gone})
            )
        dropped[native.name] = gone
    return dropped


def _first_match(path: Path, pattern: str) -> Optional[str]:
    try:
        with path.open("rb") as handle:
            for raw in handle:
                found = re.search(pattern, raw.decode("utf-8", "replace"))
                if found:
                    return found.group(1)
    except OSError:
        return None
    return None


def run_gate(work: Path, gate: Gate, env: Dict[str, str], log_dir: Path) -> GateResult:
    """Run one gate as code and keep its whole output on disk; return the tail."""
    log = log_dir / f"gate-{_slug(gate.name)}.log"
    started = time.monotonic()
    merged = dict(env, **gate.env)
    try:
        with log.open("wb") as out:
            proc = subprocess.run(
                list(gate.argv),
                cwd=work / gate.cwd,
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=gate.timeout,
            )
        code, reason = proc.returncode, "exited"
    except subprocess.TimeoutExpired:
        code, reason = None, "timeout"
    except OSError as exc:
        log.write_text(str(exc))
        code, reason = None, "could_not_start"
    tail = _tail_of(log)
    return GateResult(gate.name, code == 0, code, tail, time.monotonic() - started, reason)


def run_gates(
    work: Path, gates: Iterable[Gate], env: Dict[str, str], log_dir: Path
) -> List[GateResult]:
    return [run_gate(work, gate, env, log_dir) for gate in gates]


def fix_prompt(results: Sequence[GateResult], round_no: int, max_rounds: int) -> str:
    """Deterministic text: which gates failed, their tails, and the rules of the round.

    The rules repeat what the brief already said because the model is being
    re-entered mid-session and a short, exact instruction beats a reference."""
    failed = [r for r in results if not r.ok]
    lines = [
        f"The harness ran the gates after your last turn (round {round_no} of {max_rounds}). "
        f"{len(failed)} failed. Fix them on the branch you are on, with a NEW commit "
        "(never amend, rebase or squash), same trailer as before. Do not weaken, skip or "
        "delete a test to make a gate pass; if a gate fails for a reason outside your packet, "
        "say so in the report and leave it. Run the failing gate yourself before you finish. "
        "Then update the report file and its copy in the working directory.",
        "",
    ]
    for r in failed:
        head = f"### {r.name} — {r.reason}" + (
            f", exit {r.returncode}" if r.returncode is not None else ""
        )
        lines += [head, "```", r.tail.strip()[-TAIL_CHARS:], "```", ""]
    return "\n".join(lines)


def build_loop(
    adapter: Adapter,
    sandbox_command: Callable[[List[str]], List[str]],
    work: Path,
    env: Dict[str, str],
    prompt: str,
    gates: Sequence[Gate],
    attempt_dir: Path,
    *,
    wall_seconds: int,
    max_rounds: int = 3,
    operator_gates: Sequence[str] = (),
    min_seconds_for_round: int = 600,
    clock=time,
) -> Dict:
    """Agent → gates → (fix prompt into the same session) …, bounded.

    Returns the verdict dict. `rounds` holds one entry per agent run with its
    gate results; `verified_in_lane` is True only when every declared gate ran
    and passed in the final round and no operator gate was declared."""
    attempt_dir.mkdir(parents=True, exist_ok=True)
    deadline = clock.monotonic() + wall_seconds
    rounds: List[Dict] = []
    session: Optional[str] = None
    reason = "gates_passed"
    started_all = clock.monotonic()
    for round_no in range(1, max_rounds + 1):
        remaining = deadline - clock.monotonic()
        if round_no > 1 and remaining < min_seconds_for_round:
            reason = "wall_deadline_before_round"
            break
        text = prompt if round_no == 1 else fix_prompt(rounds[-1]["results"], round_no, max_rounds)
        argv = adapter.first(text) if round_no == 1 else adapter.resume(session, text)
        native = attempt_dir / f"native-{round_no}.jsonl"
        stderr = attempt_dir / f"native-{round_no}.stderr"
        (attempt_dir / f"prompt-{round_no}.txt").write_text(text)
        run_started = clock.monotonic()
        with native.open("xb") as out, stderr.open("xb") as err:
            proc = subprocess.Popen(
                sandbox_command(list(argv)),
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                code = proc.wait(timeout=max(1, int(remaining)))
                agent_reason = "process_exited"
            except subprocess.TimeoutExpired:
                _kill(proc)
                code = proc.wait()
                agent_reason = "wall_deadline"
        if session is None:
            session = adapter.session_id(native)
        gate_dir = attempt_dir / f"gates-{round_no}"
        gate_dir.mkdir(exist_ok=True)
        results = run_gates(work, gates, env, gate_dir)
        rounds.append(
            {
                "round": round_no,
                "agent_returncode": code,
                "agent_reason": agent_reason,
                "agent_elapsed_s": round(clock.monotonic() - run_started),
                "results": results,
            }
        )
        if agent_reason == "wall_deadline":
            reason = "wall_deadline"
            break
        if all(r.ok for r in results):
            reason = "gates_passed"
            break
        if session is None:
            reason = "no_session_to_resume"
            break
        reason = "rounds_exhausted"
    final = rounds[-1]["results"] if rounds else []
    verdict = {
        "adapter": adapter.name,
        "reason": reason,
        "rounds": [dict(r, results=[g.as_dict() for g in r["results"]]) for r in rounds],
        "session_id": session,
        "elapsed_s": round(clock.monotonic() - started_all),
        "gates": [g.name for g in gates],
        "operator_gates": list(operator_gates),
        "gates_passed": bool(final) and all(r.ok for r in final),
        "verified_in_lane": bool(final) and all(r.ok for r in final) and not operator_gates,
    }
    (attempt_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))
    return verdict


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, 15)
    except ProcessLookupError:
        return
    time.sleep(5)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass


def _tail_of(path: Path, chars: int = TAIL_CHARS) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - chars))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "gate"


def external_skeletons(
    attempt_dir: Path,
    branches: Iterable[str],
    *,
    task_prefix: str,
    project: str,
    account: str,
    model: str,
    family: str,
    argv: Sequence[str],
    work: Path,
    verdict: Dict,
    skip_suffixes: Sequence[str] = ("work",),
) -> List[Path]:
    """One `inference-grid external` skeleton per packet branch the lane left.

    `verified_in_lane` is copied from the verdict — false whenever an operator
    gate was declared or the final gates did not pass — so the operator has
    to flip it deliberately, never by default."""
    out = attempt_dir / "external"
    out.mkdir(exist_ok=True)
    written = []
    for line in branches:
        parts = line.split()
        if len(parts) != 2:
            continue
        name, sha = parts
        packet = name.split("/", 1)[1] if "/" in name else name
        if packet in skip_suffixes:
            continue
        path = out / f"{packet}.json"
        path.write_text(
            json.dumps(
                {
                    "task": f"{task_prefix}-{packet}",
                    "project": project,
                    "spec": {
                        "authorized": True,
                        "account": account,
                        "model": model,
                        "family": family,
                        "argv": list(argv)[:6],
                        "workspace": str(work),
                        "lane_stamp": attempt_dir.name,
                        "commit": sha,
                    },
                    "receipt": {
                        "verified_in_lane": bool(verdict.get("verified_in_lane")),
                        "elapsed_s": verdict.get("elapsed_s", 0),
                        "returncode": (verdict.get("rounds") or [{}])[-1].get(
                            "agent_returncode", 0
                        ),
                        "operator_gates": list(verdict.get("operator_gates") or []),
                        "rounds": len(verdict.get("rounds") or []),
                    },
                    "category": "FILL",
                    "accepted": None,
                    "repairs": 0,
                    "note": "",
                },
                indent=1,
            )
        )
        written.append(path)
    return written
