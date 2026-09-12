import sys
import tempfile
import unittest
from pathlib import Path

from inference_grid.lanes.supervision import bounded_run_with_snapshots


class SnapshotSupervisionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolve so paths match the snapshot paths the function returns
        # (macOS reports TMPDIR as /var/... while resolve() yields /private/var/...).
        self.base = Path(tmp.name).resolve()
        self.agent_cwd = self.base / "agent"
        self.agent_cwd.mkdir()
        self.snapshot_dir = self.base / "snapshots"
        # Log lives inside the supervised tree so its exclusion is exercised.
        self.log = self.agent_cwd / "run.ndjson"
        # Fake agent script stays outside the supervised tree.
        self.script = self.base / "fake_agent.py"

    def run_fake_agent(self, body):
        self.script.write_text(body)
        return bounded_run_with_snapshots(
            [sys.executable, "-u", str(self.script)],
            str(self.agent_cwd),
            str(self.log),
            str(self.snapshot_dir),
        )

    def test_snapshots_taken_at_each_iteration_end(self):
        body = (
            "\n".join(
                [
                    "import json, os, time",
                    "def emit(event):",
                    "    print(json.dumps({'event': event}), flush=True)",
                    "emit({'type': 'iteration_start', 'iteration': 1})",
                    "with open('passing.py', 'w') as f:",
                    "    f.write('VALUE = 1\\n')",
                    "emit({'type': 'iteration_end', 'iteration': 1})",
                    "time.sleep(0.3)",
                    "emit({'type': 'iteration_start', 'iteration': 2})",
                    "os.remove('passing.py')",
                    "emit({'type': 'iteration_end', 'iteration': 2})",
                ]
            )
            + "\n"
        )
        result = self.run_fake_agent(body)

        self.assertEqual(result["reason"], "process_exited")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["iterations"], 2)
        self.assertEqual(len(result["snapshots"]), 2)
        self.assertEqual([s["iteration"] for s in result["snapshots"]], [1, 2])
        for entry in result["snapshots"]:
            self.assertTrue(Path(entry["path"]).is_dir())
            self.assertGreaterEqual(entry["files"], 1)

        snap1 = Path(result["snapshots"][0]["path"])
        snap2 = Path(result["snapshots"][1]["path"])
        self.assertEqual(snap1, self.snapshot_dir / "iter-1")
        self.assertEqual(snap2, self.snapshot_dir / "iter-2")
        self.assertEqual((snap1 / "passing.py").read_text(), "VALUE = 1\n")
        self.assertFalse((snap2 / "passing.py").exists())
        self.assertFalse((self.agent_cwd / "passing.py").exists())

    def test_iteration_budget_takes_no_snapshots(self):
        body = (
            "\n".join(
                [
                    "import json, time",
                    "for i in range(1, 6):",
                    "    print(json.dumps({'event': {'type': 'iteration_start',",
                    "                      'iteration': i}}), flush=True)",
                    "time.sleep(10)",
                ]
            )
            + "\n"
        )
        result = self.run_fake_agent(body)

        self.assertEqual(result["reason"], "iteration_budget")
        self.assertEqual(result["iterations"], 5)
        self.assertEqual(result["snapshots"], [])
        self.assertFalse(self.snapshot_dir.exists() and any(self.snapshot_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
