"""ZCode lane: the bundled headless `zcode-cli` under the workspace write sandbox.

Evidence comes from two controller-readable places, never from model text: the CLI's final JSON
document on stdout (response, usage, sessionId) and its own session database, whose model_usage
rows record provider, model, status and finish reason per request. Credentials stay in the CLI's
own login; nothing secret is read or written here.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from ..receipts import safe_path
from . import sandbox

DEFAULT_CLI = "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
PROVIDER_ID = "builtin:zai-coding-plan"
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


def parse_result(raw):
    """The CLI may print warnings before its single JSON document."""
    start = raw.find(b"{")
    if start < 0:
        return None
    try:
        document = json.loads(raw[start:])
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def session_usage(db_path, session_id):
    con = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select provider_id, model_id, variant, status, finish_reason, input_tokens, "
            "output_tokens, reasoning_tokens, cache_read_input_tokens, error_type "
            "from model_usage where session_id = ? order by started_at",
            (session_id,),
        ).fetchall()
    finally:
        con.close()
    keys = (
        "provider_id",
        "model_id",
        "variant",
        "status",
        "finish_reason",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_input_tokens",
        "error_type",
    )
    return [dict(zip(keys, row)) for row in rows]


def qualify(rows, model):
    """All requests must have completed and the last must have stopped normally."""
    if not rows:
        return "no native model requests recorded"
    for row in rows:
        if row["provider_id"] != PROVIDER_ID:
            return "request served by an unexpected provider"
        if (row["model_id"] or "").lower() != model.lower():
            return "request served by an unexpected model"
        if row["status"] != "completed":
            return "a native request did not complete"
    if rows[-1]["finish_reason"] != "stop":
        return "final native request did not stop normally"
    return None


def run(request, lane, attempt_dir, *, cli=DEFAULT_CLI, db_path=None, home=None, mode="yolo"):
    """Execute one attempt; return (receipt or None, verdict). The caller prints the receipt."""
    attempt_dir = Path(attempt_dir)
    home = Path(home or Path.home())
    db_path = Path(db_path or home / ".zcode/cli/db/db.sqlite")
    work = attempt_dir / "work"
    work.mkdir(mode=0o700)
    staged = stage_inputs(request, work)
    prompt = (work / "brief.txt").read_text().strip()
    profile = sandbox.write_profile(
        work, attempt_dir / "zcode.sb", extra_write_roots=(home / ".zcode",)
    )
    sandbox.probe(profile, work)
    argv = sandbox.command(
        profile,
        [
            lane["executable"],
            cli,
            "--prompt",
            prompt,
            "--mode",
            mode,
            "--json",
            "--no-color",
            "--cwd",
            str(work),
        ],
    )
    env = dict(os.environ, HOME=str(home), TMPDIR=str(work))
    started = time.monotonic()
    with (
        (attempt_dir / "native.out").open("xb") as out,
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
    raw = (attempt_dir / "native.out").read_bytes()[:MAX_STDOUT]
    document = parse_result(raw)
    rows = []
    if document and isinstance(document.get("sessionId"), str) and db_path.is_file():
        rows = session_usage(db_path, document["sessionId"])
    verdict = {
        "supervisor": supervisor,
        "session": document.get("sessionId") if document else None,
        "usage": document.get("usage") if document else None,
        "model_usage": rows,
        "refusal": None,
    }
    if reason != "process_exited" or code != 0 or document is None:
        verdict["refusal"] = "no qualified native terminal"
        return None, verdict
    problem = qualify(rows, request["model"])
    if problem:
        verdict["refusal"] = problem
        return None, verdict
    text = document.get("response")
    if not isinstance(text, str) or not text.strip():
        verdict["refusal"] = "empty terminal text"
        return None, verdict
    try:
        names = expected_artifacts(work, staged)
    except ValueError as exc:
        verdict["refusal"] = str(exc)
        return None, verdict
    if not names:
        # A reply-only task publishes the response itself as its artifact.
        (work / "reply.txt").write_bytes(text.strip().encode())
        names = ["reply.txt"]
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
