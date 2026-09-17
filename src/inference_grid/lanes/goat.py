# Authored by ZCode (GLM-5.3-Flash) through Grid board task lane-goat; coordinator edits: package
# imports for goat_outcomes, sandbox and receipts.safe_path (see docs/CONTRIBUTIONS.md).
"""Command Code GOAT lane: headless `cmd` (kind goat_cli) under the workspace write sandbox.

Evidence comes from the CLI's NDJSON stream on stdout (native.jsonl), parsed line by line and
normalized by goat_outcomes.classify_goat: every model_request_end event must name the asked
model, the bundled grid-effort.mjs module must have forced effort "low" on every request, and
exactly one terminal result row must have completed normally. Command Code keeps its login in
the real home, so HOME is pointed there for reads only; the sandbox grants no writable root
outside the workspace, and config/auth digests are taken before and after the run to prove the
user configuration was not modified.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .. import goat_outcomes
from ..receipts import safe_path
from . import sandbox

EFFORT_MODULE_NAME = "grid-effort.mjs"
EFFORT_MODULE_TEXT = (
    "export default function (cmd) { cmd.on('session_start', () => { cmd.setEffort('low'); }); }"
)
MAX_STDOUT = 4 * 1024 * 1024


def stage_inputs(request, work):
    names = []
    root = Path(request["input_directory"])
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root)
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            names.append(str(relative))
    return names


def expected_artifacts(work, staged):
    """Names listed in inputs/expected.json, else every new regular file at the workspace root.

    The effort module is lane infrastructure, never an artifact.
    """
    listing = work / "expected.json"
    if listing.is_file():
        names = json.loads(listing.read_text())
        if not isinstance(names, list) or not all(
            isinstance(n, str) and safe_path(n) for n in names
        ):
            raise ValueError("expected.json must list safe relative artifact names")
        return names
    return sorted(
        p.name
        for p in work.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.name != EFFORT_MODULE_NAME
        and p.name not in staged
    )


_DELTA_MARKERS = (b'"thinking_delta"', b'"text_delta"', b'"content_delta"', b'"message_update"')


def _is_delta_line(line):
    """A streaming delta row (thinking/text fragments, message updates): bulk, no evidence."""
    return any(marker in line for marker in _DELTA_MARKERS) and b'"model_request_end"' not in line


def parse_rows(raw):
    """NDJSON stdout; an unparsable line is kept as the string "malformed"."""
    rows = []
    for line in raw.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append("malformed")
    return rows


def effort_values(rows):
    """The effort value of every model_request_end event, in order."""
    values = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "event":
            continue
        event = row.get("event")
        if isinstance(event, dict) and event.get("type") == "model_request_end":
            values.append(event.get("effort"))
    return values


def user_config_digests(home):
    """sha256 of the Command Code config/auth files that currently exist under home."""
    digests = {}
    for name in ("config.json", "auth.json"):
        path = Path(home) / ".commandcode" / name
        if path.is_file():
            digests[name] = sandbox_digest(path.read_bytes())
    return digests


def run(request, lane, attempt_dir, *, cmd=None, max_turns=12, home=None):
    """Execute one attempt; return (receipt or None, verdict). The caller prints the receipt."""
    attempt_dir = Path(attempt_dir)
    # Command Code keeps its login in the real home; HOME only needs to be readable, so the
    # sandbox profile below deliberately grants no writable root outside the workspace.
    home = Path(home or Path.home())
    if cmd is None:
        cmd = lane["executable"]
    work = attempt_dir / "work"
    work.mkdir(mode=0o700)
    staged = stage_inputs(request, work)
    prompt = (work / "brief.txt").read_text().strip()
    (work / EFFORT_MODULE_NAME).write_text(EFFORT_MODULE_TEXT)
    profile = sandbox.write_profile(
        work,
        attempt_dir / "goat.sb",
        deny_read_roots=sandbox.deny_read_roots(),
    )
    sandbox.probe(profile, work)
    before = user_config_digests(home)
    argv = sandbox.command(
        profile,
        [
            cmd,
            "--print",
            prompt,
            "--model",
            request["model"],
            "--max-turns",
            str(max_turns),
            "--output-format",
            "json",
            "--skip-onboarding",
            "--no-auto-update",
            "--no-skills",
            "--no-session",
            "--yolo",
            "--mod",
            str(work / EFFORT_MODULE_NAME),
            "--tools-all",
        ],
    )
    env = dict(
        os.environ,
        HOME=str(home),
        TMPDIR=str(work),
        COMMANDCODE_SKIP_UPDATES="1",
        DO_NOT_TRACK="1",
    )
    started = time.monotonic()
    with (
        (attempt_dir / "native.jsonl").open("xb") as out,
        (attempt_dir / "native.stderr").open("xb") as err,
    ):
        proc = subprocess.Popen(
            argv,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=lane["wall_seconds"])
            reason = "process_exited"
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 15)
            time.sleep(1)
            if proc.poll() is None:
                os.killpg(proc.pid, 9)
            code = proc.wait()
            reason = "wall_deadline"
    supervisor = {
        "reason": reason,
        "returncode": code,
        "elapsed": round(time.monotonic() - started, 1),
    }
    verdict = {
        "supervisor": supervisor,
        "progress": None,
        "usage": None,
        "effort": None,
        "refusal": None,
    }
    if user_config_digests(home) != before:
        verdict["refusal"] = "user configuration changed"
        return None, verdict
    # The whole stream, not its first 4 MiB: Kimi K3's thinking deltas made an 18 MB
    # transcript of one review (2026-09-16), the cut fell inside a line, the fragment
    # parsed as "malformed" and the classifier refused a complete, successful run as
    # invalid_input — with the `result` row sitting unread past the cut. Delta rows carry
    # no evidence the classifier reads, so they are dropped as the file streams; what is
    # kept is bounded by MAX_STDOUT and a run past that bound is refused by name.
    rows, kept_bytes, transcript_bytes = [], 0, 0
    with (attempt_dir / "native.jsonl").open("rb") as stream:
        for line in stream:
            transcript_bytes += len(line)
            if _is_delta_line(line):
                continue
            kept_bytes += len(line)
            if kept_bytes > MAX_STDOUT:
                verdict["refusal"] = (
                    f"native transcript exceeds {MAX_STDOUT} bytes after dropping deltas"
                )
                verdict["transcript_bytes"] = transcript_bytes
                return None, verdict
            rows.extend(parse_rows(line))
    verdict["transcript_bytes"] = transcript_bytes
    # Command Code echoes some vendors' ids in their own casing — `moonshotai/Kimi-K3`,
    # `Qwen/Qwen3.8-Flash` — while the lane names them lower-case (2026-09-16: both
    # canaries refused `model_unqualified` on a served reply). The classifier's exact
    # comparison is the provider's, kept unmodified; the rows it reads are folded to the
    # lane's casing first, and the verdict records what was actually served.
    asked = request["model"]
    served = set()

    def _fold(row):
        """The row with any model id equal to the asked one ignoring case replaced by it."""
        if not isinstance(row, dict):
            return row
        out = dict(row)
        model = out.get("model")
        if isinstance(model, str) and model != asked and model.lower() == asked.lower():
            served.add(model)
            out["model"] = asked
        if isinstance(out.get("event"), dict):
            out["event"] = _fold(out["event"])
        return out

    folded = [_fold(e) for e in rows]
    if served:
        verdict["served_model_ids"] = sorted(served)
    classified = goat_outcomes.classify_goat(folded, supervisor, request["model"])
    verdict["progress"] = classified["progress"]
    if classified["outcome"] != "native_complete":
        verdict["refusal"] = "goat " + classified["outcome"] + ": " + classified["reason"]
        return None, verdict
    efforts = effort_values(rows)
    if not efforts or any(value != "low" for value in efforts):
        verdict["refusal"] = "effort evidence missing"
        return None, verdict
    native = classified["receipt"]
    verdict["usage"] = native["usage"]
    verdict["effort"] = native["effort"]
    text = native["text"]
    try:
        names = expected_artifacts(work, staged)
    except ValueError as exc:
        verdict["refusal"] = str(exc)
        return None, verdict
    if not names:
        # A reply-only task publishes the response itself as its artifact.
        (work / "reply.txt").write_bytes(text.strip().encode())
        names = ["reply.txt"]
    if "reply.txt" in names and not (work / "reply.txt").is_file():
        # A task may ask for the response itself under its conventional name.
        (work / "reply.txt").write_bytes(text.strip().encode())
    missing = [n for n in names if not (work / n).is_file()]
    if missing:
        verdict["refusal"] = "expected artifacts missing: " + ", ".join(missing)
        return None, verdict
    artifacts = []
    output = Path(request["output_directory"])
    for name in names:
        source = (work / name).resolve()
        target = (output / name).resolve()
        if not source.is_relative_to(work.resolve()) or not target.is_relative_to(output.resolve()):
            verdict["refusal"] = "artifact path escapes the workspace or output directory"
            return None, verdict
        data = source.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        artifacts.append({"path": name, "sha256": sandbox_digest(data)})
    receipt = {
        "status": "completed",
        "finish_reason": "stop",
        "actual_model": request["model"],
        "manifest_sha256": request["manifest_sha256"],
        "artifacts": artifacts,
    }
    return receipt, verdict


def sandbox_digest(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()
