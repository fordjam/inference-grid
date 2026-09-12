"""Tests for sandbox.py: profile generation, validation, and the probe."""

import os
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from inference_grid.lanes import sandbox

BASE = pathlib.Path("/private/tmp")


def setUpModule():
    if not os.path.exists("/usr/bin/sandbox-exec") or not BASE.is_dir():
        raise unittest.SkipTest("macOS sandbox-exec and /private/tmp required")
# The module only allows workspaces under /private/tmp or ~/.grid-workspaces.
TEST_BASE = BASE


def temp_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="sandbox-test-", dir=str(TEST_BASE)))


class WriteProfileTests(unittest.TestCase):
    def setUp(self):
        self.scratch = temp_dir()
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.workspace = self.scratch / "workspace"
        self.workspace.mkdir()
        self.profile = self.scratch / "workspace.sb"

    def test_profile_written_and_contains_roots(self):
        extra = temp_dir()
        self.addCleanup(shutil.rmtree, extra, ignore_errors=True)
        result = sandbox.write_profile(self.workspace, self.profile, [extra])
        self.assertEqual(result, self.profile.resolve())
        text = self.profile.read_text()
        self.assertIn(json.dumps(str(self.workspace.resolve())), text)
        self.assertIn(json.dumps(str(extra.resolve())), text)
        self.assertIn("/dev/null", text)

    def test_workspace_outside_allowed_bases_refused(self):
        bases = (BASE, pathlib.Path.home() / ".grid-workspaces")
        for candidate in (pathlib.Path.home(), pathlib.Path("/usr")):
            if candidate.is_dir() and not any(
                candidate != base and candidate.is_relative_to(base) for base in bases
            ):
                break
        else:
            self.skipTest("no existing directory outside the allowed bases")
        with self.assertRaises(ValueError):
            sandbox.write_profile(candidate, self.profile)

    def test_profile_inside_workspace_refused(self):
        with self.assertRaises(ValueError):
            sandbox.write_profile(self.workspace, self.workspace / "profile.sb")
        self.assertFalse((self.workspace / "profile.sb").exists())

    def test_missing_extra_root_refused(self):
        missing = self.scratch / "missing-root"
        with self.assertRaises(ValueError):
            sandbox.write_profile(self.workspace, self.profile, [missing])
        self.assertFalse(self.profile.exists())

    def test_probe_passes_for_temporary_workspace(self):
        profile = sandbox.write_profile(self.workspace, self.profile)
        sandbox.probe(profile, self.workspace)


class CommandBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = temp_dir()
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.workspace = self.scratch / "workspace"
        self.workspace.mkdir()
        self.extra = self.scratch / "extra"
        self.extra.mkdir()
        self.profile = sandbox.write_profile(
            self.workspace, self.scratch / "workspace.sb", [self.extra]
        )

    def test_sandboxed_writes_respect_boundary(self):
        inside = self.workspace / "inside.txt"
        in_extra = self.extra / "extra.txt"
        in_parent = self.workspace.parent / "parent.txt"
        good = subprocess.run(
            sandbox.command(self.profile, ["/usr/bin/touch", str(inside)]),
            capture_output=True,
            timeout=10,
        )
        extra_run = subprocess.run(
            sandbox.command(self.profile, ["/usr/bin/touch", str(in_extra)]),
            capture_output=True,
            timeout=10,
        )
        bad = subprocess.run(
            sandbox.command(self.profile, ["/usr/bin/touch", str(in_parent)]),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertTrue(inside.is_file())
        self.assertEqual(extra_run.returncode, 0, extra_run.stderr)
        self.assertTrue(in_extra.is_file())
        self.assertNotEqual(bad.returncode, 0, bad.stderr)
        self.assertFalse(in_parent.exists())

    def test_command_shape_and_refusals(self):
        argv = ["/bin/echo", "hello"]
        self.assertEqual(
            sandbox.command("/tmp/p.sb", argv), ["/usr/bin/sandbox-exec", "-f", "/tmp/p.sb"] + argv
        )
        with self.assertRaises(ValueError):
            sandbox.command("/tmp/p.sb", [])
        with self.assertRaises(ValueError):
            sandbox.command("/tmp/p.sb", ["echo", "hi"])


if __name__ == "__main__":
    unittest.main()
