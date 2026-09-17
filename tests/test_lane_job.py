"""The job file for the driver: validation, path resolution and one process per lane.

`--job` is the second entry to scripts/run_lane.py; these cases pin what the file
means (relative paths vs the file's directory, unknown keys refused, `~` for the
operator's home) and that every entry becomes exactly one spawned driver with its
own `lane-<name>.log`. The spawn is injected, so no lane runs and no socket opens.
"""

import json
import sys
from pathlib import Path

import pytest

from inference_grid.lanes import job

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
try:
    import run_lane
finally:
    sys.path.remove(str(SCRIPT_DIR))


def write_job(tmp_path, **overrides):
    data = {
        "repo": ".",
        "brief": "docs/handoff-glm-15.md",
        "base": "glm/work",
        "python": ".venv/bin/python",
        "packets_root": "packets",
        "lanes": [
            {
                "name": "goat-glm",
                "clone": "clone-a",
                "adapter": "command_code",
                "model": "z-ai/glm-5.3-flash",
                "packets": ["E1", "E2"],
            }
        ],
    }
    data.update(overrides)
    path = tmp_path / "lanes.json"
    path.write_text(json.dumps(data))
    return path


def test_relative_paths_resolve_against_the_file_directory(tmp_path):
    path = write_job(
        tmp_path,
        lanes=[
            {
                "name": "goat-glm",
                "clone": "clone-a",
                "packets": ["E1"],
            },
            {
                "name": "second-glm",
                "clone": "~/lane-c",
                "packets": ["F1"],
            },
        ],
    )
    spec = job.load_job(path)
    assert spec["repo"] == str(tmp_path)
    assert spec["brief"] == str(tmp_path / "docs/handoff-glm-15.md")
    assert spec["python"] == str(tmp_path / ".venv/bin/python")
    assert spec["packets_root"] == str(tmp_path / "packets")
    assert spec["base"] == "glm/work"
    assert spec["database"] is None
    first, second = spec["lanes"]
    assert first["clone"] == str(tmp_path / "clone-a")
    assert first["adapter"] == "command_code"
    assert second["clone"] == str(Path.home() / "lane-c")
    assert second["packets"] == ["F1"]


def test_unknown_and_missing_keys_are_refused(tmp_path):
    with pytest.raises(job.JobError, match="unknown keys: extra"):
        job.load_job(write_job(tmp_path, extra=True))

    base = {
        "name": "goat-glm",
        "clone": "clone-a",
        "packets": ["E1"],
        "surprise": 1,
    }
    with pytest.raises(job.JobError, match="unknown lane keys: surprise"):
        job.load_job(write_job(tmp_path, lanes=[base]))

    data = json.loads(write_job(tmp_path).read_text())
    del data["lanes"]
    (tmp_path / "lanes.json").write_text(json.dumps(data))
    with pytest.raises(job.JobError, match="missing keys: lanes"):
        job.load_job(tmp_path / "lanes.json")

    with pytest.raises(job.JobError, match="non-empty list"):
        job.load_job(write_job(tmp_path, lanes=[]))

    with pytest.raises(job.JobError, match="duplicate lane"):
        job.load_job(
            write_job(
                tmp_path,
                lanes=[
                    {"name": "same", "clone": "a", "packets": ["E1"]},
                    {"name": "same", "clone": "b", "packets": ["E2"]},
                ],
            )
        )

    with pytest.raises(job.JobError, match="must be one of command_code"):
        job.load_job(
            write_job(
                tmp_path,
                lanes=[{"name": "x", "clone": "a", "adapter": "vim", "packets": ["E1"]}],
            )
        )

    with pytest.raises(job.JobError, match="packets"):
        job.load_job(write_job(tmp_path, lanes=[{"name": "x", "clone": "a", "packets": []}]))

    (tmp_path / "lanes.json").write_text("{not json")
    with pytest.raises(job.JobError, match="not JSON"):
        job.load_job(tmp_path / "lanes.json")


def test_lane_argv_carries_the_flags_the_single_lane_driver_takes(tmp_path):
    spec = job.load_job(
        write_job(
            tmp_path,
            database="sqlite:///board.sqlite",
            lanes=[
                {"name": "goat-glm", "clone": "clone-a", "packets": ["E1", "E2"]},
            ],
        )
    )
    script = tmp_path / "run_lane.py"

    def value(argv, flag):
        return argv[argv.index(flag) + 1]

    def packets(argv):
        out, i = [], argv.index("--packets") + 1
        while i < len(argv) and not argv[i].startswith("--"):
            out.append(argv[i])
            i += 1
        return out

    (goat,) = spec["lanes"]
    argv = job.lane_argv(spec, goat, script)
    assert argv[0] == sys.executable and argv[1] == str(script)
    assert value(argv, "--repo") == spec["repo"]
    assert value(argv, "--clone") == goat["clone"]
    assert value(argv, "--brief") == spec["brief"]
    assert value(argv, "--base") == "glm/work"
    assert value(argv, "--lane") == "goat-glm"
    assert value(argv, "--model") == "z-ai/glm-5.3-flash"
    assert value(argv, "--adapter") == "command_code"
    assert value(argv, "--python") == spec["python"]
    assert value(argv, "--packets-root") == spec["packets_root"]
    assert value(argv, "--database") == "sqlite:///board.sqlite"
    assert packets(argv) == ["E1", "E2"]


def test_run_job_launches_one_subprocess_per_entry_with_its_log(tmp_path):
    path = write_job(
        tmp_path,
        lanes=[
            {"name": "goat-glm", "clone": "clone-a", "packets": ["E1", "E2"]},
            {
                "name": "second-glm",
                "clone": "clone-c",
                "packets": ["F1"],
            },
        ],
    )
    calls = []

    def spawn(argv, log_path):
        calls.append((argv, log_path))
        return 0

    rc = job.run_job(path, spawn=spawn, script=tmp_path / "run_lane.py")

    assert rc == 0
    assert len(calls) == 2
    logs = [Path(log) for _, log in calls]
    assert logs == [
        tmp_path / "packets" / "lane-goat-glm.log",
        tmp_path / "packets" / "lane-second-glm.log",
    ]
    assert (tmp_path / "packets").is_dir()
    lanes = [argv[argv.index("--lane") + 1] for argv, _ in calls]
    assert lanes == ["goat-glm", "second-glm"]


def test_run_job_prints_a_table_and_fails_when_a_lane_fails(tmp_path, capsys):
    path = write_job(
        tmp_path,
        lanes=[
            {"name": "goat-glm", "clone": "clone-a", "packets": ["E1"]},
            {"name": "second-glm", "clone": "clone-c", "packets": ["F1"]},
        ],
    )
    codes = {"goat-glm": 0, "second-glm": 3}

    def spawn(argv, log_path):
        return codes[argv[argv.index("--lane") + 1]]

    assert job.run_job(path, spawn=spawn) == 1
    out = capsys.readouterr().out
    assert "lane" in out and "goat-glm" in out and "second-glm" in out
    assert "passed" in out and "failed (3)" in out


def test_run_job_reports_a_lane_that_cannot_start(tmp_path, capsys):
    path = write_job(
        tmp_path,
        lanes=[
            {"name": "goat-glm", "clone": "clone-a", "packets": ["E1"]},
            {"name": "second-glm", "clone": "clone-c", "packets": ["F1"]},
        ],
    )

    def spawn(argv, log_path):
        if argv[argv.index("--lane") + 1] == "second-glm":
            raise OSError("no such interpreter")
        return 0

    assert job.run_job(path, spawn=spawn) == 1
    assert "error: no such interpreter" in capsys.readouterr().out


def test_main_requires_flags_without_a_job_and_refuses_mixing(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        run_lane.main([])
    with pytest.raises(SystemExit):
        run_lane.main(["--job", str(tmp_path / "lanes.json"), "--clone", "x"])

    seen = {}

    def fake_run_job(job_path, script=None):
        seen["path"], seen["script"] = job_path, script
        return 0

    monkeypatch.setattr(run_lane, "run_job", fake_run_job)
    assert run_lane.main(["--job", str(tmp_path / "lanes.json")]) == 0
    assert seen["path"] == str(tmp_path / "lanes.json")
    assert Path(seen["script"]).name == "run_lane.py"
