"""Packaging: the built wheel carries every packaged module and the console entry points.

The wheel is built once per test run with `python -m build --wheel --no-isolation`
(offline: the isolated-build download is exactly what --no-isolation avoids), so this
module needs the `build` extra installed (`pip install -e '.[test]'`) and skips with a
named instruction when it is absent.
"""

import configparser
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

WHEEL = None
ENTRY_POINTS = None


def setUpModule():
    global WHEEL, ENTRY_POINTS
    try:
        import build  # noqa: F401
        import setuptools  # noqa: F401
    except ImportError:
        raise unittest.SkipTest(
            "wheel build tooling unavailable; install the test extra: pip install -e '.[test]'"
        )
    import tempfile

    out = tempfile.mkdtemp(prefix="ig-wheel-")
    # cwd stays away from the repository root: the repo has a `build/` directory that
    # would shadow the build package.
    import subprocess
    import sys

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            out,
            str(REPO),
        ],
        cwd=out,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise AssertionError("wheel build failed: " + done.stderr[-800:])
    wheels = list(Path(out).glob("inference_grid-*.whl"))
    assert wheels, "no wheel produced"
    WHEEL = wheels[0]

    with zipfile.ZipFile(WHEEL) as archive:
        names = archive.namelist()
        parser = configparser.ConfigParser()
        parser.read_string(
            archive.read(next(n for n in names if n.endswith(".dist-info/entry_points.txt"))).decode()
        )
    ENTRY_POINTS = {section: dict(parser[section]) for section in parser.sections()}


class WheelContentsTests(unittest.TestCase):
    def test_wheel_exists(self):
        self.assertTrue(WHEEL is not None and WHEEL.is_file(), str(WHEEL))

    def test_wheel_carries_every_packaged_module(self):
        # Paths in the wheel are relative to src/: inference_grid/… exactly as declared.
        sources = {
            str(path.relative_to(REPO / "src"))
            for path in (REPO / "src" / "inference_grid").rglob("*.py")
            if "__pycache__" not in path.parts
        }
        with zipfile.ZipFile(WHEEL) as archive:
            names = set(archive.namelist())
        missing = sorted(source for source in sources if source not in names)
        self.assertEqual(missing, [], "modules missing from the wheel")

    def test_wheel_carries_the_lane_runner_entry_point(self):
        console = ENTRY_POINTS.get("console_scripts", {})
        self.assertIn("inference-grid", console)
        self.assertIn("inference-grid-lane", console)
        self.assertEqual(console["inference-grid-lane"], "inference_grid.lanes.runner:main")

    def test_wheel_carries_the_capacity_web_assets(self):
        with zipfile.ZipFile(WHEEL) as archive:
            names = {n.rsplit("/", 1)[-1] for n in archive.namelist() if "capacity_web" in n}
        self.assertIn("app.js", names)


if __name__ == "__main__":
    unittest.main()
