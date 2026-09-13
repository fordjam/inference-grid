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
from pathlib import Path, PurePosixPath

import goat_outcomes
import sandbox

EFFORT_MODULE_NAME = "grid-effort.mjs"
EFFORT_MODULE_TEXT = (
    "export default function (cmd) { cmd.on('session_start', () => { cmd.setEffort('low'); }); }"
)
MAX_STDOUT = 4 * 1024 * 1024


def safe_path(name):
    """True for relative artifact names that cannot escape the workspace."""
    if not isinstance(name, str) or not name:
        return False
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts:
        return False
    return all(part not in ("..", ".") for part in pure.parts)


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
    raw = (attempt_dir / "native.jsonl").read_bytes()[:MAX_STDOUT]
    rows = parse_rows(raw)
    classified = goat_outcomes.classify_goat(rows, supervisor, request["model"])
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
