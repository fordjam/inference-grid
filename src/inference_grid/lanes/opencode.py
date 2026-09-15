"""OpenCode lane: the `opencode` CLI (kind opencode_cli) under the workspace write sandbox.

Evidence comes from the CLI's NDJSON event stream on stdout (native.jsonl), parsed line by
line and qualified by opencode_outcomes.classify_opencode: every line must parse, exactly
one terminal step_finish must echo the asked model, and the last assistant text part is
the terminal text. OpenCode keeps its login on the real home (`~/.local/share/opencode/
auth.json`), so HOME is pointed there for reads only — the sandbox grants no writable
root outside the workspace, and digests of the CLI's config and auth files are taken
before and after the run to prove the user configuration was not modified.

Policy gate, decided by the operator: the deny-read list may name the opencode auth file.
When it does, the lane refuses to start — verdict `credential_denied_by_policy` — before
any process is spawned or any digest is taken; the digest check can prove only that a
readable key was not modified, never that it was not read, so running under that denial
is the operator's call, not the code's.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .. import opencode_outcomes
from ..receipts import safe_path
from . import sandbox

AUTH_RELATIVE = ".local/share/opencode/auth.json"
CONFIG_RELATIVE = ".config/opencode/opencode.json"
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


def parse_rows(raw):
    """NDJSON stdout; one unparsable line makes the whole stream malformed."""
    rows = []
    for line in raw.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            raise ValueError("malformed native event stream") from None
    return rows


def credential_denied(home, deny_roots):
    """The opencode auth path when a deny-read root covers it, else None.

    A root denies when it is the auth file itself or one of its ancestors.
    """
    auth = (Path(home) / AUTH_RELATIVE).resolve()
    for root in deny_roots:
        candidate = Path(root).resolve()
        if auth == candidate or candidate in auth.parents:
            return auth
    return None


def user_config_digests(home):
    """sha256 of the OpenCode config/auth files that currently exist under home."""
    digests = {}
    for relative in (CONFIG_RELATIVE, AUTH_RELATIVE):
        path = Path(home) / relative
        if path.is_file():
            digests[relative] = sandbox_digest(path.read_bytes())
    return digests


def run(request, lane, attempt_dir, *, opencode=None, home=None, deny_roots=None):
    """Execute one attempt; return (receipt or None, verdict). The caller prints the receipt."""
    attempt_dir = Path(attempt_dir)
    home = Path(home or Path.home())
    deny = tuple(deny_roots) if deny_roots is not None else sandbox.deny_read_roots()
    verdict = {
        "supervisor": None,
        "progress": None,
        "usage": None,
        "refusal": None,
    }
    auth = credential_denied(home, deny)
    if auth is not None:
        verdict["refusal"] = (
            "credential_denied_by_policy: the sandbox deny-read list covers " + str(auth)
        )
        return None, verdict
    if opencode is None:
        opencode = lane["executable"]
    work = attempt_dir / "work"
    work.mkdir(mode=0o700)
    staged = stage_inputs(request, work)
    prompt = (work / "brief.txt").read_text().strip()
    profile = sandbox.write_profile(work, attempt_dir / "opencode.sb", deny_read_roots=deny)
    sandbox.probe(profile, work)
    before = user_config_digests(home)
    argv = sandbox.command(
        profile,
        [
            opencode,
            "run",
            "--model",
            request["model"],
            "--format",
            "json",
            "--dir",
            str(work),
            prompt,
        ],
    )
    env = dict(os.environ, HOME=str(home), TMPDIR=str(work), DO_NOT_TRACK="1")
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
    verdict["supervisor"] = supervisor
    if user_config_digests(home) != before:
        verdict["refusal"] = "user configuration changed"
        return None, verdict
    raw = (attempt_dir / "native.jsonl").read_bytes()[:MAX_STDOUT]
    try:
        rows = parse_rows(raw)
    except ValueError as exc:
        verdict["refusal"] = str(exc)
        return None, verdict
    classified = opencode_outcomes.classify_opencode(rows, supervisor, request["model"])
    verdict["progress"] = classified["progress"]
    if classified["outcome"] != "native_complete":
        verdict["refusal"] = "opencode " + classified["outcome"] + ": " + classified["reason"]
        return None, verdict
    native = classified["receipt"]
    verdict["usage"] = native["usage"]
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
        "finish_reason": native["finish_reason"],
        "actual_model": request["model"],
        "manifest_sha256": request["manifest_sha256"],
        "artifacts": artifacts,
    }
    return receipt, verdict


def sandbox_digest(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()
