"""02-A5: the ~/.grid-workspaces pruner, entirely against tmp_path fixtures.

Never points at the real ~/.grid-workspaces or the real board database.
"""

import json
import time
import unittest
from pathlib import Path

from inference_grid.ledger import Ledger, attempts

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deployments" / "local"))
import workspace_pruner as pruner  # noqa: E402


def make_ledger(tmp_path):
    url = f"sqlite:///{tmp_path}/board.sqlite"
    ledger = Ledger(url)
    ledger.initialize()
    return ledger, url


def seed_attempt(ledger, id, workspace, state, updated, task=None):
    with ledger.tx() as con:
        con.execute(
            attempts.insert().values(
                id=id,
                task=task or id,
                account="acc",
                generation=1,
                state=state,
                estimate={},
                workspace=str(workspace),
                updated=updated,
            )
        )


def make_dir_with_bytes(path, size):
    """A directory holding one file whose *apparent* size is exactly `size`, via a
    sparse file (seek + single write) so multi-GB fixtures cost no real disk."""
    path.mkdir(parents=True, exist_ok=True)
    target = path / "payload.bin"
    with target.open("wb") as f:
        if size:
            f.seek(size - 1)
            f.write(b"\0")
        else:
            f.truncate(0)


class DirectorySizeTests(unittest.TestCase):
    def test_sums_every_regular_file_recursively(self):
        tmp = Path(self._tmp())
        make_dir_with_bytes(tmp / "a", 100)
        make_dir_with_bytes(tmp / "b" / "nested", 50)
        self.assertEqual(pruner.directory_size(tmp), 150)

    def _tmp(self):
        import tempfile

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class ResolvedWorkspacesTests(unittest.TestCase):
    def test_active_states_are_excluded(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ledger, url = make_ledger(d)
            seed_attempt(ledger, "a1", Path(d) / "ws1", "landed", 1.0)
            seed_attempt(ledger, "a2", Path(d) / "ws2", "queued", 2.0)
            seed_attempt(ledger, "a3", Path(d) / "ws3", "dispatching", 3.0)
            seed_attempt(ledger, "a4", Path(d) / "ws4", "held", 4.0)
            seed_attempt(ledger, "a5", Path(d) / "ws5", "failed", 5.0)
            seed_attempt(ledger, "a6", Path(d) / "ws6", "abandoned", 6.0)
            resolved = pruner.resolved_workspaces(url)
            self.assertEqual(
                set(resolved),
                {str((Path(d) / n).resolve()) for n in ("ws1", "ws5", "ws6")},
            )

    def test_a_workspace_reused_by_two_attempts_keeps_the_later_time(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ledger, url = make_ledger(d)
            ws = Path(d) / "ws1"
            seed_attempt(ledger, "a1", ws, "failed", 10.0)
            seed_attempt(ledger, "a2", ws, "landed", 99.0, task="t2")
            resolved = pruner.resolved_workspaces(url)
            self.assertEqual(resolved[str(ws.resolve())], 99.0)

    def test_a_path_currently_held_by_an_active_attempt_is_never_resolved(self):
        """Workspace paths are reused across attempts (retries share the same clone,
        and board/runner.py's inbox worktree is shared across tasks); a path that is
        both an older attempt's resolved workspace AND a live attempt's current one
        must never show up as safe to delete."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ledger, url = make_ledger(d)
            ws = Path(d) / "ws1"
            seed_attempt(ledger, "a1", ws, "failed", 10.0)
            seed_attempt(ledger, "a2", ws, "dispatching", 99.0, task="t2")
            resolved = pruner.resolved_workspaces(url)
            self.assertNotIn(str(ws.resolve()), resolved)


class PruneTests(unittest.TestCase):
    def test_dry_run_is_the_default_and_never_deletes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            ws = root / "attempt-1"
            make_dir_with_bytes(ws, 5 * pruner.GIGABYTE)
            seed_attempt(ledger, "a1", ws, "landed", 1.0)
            report = pruner.prune(
                root=root, database_url=url, reading_path=Path(d) / "reading.json"
            )
            self.assertTrue(report["dry_run"])
            self.assertTrue(ws.exists(), "dry-run must never delete")
            self.assertEqual(report["deleted"], [])
            self.assertEqual([Path(p) for p in report["would_delete"]], [ws])

    def test_below_cap_does_nothing_even_with_execute(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            ws = root / "attempt-1"
            make_dir_with_bytes(ws, 1024)
            seed_attempt(ledger, "a1", ws, "landed", 1.0)
            report = pruner.prune(
                root=root, database_url=url, dry_run=False, reading_path=Path(d) / "reading.json"
            )
            self.assertTrue(ws.exists())
            self.assertEqual(report["deleted"], [])

    def test_over_cap_deletes_resolved_oldest_first_until_under_cap(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            unit = 2 * pruner.GIGABYTE
            old = root / "attempt-old"
            newer = root / "attempt-newer"
            make_dir_with_bytes(old, unit)
            make_dir_with_bytes(newer, unit)
            seed_attempt(ledger, "a1", old, "landed", 1.0)
            seed_attempt(ledger, "a2", newer, "landed", 2.0)
            report = pruner.prune(
                root=root,
                database_url=url,
                cap_bytes=3 * pruner.GIGABYTE,
                dry_run=False,
                reading_path=Path(d) / "reading.json",
            )
            self.assertFalse(old.exists(), "the older resolved attempt is freed first")
            self.assertTrue(newer.exists(), "freeing one is enough to drop under cap")
            self.assertEqual(report["deleted"], [str(old)])
            self.assertEqual(report["freed_bytes"], unit)

    def test_active_attempts_are_never_deleted_even_under_cap_pressure(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            unit = 5 * pruner.GIGABYTE
            active = root / "attempt-active"
            make_dir_with_bytes(active, unit)
            seed_attempt(ledger, "a1", active, "dispatching", 1.0)
            report = pruner.prune(
                root=root, database_url=url, dry_run=False, reading_path=Path(d) / "reading.json"
            )
            self.assertTrue(active.exists())
            self.assertEqual(report["deleted"], [])
            self.assertEqual(report["would_delete"], [])

    def test_a_reused_workspace_currently_held_active_is_never_deleted(self):
        """End-to-end version of ResolvedWorkspacesTests' reuse case: the same on-disk
        directory backs a resolved older attempt and a live newer one."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            ws = root / "attempt-reused"
            make_dir_with_bytes(ws, 5 * pruner.GIGABYTE)
            seed_attempt(ledger, "a1", ws, "failed", 1.0)
            seed_attempt(ledger, "a2", ws, "dispatching", 2.0, task="t2")
            report = pruner.prune(
                root=root, database_url=url, dry_run=False, reading_path=Path(d) / "reading.json"
            )
            self.assertTrue(ws.exists(), "a live attempt is using this workspace right now")
            self.assertEqual(report["deleted"], [])

    def test_unrecognized_directories_are_left_alone(self):
        """A manual checkout or packets/ dir with no attempts row is never a candidate,
        even when it dwarfs everything the ledger actually resolved."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            orphan = root / "packets"
            make_dir_with_bytes(orphan, 6 * pruner.GIGABYTE)
            report = pruner.prune(
                root=root, database_url=url, dry_run=False, reading_path=Path(d) / "reading.json"
            )
            self.assertTrue(orphan.exists())
            self.assertEqual(report["deleted"], [])

    def test_alarm_file_written_once_the_watermark_is_crossed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            make_dir_with_bytes(root / "attempt-1", 4 * pruner.GIGABYTE)
            alarm_path = Path(d) / "alarms" / "workspace-usage.json"
            # A non-default alarm_bytes: the watermark the file records must reflect
            # what was actually configured here, not the module's ALARM_BYTES constant.
            configured_alarm_bytes = 1 * pruner.GIGABYTE
            report = pruner.prune(
                root=root,
                database_url=url,
                cap_bytes=100 * pruner.GIGABYTE,  # stay under cap: alarm-only path
                alarm_bytes=configured_alarm_bytes,
                alarm_path=alarm_path,
                reading_path=Path(d) / "reading.json",
                dry_run=False,
            )
            self.assertTrue(report["alarm_written"])
            self.assertTrue(alarm_path.exists())
            payload = json.loads(alarm_path.read_text())
            self.assertGreaterEqual(payload["total_bytes"], 4 * pruner.GIGABYTE)
            self.assertEqual(payload["alarm_bytes"], configured_alarm_bytes)

    def test_dry_run_reports_the_alarm_but_never_writes_the_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            make_dir_with_bytes(root / "attempt-1", 4 * pruner.GIGABYTE)
            alarm_path = Path(d) / "alarms" / "workspace-usage.json"
            report = pruner.prune(
                root=root,
                database_url=url,
                cap_bytes=100 * pruner.GIGABYTE,
                alarm_path=alarm_path,
                reading_path=Path(d) / "reading.json",
                dry_run=True,
            )
            self.assertTrue(report["alarm_written"])
            self.assertFalse(alarm_path.exists())

    def test_reading_is_always_written_dry_run_and_below_every_threshold(self):
        """02-A1: the needs-you page reads this file instead of re-walking the tree
        itself on every overlay build -- it must exist even when nothing is alarmed
        and even on a dry run, since a dry run is prune()'s own default."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "grid-workspaces"
            ledger, url = make_ledger(d)
            make_dir_with_bytes(root / "attempt-1", 1024)
            reading_path = Path(d) / "reading.json"
            report = pruner.prune(root=root, database_url=url, reading_path=reading_path)
            self.assertFalse(report["alarm_written"])
            self.assertTrue(reading_path.exists())
            payload = json.loads(reading_path.read_text())
            self.assertEqual(payload["total_bytes"], report["total_bytes"])
            self.assertEqual(payload["cap_bytes"], report["cap_bytes"])
            self.assertIn("written_at", payload)


if __name__ == "__main__":
    unittest.main()
