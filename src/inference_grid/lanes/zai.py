# Authored by ZCode (GLM-5.3-Flash) through Grid attempts zcode-zai-lane-1/2. Coordinator edits:
# package import, scratch HOME default, safe_path artifact names (see docs/CONTRIBUTIONS.md).
"""Z.ai lane: headless Claude Code against the GLM Coding Plan endpoint under the workspace
write sandbox.

Evidence comes from Claude Code's own stream-json record on stdout: the last `result` row
carries the response text, usage, num_turns and per-model modelUsage, so qualification never
trusts prose. The API key is read from a 0o600 credential file and reaches only the process
environment (HOME is redirected to `home`, the attempt's scratch home, which is added to the
writable roots); the key is never written into the verdict, receipt or any file under the
attempt directory.
"""

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from ..receipts import safe_path
from . import sandbox

DEFAULT_CLAUDE = "/Users/fordjam/.local/bin/claude"
DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
HAIKU_MODEL = "glm-5.3-flash"
MAX_STDOUT = 4 * 1024 * 1024
STRIPPED_ENV = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


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


def read_key(credential_path):
    """Return (api_key, None), or (None, refusal reason) for an unusable credential file."""
    path = Path(credential_path)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None, "credential file missing"
    if mode != 0o600:
        return None, "credential file must be mode 0o600"
    try:
        document = json.loads(path.read_text())
    except ValueError:
        return None, "credential file is not valid JSON"
    key = document.get("api_key") if isinstance(document, dict) else None
    if not isinstance(key, str) or not key:
        return None, "credential api_key is empty"
    return key, None


def build_env(request, lane, home, work, key, *, thinking_tokens, base_url):
    """Point Claude Code at the Z.ai endpoint; strip every other provider credential."""
    env = {name: value for name, value in os.environ.items() if name not in STRIPPED_ENV}
    env.update(
        {
            "HOME": str(home),
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": request["model"],
            "ANTHROPIC_DEFAULT_SONNET_MODEL": request["model"],
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": HAIKU_MODEL,
            "MAX_THINKING_TOKENS": str(thinking_tokens),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": str(lane["wall_seconds"] * 1000),
            "TMPDIR": str(work),
        }
    )
    return env


def parse_rows(raw):
    """stream-json prints one JSON object per line, warnings included; keep the dict rows."""
    rows = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def terminal_result(rows):
    for row in reversed(rows):
        if row.get("type") == "result":
            return row
    return None


def qualify(result_row, model):
    """The run must have succeeded and every model request must have gone to the asked model."""
    if result_row.get("subtype") != "success":
        return "native result was not a success"
    if result_row.get("is_error") is not False:
        return "native result reported an error"
    usage = result_row.get("modelUsage")
    if not isinstance(usage, dict) or [k.lower() for k in usage] != [model.lower()]:
        return "request served by an unexpected model"
    return None


def run(
    request,
    lane,
    attempt_dir,
    *,
    claude=DEFAULT_CLAUDE,
    home=None,
    thinking_tokens=6000,
    base_url=DEFAULT_BASE_URL,
):
    """Execute one attempt; return (receipt or None, verdict). The caller prints the receipt."""
    attempt_dir = Path(attempt_dir)
    # Scratch HOME per attempt: the real home must never become a writable sandbox root.
    home = Path(home or (attempt_dir / "home"))
    home.mkdir(parents=True, exist_ok=True)
    work = attempt_dir / "work"
    work.mkdir(mode=0o700)
    staged = stage_inputs(request, work)
    prompt = (work / "brief.txt").read_text().strip()
    verdict = {
        "supervisor": None,
        "session": None,
        "usage": None,
        "model_usage": None,
        "num_turns": None,
        "refusal": None,
    }
    key, problem = read_key(lane["credential_path"])
    if problem:
        verdict["refusal"] = problem
        return None, verdict
    profile = sandbox.write_profile(work, attempt_dir / "claude.sb", extra_write_roots=(home,))
    sandbox.probe(profile, work)
    argv = sandbox.command(
        profile,
        [
            claude,
            "--print",
            "--model",
            request["model"],
            "--tools",
            "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            "1",
        ],
    )
    env = build_env(
        request, lane, home, work, key, thinking_tokens=thinking_tokens, base_url=base_url
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
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            proc.communicate(prompt.encode(), timeout=lane["wall_seconds"])
            code = proc.returncode
            reason = "process_exited"
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 15)
            time.sleep(1)
            if proc.poll() is None:
                os.killpg(proc.pid, 9)
            proc.communicate()
            code = proc.returncode
            reason = "wall_deadline"
    verdict["supervisor"] = {
        "reason": reason,
        "returncode": code,
        "elapsed": round(time.monotonic() - started, 1),
    }
    raw = (attempt_dir / "native.jsonl").read_bytes()[:MAX_STDOUT]
    result_row = terminal_result(parse_rows(raw))
    if result_row:
        verdict["session"] = result_row.get("session_id")
        verdict["usage"] = result_row.get("usage")
        verdict["model_usage"] = result_row.get("modelUsage")
        verdict["num_turns"] = result_row.get("num_turns")
    if reason != "process_exited" or code != 0 or result_row is None:
        verdict["refusal"] = "no qualified native terminal"
        return None, verdict
    problem = qualify(result_row, request["model"])
    if problem:
        verdict["refusal"] = problem
        return None, verdict
    text = result_row.get("result")
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
        data = (work / name).read_bytes()
        (output / name).write_bytes(data)
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
