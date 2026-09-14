"""Codex lane: the OpenAI Codex CLI headless (kind codex_cli) under the workspace write sandbox.

Evidence comes from the CLI's own JSONL event stream on stdout (native.jsonl), classified by
classify_codex against the documented shapes (docs/LANES.md; the operator confirms the
installed CLI's invocation for their version): thread events name the asked model, exactly
one `turn.completed` with usage terminates the stream, and the last agent-message item
carries non-empty terminal text. The login stays in the CLI's own home directory; the
sandbox grants no writable root outside the workspace and the CLI's own state directory.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from ..receipts import safe_path
from . import sandbox

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
    """Names listed in inputs/expected.json, else every new regular file at the workspace root."""
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
        if p.is_file() and not p.name.startswith(".") and p.name not in staged
    )


def parse_events(raw):
    """JSONL stdout; an unparsable line is kept as the string "malformed"."""
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append("malformed")
    return events


def classify_codex(events, supervisor, expected_model):
    """The native outcome from the CLI's event stream and the supervisor's verdict."""
    result = {
        "outcome": "unqualified",
        "reason": "invalid_input",
        "receipt": None,
        "progress": {"turns": 0, "output_tokens_reported": None},
    }
    if not isinstance(events, list) or not all(isinstance(row, dict) for row in events):
        return result
    progress = result["progress"]
    messages = []
    models = []
    turns = []
    for row in events:
        if row.get("type") == "thread.started" and isinstance(row.get("model"), str):
            models.append(row["model"])
        if row.get("type") == "item.completed":
            item = row.get("item")
            if isinstance(item, dict) and item.get("item_type") == "agent_message":
                messages.append(item.get("text"))
        if row.get("type") == "turn.completed":
            turns.append(row)
            progress["turns"] += 1
            usage = row.get("usage")
            if isinstance(usage, dict):
                out_tokens = usage.get("output_tokens")
                if isinstance(out_tokens, int) and not isinstance(out_tokens, bool) and out_tokens >= 0:
                    if progress["output_tokens_reported"] is None:
                        progress["output_tokens_reported"] = 0
                    progress["output_tokens_reported"] += out_tokens
    if not isinstance(supervisor, dict):
        return result
    supervisor_reason = supervisor.get("reason")
    if supervisor_reason in ("wall_deadline", "visible_output_budget", "iteration_budget", "log_byte_budget"):
        result["outcome"] = "interrupted"
        result["reason"] = supervisor_reason
        return result
    if supervisor_reason != "process_exited":
        result["reason"] = "supervision_unqualified"
        return result
    returncode = supervisor.get("returncode")
    if not (isinstance(returncode, int) and not isinstance(returncode, bool) and returncode == 0):
        result["reason"] = "process_failed"
        return result
    if len(turns) != 1 or events[-1] is not turns[0]:
        result["reason"] = "missing_or_multiple_terminals"
        return result
    if not models or any(model != expected_model for model in models):
        result["reason"] = "model_unqualified"
        return result
    text = messages[-1] if messages else None
    if not (isinstance(text, str) and text.strip()):
        result["reason"] = "empty_terminal_text"
        return result
    usage_out = None
    usage = turns[0].get("usage")
    if isinstance(usage, dict):
        in_tokens = usage.get("input_tokens")
        out_tokens = usage.get("output_tokens")
        if (
            isinstance(in_tokens, int)
            and not isinstance(in_tokens, bool)
            and in_tokens >= 0
            and isinstance(out_tokens, int)
            and not isinstance(out_tokens, bool)
            and out_tokens >= 0
        ):
            usage_out = usage
    result["outcome"] = "native_complete"
    result["reason"] = "verified_native_shape"
    result["receipt"] = {
        "actual_model": expected_model,
        "finish_reason": "stop",
        "text": text,
        "usage": usage_out,
    }
    return result


def run(request, lane, attempt_dir, *, home=None):
    """Execute one attempt; return (receipt or None, verdict). The caller prints the receipt."""
    attempt_dir = Path(attempt_dir)
    home = Path(home or Path.home())
    work = attempt_dir / "work"
    work.mkdir(mode=0o700)
    staged = stage_inputs(request, work)
    prompt = (work / "brief.txt").read_text().strip()
    profile = sandbox.write_profile(
        work,
        attempt_dir / "codex.sb",
        extra_write_roots=(home / ".codex",),
        deny_read_roots=sandbox.deny_read_roots(),
    )
    sandbox.probe(profile, work)
    argv = sandbox.command(profile, [lane["executable"], "exec", "--json", prompt])
    env = dict(os.environ, HOME=str(home), TMPDIR=str(work))
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
    raw = (attempt_dir / "native.jsonl").read_bytes()[:MAX_STDOUT]
    events = parse_events(raw)
    verdict = {
        "supervisor": supervisor,
        "usage": None,
        "refusal": None,
    }
    classification = classify_codex(events, supervisor, request["model"])
    verdict["classify"] = {"outcome": classification["outcome"], "reason": classification["reason"]}
    receipt_text = (classification.get("receipt") or {}).get("text")
    if classification["outcome"] != "native_complete":
        verdict["refusal"] = f"codex {classification['outcome']}: {classification['reason']}"
        return None, verdict
    verdict["usage"] = classification["receipt"]["usage"]
    try:
        names = expected_artifacts(work, staged)
    except ValueError as exc:
        verdict["refusal"] = str(exc)
        return None, verdict
    if not names:
        # A reply-only task publishes the response itself as its artifact.
        (work / "reply.txt").write_bytes(receipt_text.strip().encode())
        names = ["reply.txt"]
    if "reply.txt" in names and not (work / "reply.txt").is_file():
        (work / "reply.txt").write_bytes(receipt_text.strip().encode())
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
        artifacts.append({"path": name, "sha256": digest(data)})
    receipt = {
        "status": "completed",
        "finish_reason": "stop",
        "actual_model": request["model"],
        "manifest_sha256": request["manifest_sha256"],
        "artifacts": artifacts,
    }
    return receipt, verdict


def digest(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()
