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

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

TAIL_CHARS = 6000
DEFAULT_MIN_FREE_BYTES = 5 * 1024**3
# An agent round that neither moves the tree nor writes to its transcript for this long is
# idle; the round is cut and the loop re-enters (J4 on cline-deepseek and the first Q5 each
# burned an hour at the wall before anything named the silence).
DEFAULT_IDLE_SECONDS = 900
IDLE_TRANSCRIPT_BYTES = 2048
IDLE_POLL_SECONDS = 15.0


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


DELTA_EVENTS = ("thinking_delta", "text_delta", "content_delta")


def _is_delta_line(raw: bytes) -> bool:
    """True when one streamed transcript line carries a per-token delta event."""
    return any(f'"{kind}"'.encode() in raw for kind in DELTA_EVENTS)


def compact_transcripts(attempt_dir: Path) -> Dict[str, int]:
    """Drop per-token delta events from native-*.jsonl in place; keep every other line.

    A streamed JSON transcript is one line per token, so a long session runs to gigabytes
    of `thinking_delta`. The session id, tool calls and terminal events survive; a count of
    what was dropped is written beside each file. Kept for attempts written before the
    loop streamed its transcript through `stream_transcript`."""
    dropped = {}
    for native in sorted(Path(attempt_dir).glob("native-*.jsonl")):
        keep, gone = [], 0
        with native.open("rb") as fh:
            for raw in fh:
                if _is_delta_line(raw):
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


def stream_transcript(stream, native: Path) -> int:
    """Copy the agent's stdout into `native`, dropping DELTA_EVENTS as they arrive.

    The filter runs while the agent is still writing (the loop reads the pipe on a
    thread), so `native-<n>.jsonl` never holds a per-token delta line. The running count
    is kept beside the transcript in `native-<n>.compacted.json`, the same file and shape
    `compact_transcripts` writes for an attempt compacted after the fact. Returns the
    number of lines dropped."""
    gone = 0
    with native.open("xb") as out:
        for raw in stream:
            if _is_delta_line(raw):
                gone += 1
                continue
            out.write(raw)
    (native.with_suffix(".compacted.json")).write_text(json.dumps({"dropped_delta_events": gone}))
    return gone


def free_disk_bytes(path: Path) -> int:
    """Free bytes on the volume holding `path` (the packets root's own filesystem)."""
    return shutil.disk_usage(path).free


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


def _git(work: Path, argv: Sequence[str]) -> Optional[bytes]:
    """stdout of one git query in `work`, or None when it is not a worktree we can read."""
    try:
        proc = subprocess.run(
            ["git", *argv], cwd=work, capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def worktree_fingerprint(work: Path) -> Optional[str]:
    """A digest of HEAD, the tracked diff and every untracked file's bytes.

    Early-stop detection asks whether a round moved the worktree at all, so the
    fingerprint sees commits (`rev-parse`), uncommitted edits (`diff HEAD`, `status`)
    and untracked files with their contents (`ls-files --others`). A directory that is
    not a git worktree, or a git command that fails, has no answer: None. The caller
    must not read None as "unchanged" — an unreadable worktree is not evidence that
    the agent did nothing."""
    digest = hashlib.sha256()
    for argv in (["rev-parse", "HEAD"], ["diff", "HEAD"], ["status", "--porcelain"]):
        chunk = _git(work, argv)
        if chunk is None:
            return None
        digest.update(chunk)
        digest.update(b"\0")
    listing = _git(work, ["ls-files", "--others", "--exclude-standard", "-z"])
    if listing is None:
        return None
    for name in sorted(filter(None, listing.split(b"\0"))):
        digest.update(name)
        digest.update(b"\0")
        try:
            digest.update((work / name.decode("utf-8", "surrogateescape")).read_bytes())
        except OSError:
            return None
        digest.update(b"\0")
    return digest.hexdigest()


def tree_marker(work: Path) -> Optional[bytes]:
    """HEAD plus `git status --porcelain`: the cheap pair the idle watchdog polls.

    Deliberately weaker than `worktree_fingerprint` — it does not hash untracked file
    *contents* — because it is read every few seconds while the agent runs. A directory
    that is not a git worktree, or a git command that fails, has no answer: None, which
    is never read as "unchanged"."""
    head = _git(work, ["rev-parse", "HEAD"])
    status = _git(work, ["status", "--porcelain"])
    if head is None or status is None:
        return None
    return head + b"\0" + status


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class IdleWatch:
    """When a running round has gone idle: the tree stood still and the transcript is quiet.

    `poll` is called between short waits on the agent. The tree marker and the transcript
    size are read at construction and re-read on every poll; either a moved tree or
    `IDLE_TRANSCRIPT_BYTES` of new transcript is activity and resets the window. `poll`
    returns True once the tree marker has stood still and the transcript has grown by
    fewer than `IDLE_TRANSCRIPT_BYTES` for a whole `idle_seconds`. An unreadable marker
    never reads as "unchanged", so a non-worktree round always runs to the wall."""

    def __init__(self, work: Path, native: Path, idle_seconds: float, clock=time):
        self.work, self.native, self.idle_seconds, self.clock = work, native, idle_seconds, clock
        self.marker = tree_marker(work)
        self.size = _size_of(native)
        self.since = clock.monotonic()

    def poll(self) -> bool:
        now = self.clock.monotonic()
        marker = tree_marker(self.work)
        size = _size_of(self.native)
        if (marker is not None and marker != self.marker) or (
            size - self.size >= IDLE_TRANSCRIPT_BYTES
        ):
            self.marker, self.size, self.since = marker, size, now
        return marker is not None and (now - self.since) >= self.idle_seconds


def _poll_seconds(idle_seconds: float) -> float:
    """How often the watchdog looks: often enough to cut near the window, never a busy loop."""
    return min(IDLE_POLL_SECONDS, max(0.05, idle_seconds / 4))


def idle_minutes(idle_seconds: float) -> int:
    """The window as whole minutes for the re-entry prompt, never below one."""
    return max(1, int(round(idle_seconds / 60.0)))


def _kill_and_reap(proc: subprocess.Popen, grace: float) -> int:
    _kill(proc, grace)
    return proc.wait()


def _wait_for_round(
    proc: subprocess.Popen,
    deadline: float,
    clock,
    watch: Optional[IdleWatch],
    poll: Optional[float],
    grace: float,
) -> tuple:
    """Block until the agent exits, the wall passes, or the round has gone idle.

    Returns `(agent_reason, returncode)`; a round cut at the wall or on idleness is killed
    as a process group and reaped, so the caller sees the signal it died on."""
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            return "wall_deadline", _kill_and_reap(proc, grace)
        step = remaining if poll is None else min(remaining, poll)
        try:
            return "process_exited", proc.wait(timeout=max(0.01, step))
        except subprocess.TimeoutExpired:
            if watch is not None and watch.poll():
                return "agent_idle", _kill_and_reap(proc, grace)


def terminal_corroboration(native: Path) -> Optional[Dict]:
    """What the CLI said about its own ending, for the record — never the decision.

    `cmd --print --output-format json` ends with a `result` document carrying `subtype`
    and `stopReason`. The last such line wins. An unknown or unreadable stream returns
    None, so the round still carries whatever the exit code and the worktree proved."""
    found = None
    try:
        with native.open("rb") as handle:
            for raw in handle:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("type") == "result":
                    found = {
                        "kind": "command_code",
                        "subtype": row.get("subtype"),
                        "stop_reason": row.get("stopReason"),
                    }
    except OSError:
        return None
    return found


# What a provider says when it stops the agent for quota, not for the work: ClinePass's
# "You have reached your 5-hour Clinepass limit", HTTP 429s, rate limits. Matched only on the
# native stream's own error rows, never on the agent's prose.
PROVIDER_LIMIT_PATTERNS = (
    r"limit reached",
    r"rate.?limit",
    r"\b429\b",
    r"quota",
    r"usage limit",
    r"insufficient (?:credits|balance|quota)",
)


def provider_limit_message(native: Path) -> Optional[str]:
    """The provider's own limit message from the native stream, or None.

    On 2026-09-16 two cline packets each ran 52 minutes, then ClinePass cut them off with
    "5-hour limit reached"; the loop saw an exited process with red gates and, with under
    ten minutes left, settled `wall_deadline_before_round` — a deadline that was really a
    quota refusal, so the one different-family retry never fired. Reads the error rows
    (`{"type": "error", "message": ...}`) and a `run_result`/`result` terminal whose
    finish is an error, and returns the first line of the first match."""
    try:
        with native.open("rb") as handle:
            for raw in handle:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                text = None
                if row.get("type") == "error":
                    text = row.get("message")
                elif row.get("type") == "run_result" and row.get("finishReason") == "error":
                    text = row.get("text")
                elif row.get("type") == "result" and row.get("subtype", "").startswith("error"):
                    text = row.get("result") or row.get("error")
                if not isinstance(text, str):
                    continue
                if any(re.search(pat, text, re.IGNORECASE) for pat in PROVIDER_LIMIT_PATTERNS):
                    return text.strip().splitlines()[0][:200]
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


def _gate_mark(result: GateResult) -> tuple:
    """The part of a gate result that must be identical for two rounds to look alike."""
    return (result.name, result.ok, result.reason, result.returncode)


def _gates_unchanged(
    previous: Optional[Sequence[GateResult]], results: Sequence[GateResult]
) -> bool:
    """True when this round's gates ended exactly as the previous round's did.

    With no previous round there is nothing that can have changed, which is what lets
    a first round that does nothing be named an early stop."""
    if previous is None:
        return True
    return [_gate_mark(r) for r in previous] == [_gate_mark(r) for r in results]


def fix_prompt(
    results: Sequence[GateResult],
    round_no: int,
    max_rounds: int,
    stopped_early: bool = False,
    idle_minutes: Optional[int] = None,
) -> str:
    """Deterministic text: which gates failed, their tails, and the rules of the round.

    The rules repeat what the brief already said because the model is being
    re-entered mid-session and a short, exact instruction beats a reference. When the
    previous round moved nothing, that is the first line: a session that answered
    without working has to be told plainly that the answer was not the work. A round the
    idle watchdog cut is told the same way, but it is told to commit or explain rather
    than to start from nothing — the cut may have fallen early in a slow turn."""
    failed = [r for r in results if not r.ok]
    lines = []
    if idle_minutes is not None:
        unit = "minute" if idle_minutes == 1 else "minutes"
        lines += [
            f"the previous round produced no change in {idle_minutes} {unit}; commit what you "
            "have or say why. The worktree and the transcript both stood still, so the harness "
            "cut the round short — finish the packet in this session, on this branch.",
            "",
        ]
    elif stopped_early:
        lines += [
            "your previous session ended without changing anything: the worktree and every "
            "gate result are identical to the round before, so that round was not the work. "
            "Make the change now, in this session, and leave it on the branch.",
            "",
        ]
    lines += [
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
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    idle_seconds: Optional[int] = DEFAULT_IDLE_SECONDS,
    kill_grace: float = 5.0,
    free_bytes: Callable[[Path], int] = free_disk_bytes,
    clock=time,
) -> Dict:
    """Agent → gates → (fix prompt into the same session) …, bounded.

    Returns the verdict dict. `rounds` holds one entry per agent run with its
    gate results; `verified_in_lane` is True only when every declared gate ran
    and passed in the final round and no operator gate was declared. The agent's
    stdout is filtered as it arrives (`stream_transcript`), so the native
    transcript never holds a per-token delta line. No round starts while the
    packets root's filesystem is short of `min_free_bytes`: that verdict is
    `disk_low`, and the free space the guard measured rides in the verdict.

    A round whose tree (`HEAD` plus `git status --porcelain`) stands still and
    whose transcript grows by fewer than `IDLE_TRANSCRIPT_BYTES` for
    `idle_seconds` is cut with `agent_reason: agent_idle` instead of running to
    the wall; its gates run as usual and the next round is told the round was
    idle. Two idle rounds in one attempt — or a single idle round with no round
    or session left to continue on — settle the verdict `agent_idle`."""
    attempt_dir.mkdir(parents=True, exist_ok=True)
    deadline = clock.monotonic() + wall_seconds
    rounds: List[Dict] = []
    session: Optional[str] = None
    reason = "gates_passed"
    started_all = clock.monotonic()
    disk_free: Optional[int] = None
    idle_rounds = 0
    watching = bool(idle_seconds) and idle_seconds > 0
    poll = _poll_seconds(idle_seconds) if watching else None
    for round_no in range(1, max_rounds + 1):
        disk_free = free_bytes(attempt_dir)
        if disk_free < min_free_bytes:
            reason = "disk_low"
            break
        remaining = deadline - clock.monotonic()
        if round_no > 1 and remaining < min_seconds_for_round:
            reason = "wall_deadline_before_round"
            break
        if round_no == 1:
            text = prompt
        else:
            previous = rounds[-1]
            text = fix_prompt(
                previous["results"],
                round_no,
                max_rounds,
                stopped_early=previous["agent_reason"] == "agent_stopped_early",
                idle_minutes=(
                    idle_minutes(idle_seconds) if previous["agent_reason"] == "agent_idle" else None
                ),
            )
        argv = adapter.first(text) if round_no == 1 else adapter.resume(session, text)
        native = attempt_dir / f"native-{round_no}.jsonl"
        stderr = attempt_dir / f"native-{round_no}.stderr"
        (attempt_dir / f"prompt-{round_no}.txt").write_text(text)
        before = worktree_fingerprint(work)
        run_started = clock.monotonic()
        with stderr.open("xb") as err:
            proc = subprocess.Popen(
                sandbox_command(list(argv)),
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err,
                start_new_session=True,
            )
            reader = threading.Thread(
                target=stream_transcript, args=(proc.stdout, native), daemon=True
            )
            reader.start()
            watch = IdleWatch(work, native, idle_seconds, clock) if watching else None
            agent_reason, code = _wait_for_round(proc, deadline, clock, watch, poll, kill_grace)
            reader.join(timeout=30)
        after = worktree_fingerprint(work)
        if session is None:
            session = adapter.session_id(native)
        gate_dir = attempt_dir / f"gates-{round_no}"
        gate_dir.mkdir(exist_ok=True)
        results = run_gates(work, gates, env, gate_dir)
        previous_results = rounds[-1]["results"] if rounds else None
        stopped_early = (
            code == 0
            and agent_reason == "process_exited"
            and before is not None
            and after is not None
            and after == before
            and _gates_unchanged(previous_results, results)
        )
        if stopped_early:
            agent_reason = "agent_stopped_early"
        if agent_reason == "agent_idle":
            idle_rounds += 1
        rounds.append(
            {
                "round": round_no,
                "agent_returncode": code,
                "agent_reason": agent_reason,
                "agent_elapsed_s": round(clock.monotonic() - run_started),
                "agent_terminal": terminal_corroboration(native),
                "results": results,
            }
        )
        if agent_reason == "wall_deadline":
            reason = "wall_deadline"
            break
        if all(r.ok for r in results):
            reason = "gates_passed"
            break
        limit = provider_limit_message(native) if code != 0 else None
        if limit is not None:
            # The provider stopped the agent, not the work: no fix round can help on this
            # account, and the runner's failover reads `transport_error` as its cue.
            reason = "transport_error: provider limit: " + limit
            break
        if agent_reason == "agent_idle" and (
            idle_rounds >= 2 or round_no == max_rounds or session is None
        ):
            # Two idle rounds, or an idle round with nothing left to re-enter, is the
            # operator's to see: hold it now instead of burning the rest of the wall.
            reason = "agent_idle"
            break
        if session is None:
            reason = "no_session_to_resume"
            break
        reason = "agent_stopped_early" if stopped_early else "rounds_exhausted"
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
        "min_free_bytes": min_free_bytes,
        "disk_free_bytes": disk_free,
        "idle_seconds": idle_seconds,
    }
    (attempt_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))
    return verdict


def _kill(proc: subprocess.Popen, grace: float = 5.0) -> None:
    try:
        os.killpg(proc.pid, 15)
    except ProcessLookupError:
        return
    time.sleep(grace)
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
