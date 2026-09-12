# Authored by ZCode (GLM-5.3-Flash) through a Grid attempt; integrated unmodified after local
# tests. See docs/CONTRIBUTIONS.md.
"""Build a macOS sandbox-exec profile that allows writes only inside a scoped
workspace, and prove the boundary with a probe before it is trusted.

This enforces write boundaries only, NOT read/network isolation. No workload is
launched by this module; callers wrap their own argv via command().
"""

from pathlib import Path
import json
import subprocess
import uuid

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _allowed_bases():
    return (Path("/private/tmp"), Path.home() / ".grid-workspaces")


def write_profile(workspace, profile_path, extra_write_roots=(), deny_read_roots=()):
    """Write a deny-all-writes profile scoped to workspace plus extra roots.

    deny_read_roots are absolute paths the workload may not read at all (personal data,
    credential stores); they are denied even when they exist inside a writable root.
    Returns the resolved profile path.
    """
    try:
        root = Path(workspace).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"workspace must be an existing directory: {workspace}") from exc
    target = Path(profile_path).resolve()
    if not root.is_dir() or root == Path("/") or target.is_relative_to(root):
        raise ValueError("existing workspace and separate profile path required")
    if not any(root != base and root.is_relative_to(base) for base in _allowed_bases()):
        raise ValueError("workspace must lie under /private/tmp or ~/.grid-workspaces")
    filters = ['(literal "/dev/null")']
    for extra in extra_write_roots:
        extra_path = Path(extra)
        if not extra_path.is_absolute():
            raise ValueError(f"extra write root must be an absolute path: {extra}")
        try:
            extra_path = extra_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"extra write root must be an existing directory: {extra}") from exc
        if not extra_path.is_dir():
            raise ValueError(f"extra write root must be an existing directory: {extra}")
        filters.append("(subpath " + json.dumps(str(extra_path)) + ")")
    denied = []
    for path in deny_read_roots:
        denied_path = Path(path)
        if not denied_path.is_absolute():
            raise ValueError(f"deny-read root must be an absolute path: {path}")
        denied.append("(subpath " + json.dumps(str(denied_path)) + ")")
        denied.append("(literal " + json.dumps(str(denied_path)) + ")")
    # JSON quoting is also valid for these sandbox Scheme string literals. Read denial is
    # written last so it takes precedence over the default allow.
    with target.open("w") as stream:
        stream.write(
            "(version 1)\n(allow default)\n(deny file-write*)\n"
            "(allow file-write* (subpath "
            + json.dumps(str(root))
            + ") "
            + " ".join(filters)
            + ")\n"
        )
        if denied:
            stream.write("(deny file-read* " + " ".join(denied) + ")\n")
    return target


def deny_read_roots(config_path=None):
    """Operator deny-list: absolute paths no lane workload may read (private JSON list)."""
    path = Path(config_path or Path.home() / ".config/inference-grid/deny-read.json")
    try:
        roots = json.loads(path.read_text())
    except (OSError, ValueError):
        return ()
    if not isinstance(roots, list) or not all(
        isinstance(r, str) and r.startswith("/") for r in roots
    ):
        raise ValueError("deny-read.json must be a list of absolute paths")
    return tuple(roots)


def probe(profile_path, workspace):
    """Prove the profile allows writes inside workspace and denies the parent.

    Raises RuntimeError on any failure. Removes every probe file.
    """
    try:
        root = Path(workspace).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"probe workspace missing: {workspace}") from exc
    target = Path(profile_path)
    nonce = ".write-probe-" + uuid.uuid4().hex
    inside = root / nonce
    outside = root.parent / nonce
    try:
        # Demonstrate the denied target is ordinarily writable, not merely
        # protected by its parent directory permissions.
        try:
            with outside.open("xb") as stream:
                stream.write(b"control")
        except OSError as exc:
            raise RuntimeError(f"control write outside sandbox failed: {exc}") from exc
        outside.unlink()
        prefix = [SANDBOX_EXEC, "-f", str(target), "/usr/bin/touch"]
        good = subprocess.run(prefix + [str(inside)], capture_output=True, timeout=5)
        bad = subprocess.run(prefix + [str(outside)], capture_output=True, timeout=5)
        if good.returncode != 0 or not inside.is_file() or bad.returncode == 0 or outside.exists():
            raise RuntimeError(
                "sandbox write isolation probe failed: "
                f"inside rc={good.returncode} stderr={good.stderr!r}, "
                f"outside rc={bad.returncode} stderr={bad.stderr!r}"
            )
    finally:
        inside.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)


def command(profile_path, argv):
    """Wrap argv for execution under the profile's write sandbox."""
    argv = [str(arg) for arg in argv]
    if not argv:
        raise ValueError("argv must not be empty")
    if not Path(argv[0]).is_absolute():
        raise ValueError("argv[0] must be an absolute path")
    return [SANDBOX_EXEC, "-f", str(profile_path), *argv]
