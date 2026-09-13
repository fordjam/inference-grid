"""One bounded trusted adapter process. This is not an OS security sandbox."""

import hashlib
import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path

from .ledger import Refused, digest
from .receipts import safe_path, validate_receipt


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def regular_beneath(root, relative):
    if not safe_path(relative):
        raise Refused("unsafe relative path")
    path = root / relative
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise Refused("symlink not allowed")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise Refused("artifact missing or outside workspace")
    return path


def exited_without_reaping(proc):
    """Keep the leader PID reserved until its process group has been stopped."""
    if hasattr(os, "waitid"):
        return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    if hasattr(select, "kqueue"):
        queue = select.kqueue()
        try:
            event = select.kevent(
                proc.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            return bool(queue.control([event], 1, 0))
        except ProcessLookupError:
            return True
        finally:
            queue.close()
    raise RuntimeError("non-reaping supervision unavailable on this platform")


def stop_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin may deny signals to an all-zombie group. Never ignore a live member.
        listing = subprocess.check_output(["/bin/ps", "-axo", "pgid=,stat="], text=True)
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) != 2:
                raise RuntimeError("cannot verify process group state")
            if int(fields[0]) == proc.pid and not fields[1].startswith("Z"):
                raise RuntimeError("live process group could not be stopped")
    proc.wait()


def execute(ledger, aid, generation):
    row = ledger.start(aid, generation)
    if row is None:
        return "duplicate_or_stale"
    spec = row["spec"]
    proc = None
    stopped = False
    try:
        directory = Path(spec["workspace"]) / aid
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        manifest = spec.get("inputs", {})
        if digest(manifest) != spec["manifest_sha256"]:
            raise Refused("input manifest changed")
        for name, expected in manifest.items():
            source = regular_beneath(Path(spec["input_root"]), name)
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise Refused("input content changed")
            dest = directory / "inputs" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        output = directory / "artifacts"
        output.mkdir()
        request = dict(
            attempt=aid,
            generation=generation,
            model=spec["model"],
            manifest_sha256=spec["manifest_sha256"],
            input_directory=str(directory / "inputs"),
            output_directory=str(output),
            # The submitting board's task budget, for lanes that shape their own transport
            # bounds; absent when the spec does not carry one.
            wall_seconds=spec.get("wall_seconds"),
        )
        # Credentials belong to the trusted adapter, not task arguments or this ledger.
        env = {k: os.environ[k] for k in ("PATH", "LANG", "SYSTEMROOT") if k in os.environ}
        # A file avoids blocking on stdin when an adapter does not read its request.
        request_path = directory / "request.json"
        request_path.write_text(json.dumps(request))
        with (
            request_path.open("rb") as stdin,
            (directory / "stdout").open("wb") as stdout,
            (directory / "stderr").open("wb") as stderr,
        ):
            proc = subprocess.Popen(
                spec["argv"],
                cwd=directory,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            deadline = time.monotonic() + spec["timeout"]
            cap = spec["output_bytes"]
            while not exited_without_reaping(proc):
                if time.monotonic() >= deadline:
                    raise Refused("timeout: provider acceptance may be ambiguous")
                if sum((directory / p).stat().st_size for p in ("stdout", "stderr")) > cap:
                    raise Refused("output limit exceeded")
                time.sleep(0.02)
        stop_group(proc)
        stopped = True
        if proc.returncode != 0:
            raise Refused("adapter exited without a successful terminal receipt")
        if sum((directory / p).stat().st_size for p in ("stdout", "stderr")) > cap:
            raise Refused("output limit exceeded")
        receipt = json.loads((directory / "stdout").read_text())
        errors = validate_receipt(receipt, spec["model"], spec["manifest_sha256"])
        if errors:
            raise Refused("; ".join(errors))
        for artifact in receipt["artifacts"]:
            path = regular_beneath(output, artifact["path"])
            if path.stat().st_size > cap:
                raise Refused("artifact size exceeds limit")
            if file_hash(path) != artifact["sha256"]:
                raise Refused("artifact content mismatch")
        ledger.finish(aid, generation, receipt)
        return "completed"
    except Exception as exc:
        ledger.hold(aid, type(exc).__name__ + ": " + str(exc)[:500])
        return "held"
    finally:
        if proc is not None and not stopped:
            stop_group(proc)
