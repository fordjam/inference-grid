"""Board runner: one tick dispatches ready tasks to selected lanes and tests what comes back.

A board is a directory of task JSON files (validated by board.task) beside a project. The tick
never edits the project: inputs are copied into a packet, the attempt runs through the ledger on
the packaged lane runner, and the task's tests run against the returned artifacts in a scratch
directory. Results land back in the task file as state changes with recorded reasons.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..flash_window import flash_window
from ..lane_readiness import lane_readiness
from ..ledger import Refused, digest, lanes as lane_records, select
from ..worker import execute
from .guard import check_input
from .task import validate_task
from ..lanes.select import select_lane

RUNNER = [sys.executable, "-m", "inference_grid.lanes.runner"]


def load_board(board_dir):
    tasks = {}
    for path in sorted(Path(board_dir).glob("*.json")):
        task = validate_task(json.loads(path.read_text()))
        if task["id"] != path.stem:
            raise ValueError(f"{path.name}: id must match the file name")
        tasks[task["id"]] = (path, task)
    return tasks


def save_task(path, task, **changes):
    updated = dict(task, **changes)
    validate_task(updated)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(updated, indent=1) + "\n")
    tmp.replace(path)
    return updated


def lane_view(lanes, now):
    """Selection view of lanes.json: category list plus campaign-window state."""
    view = {}
    for lane_id, lane in lanes.items():
        active = None
        if lane["window"] == "zai_flash":
            active = flash_window(now)["active"]
        view[lane_id] = {
            "family": lane["family"],
            "model": lane["model"],
            "categories": lane["categories"],
            "window_active": active,
        }
    return view


def readiness_view(ledger, lanes, now):
    """Lane records classified now; a lane without a record is stale, never ready."""
    with ledger.engine.connect() as con:
        records = {r["provider"]: r["record"] for r in con.execute(select(lane_records)).mappings()}
    view = {}
    for lane_id, lane in lanes.items():
        record = records.get(lane_id)
        if record is None:
            view[lane_id] = {"state": "stale"}
            continue
        try:
            view[lane_id] = {"state": lane_readiness(record, now)["state"]}
        except ValueError:
            view[lane_id] = {"state": "invalid"}
    return view


def stage_packet(project_root, task, packet_dir):
    """Copy task inputs and expected-artifact list into a packet; returns (input_dir, manifest)."""
    input_dir = Path(packet_dir) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name in task["inputs"]:
        source = Path(project_root) / name
        # Lane modules read the prompt from brief.txt whatever the project calls the file.
        target = input_dir / ("brief.txt" if name == task["brief"] else Path(name).name)
        if not source.is_file():
            raise Refused(f"{task['id']}: input missing: {name}")
        problem = check_input(name, source.read_bytes())
        if problem:
            raise Refused(f"{task['id']}: input refused: {name}: {problem}")
        shutil.copy2(source, target)
        manifest[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    expected = input_dir / "expected.json"
    expected.write_text(json.dumps([Path(a).name for a in task["artifacts"]]))
    manifest["expected.json"] = hashlib.sha256(expected.read_bytes()).hexdigest()
    return input_dir, manifest


def run_tests(project_root, task, artifact_dir, scratch):
    """Run the task's tests beside the artifacts in a scratch directory; returns (passed, summary)."""
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    for name in task["inputs"] + task["tests"]:
        source = Path(project_root) / name
        if source.is_file():
            shutil.copy2(source, scratch / Path(name).name)
    for name in task["artifacts"]:
        shutil.copy2(Path(artifact_dir) / Path(name).name, scratch / Path(name).name)
    if not task["tests"]:
        return True, "no tests declared; artifact accepted on presence only"
    modules = [Path(t).stem for t in task["tests"]]
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *modules],
        cwd=scratch,
        capture_output=True,
        text=True,
        timeout=600,
    )
    summary = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
    return proc.returncode == 0, summary[0][:300]


def dispatch(ledger, lanes, lanes_path, lane_id, task, project_root, packet_dir, account_alias):
    """Submit, claim and execute one attempt for the task on the lane; returns (aid, state, output_dir)."""
    lane = lanes[lane_id]
    input_dir, manifest = stage_packet(project_root, task, packet_dir)
    workspace = Path(packet_dir) / "attempts"
    workspace.mkdir(exist_ok=True)
    spec = {
        "authorized": True,
        "model": lane["model"],
        "family": lane["family"],
        "argv": RUNNER + [lane_id, "--config", str(lanes_path)],
        "workspace": str(workspace),
        "timeout": task["budget"]["wall_seconds"] + 40,
        "output_bytes": task["budget"]["output_bytes"],
        "inputs": manifest,
        "input_root": str(input_dir),
        "manifest_sha256": digest(manifest),
    }
    task_id = task["id"] + "-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    ledger.submit(task_id, Path(project_root).name, spec)
    with ledger.engine.connect() as con:
        from ..ledger import accounts, aliases

        binding = con.execute(select(aliases).where(aliases.c.id == account_alias)).mappings().one()
        acct = (
            con.execute(select(accounts).where(accounts.c.id == binding["account"]))
            .mappings()
            .one()
        )
    estimate = {window: 0.01 for window in acct["windows"]}
    aid, generation = ledger.claim(task_id, account_alias, estimate)
    state = execute(ledger, aid, generation)
    return aid, state, workspace / aid / "artifacts"


def tick(
    board_dir, project_root, ledger, lanes, lanes_path, accounts_by_lane, packets_root, now=None
):
    """One pass over ready tasks. Returns a list of {task, lane, attempt, result} records."""
    now = time.time() if now is None else now
    results = []
    view = lane_view(lanes, now)
    readiness = readiness_view(ledger, lanes, now)
    scorecard = ledger.scorecard()
    for task_id, (path, task) in load_board(board_dir).items():
        if task["state"] != "ready":
            continue
        allowed = {k: v for k, v in view.items() if k in task["lanes"]}
        choice = select_lane(
            {"category": task["category"], "author_family": task["author_family"]},
            allowed,
            {k: readiness[k] for k in allowed},
            scorecard,
            now,
        )
        if choice["lane"] is None:
            results.append(
                {"task": task_id, "lane": None, "attempt": None, "result": choice["reason"]}
            )
            continue
        lane_id = choice["lane"]
        packet_dir = Path(packets_root) / task_id / time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
        try:
            aid, state, output_dir = dispatch(
                ledger,
                lanes,
                lanes_path,
                lane_id,
                task,
                project_root,
                packet_dir,
                accounts_by_lane[lane_id],
            )
        except Refused as exc:
            results.append(
                {
                    "task": task_id,
                    "lane": lane_id,
                    "attempt": None,
                    "result": "refused: " + str(exc)[:200],
                }
            )
            continue
        if state != "completed":
            ledger_state = "held"
            save_task(
                path,
                task,
                state="blocked",
                blocked_reason=f"attempt {aid} {state}; resolve with evidence",
            )
            results.append(
                {"task": task_id, "lane": lane_id, "attempt": aid, "result": ledger_state}
            )
            continue
        passed, summary = run_tests(project_root, task, output_dir, packet_dir / "scratch")
        ledger.record_outcome(aid, task["category"], passed, note=summary[:300])
        if passed:
            next_state = "review_pending" if task["author_family"] is None else "passed"
            save_task(path, task, state=next_state, blocked_reason=None)
        else:
            save_task(
                path,
                task,
                state="blocked",
                blocked_reason=f"attempt {aid} failed tests: {summary}"[:300],
            )
        results.append(
            {
                "task": task_id,
                "lane": lane_id,
                "attempt": aid,
                "result": "passed" if passed else "failed_tests",
            }
        )
    return results
