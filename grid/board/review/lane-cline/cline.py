# Authored by ZCode (GLM-5.3-Flash) through a Grid attempt. Flat-checkout note: this packet
# keeps the modules side by side without the inference_grid package on disk, so the receipts
# import below falls back to a local safe_path mirror; test_cline.py loads this module
# through a tiny inference_grid.lanes package shim so every other import stays
# package-relative. See docs/CONTRIBUTIONS.md.
"""Cline lane: the ClinePass CLI (kind cline_cli) under the workspace write sandbox.

Evidence comes from the CLI's own NDJSON event stream on stdout: every line must parse as
JSON, and cline_outcomes.classify_cline qualifies the run from the terminal run_result
record (finish reason, provider/model id, terminal text) plus the controller-owned
supervisor result, never from model prose. The API key is read from a 0o600 credential
file and reaches only the process environment around the supervised call; HOME defaults
to the real home and is never a writable sandbox root, and the key is never written into
the verdict, receipt or any file under the attempt directory. Artifacts must be declared
in the staged expected.json; an artifact the agent deleted after an iteration is served
from the newest controller-side snapshot that still contains it, and the verdict records
what served each artifact.
"""

import json
import os
import shutil
import stat
from pathlib import Path, PurePosixPath

from . import cline_outcomes
from . import sandbox
from . import supervision

try:
    from ..receipts import safe_path
except ImportError:
    # Flat packet checkout: inference_grid.receipts is not present here, so mirror its
    # contract (safe relative artifact names, no absolute or escaping segments) locally.
    def safe_path(name):
        path = PurePosixPath(name)
        return (
            bool(name)
            and not path.is_absolute()
            and all(part not in ("", ".", "..") for part in path.parts)
        )


MAX_STDOUT = 4 * 1024 * 1024
ENV_KEYS = ("CLINE_API_KEY", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE")


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


def expected_artifacts(work):
    """This lane requires a staged expected.json listing safe relative artifact names."""
    listing = work / "expected.json"
    if not listing.is_file():
        raise ValueError("expected.json is required for this lane")
    names = json.loads(listing.read_text())
    if not isinstance(names, list) or not all(
        isinstance(n, str) and safe_path(n) for n in names
    ):
        raise ValueError("expected.json must list safe relative artifact names")
    return names


def parse_rows(raw):
    """Every native line is one JSON object; anything else is a malformed event stream."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("malformed native event stream")
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            raise ValueError("malformed native event stream")
    return rows


def newest_snapshot_with(snapshots_dir, name):
    """The newest iter-N directory holding name (a passing file the agent deleted), else None."""
    try:
        children = list(Path(snapshots_dir).iterdir())
    except OSError:
        return None

    def order(path):
        stem = path.name.rsplit("-", 1)[-1]
        return (int(stem) if stem.isdigit() else -1, path.name)

    best = None
    for child in sorted(children, key=order):
        if child.is_dir() and (child / name).is_file():
            best = child
    return best


def run(
    request,
    lane,
    attempt_dir,
    *,
    cline=None,
    thinking="low",
    retries=1,
    char_limit=16000,
    iteration_limit=8,
    home=None,
):
    """Execute one attempt; return (receipt or None, verdict). The caller prints the receipt."""
    attempt_dir = Path(attempt_dir)
    # HOME defaults to the real home; unlike the scratch-home lanes it is never added to
    # the sandbox's writable roots, so nothing under HOME can be modified by the workload.
    home = Path(home or Path.home())
    cline = cline or lane["executable"]
    work = attempt_dir / "work"
    work.mkdir(mode=0o700)
    snapshots = attempt_dir / "snapshots"
    snapshots.mkdir(mode=0o700)
    stage_inputs(request, work)
    prompt = (work / "brief.txt").read_text().strip()
    verdict = {
        "supervisor": None,
        "outcome": None,
        "progress": None,
        "artifact_sources": {},
        "refusal": None,
    }
    key, problem = read_key(lane["credential_path"])
    if problem:
        verdict["refusal"] = problem
        return None, verdict
    profile = sandbox.write_profile(
        work,
        attempt_dir / "cline.sb",
        deny_read_roots=sandbox.deny_read_roots(),
    )
    sandbox.probe(profile, work)
    argv = sandbox.command(
        profile,
        [
            cline,
            "--provider",
            "cline",
            "--model",
            request["model"],
            "--json",
            "--timeout",
            str(lane["wall_seconds"]),
            "--retries",
            str(retries),
            "--thinking",
            thinking,
            "--cwd",
            str(work),
            "--data-dir",
            str(work / ".cline-state"),
            prompt,
        ],
    )
    # bounded_run_with_snapshots spawns from this process's environment, so the key is
    # published only for the supervised call and every previous value is restored after.
    saved = {name: os.environ.get(name) for name in ENV_KEYS}
    os.environ.update(
        {
            "CLINE_API_KEY": key,
            "HOME": str(home),
            "TMPDIR": str(work),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        supervisor = supervision.bounded_run_with_snapshots(
            argv,
            work,
            attempt_dir / "native.jsonl",
            snapshots,
            seconds=lane["wall_seconds"],
            char_limit=char_limit,
            iteration_limit=iteration_limit,
        )
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    verdict["supervisor"] = supervisor
    try:
        rows = parse_rows((attempt_dir / "native.jsonl").read_bytes()[:MAX_STDOUT])
    except ValueError as exc:
        verdict["refusal"] = str(exc)
        return None, verdict
    classifier = cline_outcomes.classify_cline(rows, supervisor, request["model"])
    verdict["outcome"] = classifier["outcome"]
    verdict["progress"] = classifier["progress"]
    if classifier["outcome"] != "native_complete":
        verdict["refusal"] = "cline " + classifier["outcome"] + ": " + classifier["reason"]
        return None, verdict
    try:
        names = expected_artifacts(work)
    except ValueError as exc:
        verdict["refusal"] = str(exc)
        return None, verdict
    # An artifact still in the workspace comes from there; otherwise the newest snapshot
    # that still holds it serves, and the verdict records which place served each name.
    sources = {}
    missing = []
    for name in names:
        if (work / name).is_file():
            sources[name] = "work"
            continue
        snapshot = newest_snapshot_with(snapshots, name)
        if snapshot is None:
            missing.append(name)
        else:
            sources[name] = snapshot.name
    if missing:
        verdict["refusal"] = "expected artifacts missing: " + ", ".join(missing)
        return None, verdict
    verdict["artifact_sources"] = sources
    artifacts = []
    output = Path(request["output_directory"])
    for name in names:
        root = work if sources[name] == "work" else snapshots / sources[name]
        source = (root / name).resolve()
        target = (output / name).resolve()
        if not source.is_relative_to(root.resolve()) or not target.is_relative_to(output.resolve()):
            verdict["refusal"] = "artifact path escapes the workspace, snapshot or output directory"
            return None, verdict
        data = source.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        artifacts.append({"path": name, "sha256": sandbox_digest(data)})
    receipt = {
        "status": "completed",
        "finish_reason": classifier["receipt"]["finish_reason"],
        "actual_model": classifier["receipt"]["actual_model"],
        "manifest_sha256": request["manifest_sha256"],
        "artifacts": artifacts,
    }
    return receipt, verdict


def sandbox_digest(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()
