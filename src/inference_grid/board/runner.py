"""Board runner: one tick dispatches ready tasks to selected lanes and tests what comes back.

A board is a directory of task JSON files (validated by board.task) beside a project. The tick
writes only board-owned state: task files, review staging under <board>/review/<task-id>/ and
generated review briefs beside the board; it never edits project sources. Inputs are copied
into a packet, the attempt runs through the ledger on the packaged lane runner, and the task's
tests run against the returned artifacts in a scratch directory. Results land back in the task
file as state changes with recorded reasons.

A review task (author_family set) passes only on an "approved" verdict in its reply artifact;
a rejected verdict blocks the task with the finding count. A passing work task gets a
review-<id> task created for it; acceptance itself stays an operator action.
"""

import hashlib
import importlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from functools import partial
from pathlib import Path

from ..flash_window import flash_window
from ..lane_readiness import lane_readiness
from ..ledger import ACTIVE, Refused, classifier_view, digest, lanes as lane_records, select
from ..ledger import accounts as account_records, aliases as alias_records
from ..ledger import attempts as attempt_records
from ..ledger import events as ledger_events, tasks as task_records
from ..worker import execute
from .failover import (
    author_failover,
    failover_candidate,
    failed_family,
    pending_failover_note,
)
from .fix_packet import author_fix_plan, fix_reason, recover_fix
from .guard import check_input
from .land import land
from .packet_task import admit_packet, run_packet, validate_board_task
from .plan_task import DRAFTS_FILE, load_drafts, settle_plan
from .policy import owner_only_prefix, record_waiver, review_needed
from .task import validate_task
from .verify_merge import verify_merge
from ..lanes.meta import load_lane_meta
from ..lanes.route import route

RUNNER = [sys.executable, "-m", "inference_grid.lanes.runner"]

# How long a model stays excluded from a lane after the endpoint answered 401/403 for it.
MODEL_REFUSAL_TTL = 24 * 3600

# The tail the ledger appends to a board task id to name an attempt's own task:
# <board id>-<UTC stamp>-<random>, with an optional -verify on the follow-up of a
# deadline hold. The board id is recovered by matching this exact tail against a
# candidate id, because a task id may be a prefix of another (`copy` vs `copy-ok`).
LEDGER_ATTEMPT = re.compile(r"^\d{8}T\d{6}-[0-9a-f]+(?:-verify)?$")


def model_unsupported_until(record, model, now):
    """The instant until which the lane's endpoint has refused this model, else None."""
    meta = record.get("unsupported_until") if isinstance(record, dict) else None
    until = meta.get(model) if isinstance(meta, dict) else meta
    if type(until) in (int, float) and until > now:
        return until
    return None


def load_board(board_dir):
    tasks = {}
    for path in sorted(Path(board_dir).glob("*.json")):
        if path.name == DRAFTS_FILE:
            # The plan node's drafts list is board-owned state, not a task file.
            continue
        task = validate_board_task(json.loads(path.read_text()))
        if task["id"] != path.stem:
            raise ValueError(f"{path.name}: id must match the file name")
        tasks[task["id"]] = (path, task)
    return tasks


def save_task(path, task, **changes):
    updated = dict(task, **changes)
    validate_board_task(updated)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(updated, indent=1) + "\n")
    tmp.replace(path)
    return updated


def duplicate_basenames(task, key):
    """Declared paths in one task list sharing a basename: [(later, earlier)], in order.

    Flat tasks copy by basename, so the later file silently replaces the earlier one in
    the scratch directory; tree tasks keep paths but unittest discovery refuses duplicate
    test-module basenames. Either way, not every declared file can run.
    """
    seen, dupes = {}, []
    for name in task[key]:
        base = Path(name).name
        if base in seen:
            dupes.append((name, seen[base]))
        else:
            seen[base] = name
    return dupes


def shadowing_names(task):
    """Basename collisions between tests and artifacts or staged inputs, if any.

    run_tests copies inputs and tests into the scratch directory and then the artifacts
    over them, so a file sharing a basename with another staged file silently replaces
    it — most dangerously an artifact named like a coordinator test, which swaps the
    acceptance test for provider-authored code. Identical paths listed twice (a test
    that is also a staged input) are harmless; only different files colliding block.

    Duplicate basenames within one declared list block where the silent replacement
    actually happens: all three lists in flat tasks (copies land on one basename), and
    tests in tree tasks (unittest discovery refuses duplicate module basenames). Tree
    inputs and artifacts keep their full paths, so same-basename files there — two
    __init__.py, most commonly — are distinct staged files, not collisions.
    """

    keys = ["tests"] if tree_task(task) else ["tests", "inputs", "artifacts"]
    problems = []
    for key in keys:
        kind = key[:-1]
        for later, earlier in duplicate_basenames(task, key):
            problems.append(
                f"{kind} {later} and {kind} {earlier} share a basename; only one can"
                " run in the scratch directory"
            )

    if tree_task(task):
        # Tree tasks keep full relative paths in the scratch directory, so basenames cannot
        # collide — but an artifact claiming a test's exact path would still replace the
        # coordinator's test with provider-authored code.
        tests = set(task["tests"])
        clashes = [a for a in task["artifacts"] if a in tests]
        if clashes:
            return "artifact would replace the test file at: " + ", ".join(sorted(clashes))
        return "; ".join(problems)

    def by_base(key):
        return {Path(name).name: name for name in task[key]}

    artifacts, tests, inputs = by_base("artifacts"), by_base("tests"), by_base("inputs")
    for base, name in sorted(artifacts.items()):
        if base in tests:
            problems.append(
                f"artifact {name} would replace the test {tests[base]} in the scratch directory"
            )
        if base in inputs:
            problems.append(
                f"artifact {name} would replace the staged input {inputs[base]} "
                "in the scratch directory"
            )
    for base, name in sorted(tests.items()):
        if base in inputs and inputs[base] != name:
            problems.append(
                f"test {name} would replace the staged input {inputs[base]} in the scratch directory"
            )
    return "; ".join(problems)


def parse_review(reply_path):
    """The verdict object from a review artifact reply; ValueError when it is not one.

    Tolerates code fences with or without a newline after the fence marker: a reply of
    exactly ```json{...}``` is malformed output from a model, not a crash.
    """
    text = Path(reply_path).read_text().strip()
    if text.startswith("```"):
        body = text[3:]
        newline = body.find("\n")
        if newline != -1:
            body = body[newline + 1 :]
        else:
            start = body.find("{")
            if start == -1:
                raise ValueError("fenced reply contains no JSON object")
            body = body[start:]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        text = body.strip()
    review = json.loads(text)
    if not isinstance(review, dict) or review.get("verdict") not in ("approved", "rejected"):
        raise ValueError("reply is not a review verdict object")
    return review


def lane_view(lanes, now):
    """Selection view of lanes.json: category list plus campaign-window state.

    First-party families (claude, openai) are explicit_only: the runner never selects
    them for a task whose `lanes` do not name them, so a first-party review lane runs
    only where the author chose it.
    """
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
            "explicit_only": lane["family"] in ("claude", "openai"),
            # The operator's model-tier intent: SOTA for plan and review, workhorses for
            # build. The packaged config's fixed key set cannot carry it (J2 reads it in
            # route), so an absent tier is build.
            "tier": lane.get("tier") if isinstance(lane.get("tier"), str) else "build",
            # Budget-fit caps when the operator's config carries them; the packaged
            # config's fixed key set cannot, and route falls back to the go.py policy.
            "max_tokens": lane.get("max_tokens"),
            "context": lane.get("context"),
        }
    return view


# Accepted canary/qualification rows per category a lane's model needs before the
# category is qualified for it (Q1).
def bare_model(model):
    """The model id without its provider prefix: `cline-pass/kimi-k3` -> `kimi-k3`."""
    return str(model).rsplit("/", 1)[-1] if model else model


CATEGORY_QUALIFICATION = {
    "independent_review": 3,
    "pure_function": 2,
    "tests_multi_file": 2,
    "fixtures_multi_file": 2,
    "canary": 1,
}


def readiness_view(ledger, lanes, now, accounts_by_lane=None, scorecard=None):
    """Lane records classified now; a lane without a record is stale, never ready.

    A lane whose account already carries its max_concurrency of ACTIVE attempts (the
    ledger's own set: queued, dispatching and held — a hold still occupies the account
    slot) is busy: select_lane treats any non-ready state as unavailable, so the tick can
    skip to another lane or report lane_busy instead of colliding with a refused 'account
    busy' dispatch.

    With the scorecard supplied, a lane whose model has no accepted `canary` row is
    unqualified: registering a model costs nothing until a canary earns it evidence.
    The canary is per lane model id as written (it proves the harness); category
    qualification is per bare model — `cline-pass/kimi-k3` and `kimi-k3` are one model
    behind two providers, and the reviews one earned qualify the other.
    """
    with ledger.engine.connect() as con:
        records = {r["provider"]: r["record"] for r in con.execute(select(lane_records)).mappings()}
        alias_map = {r["id"]: r["account"] for r in con.execute(select(alias_records)).mappings()}
        active = {}
        for row in con.execute(
            select(attempt_records.c.account).where(attempt_records.c.state.in_(ACTIVE))
        ).mappings():
            active[row["account"]] = active.get(row["account"], 0) + 1
    accepted_by_model = {}
    if scorecard is not None:
        for row in scorecard:
            # The scorecard already aggregates accepted attempts per (model, category).
            if row.get("accepted"):
                key = (bare_model(row.get("model")), row.get("category"))
                accepted_by_model[key] = accepted_by_model.get(key, 0) + row["accepted"]

    def qualified_for(model):
        return sorted(
            category
            for category, needed in CATEGORY_QUALIFICATION.items()
            if accepted_by_model.get((bare_model(model), category), 0) >= needed
        )

    canaried = (
        {
            row.get("model")
            for row in scorecard
            if row.get("category") == "canary" and row.get("accepted")
        }
        if scorecard is not None
        else None
    )
    view = {}
    for lane_id, lane in lanes.items():
        record = records.get(lane_id)
        if record is None:
            view[lane_id] = {"state": "stale"}
            continue
        try:
            state = lane_readiness(classifier_view(record), now)["state"]
        except ValueError:
            view[lane_id] = {"state": "invalid"}
            continue
        if state == "ready" and model_unsupported_until(record, lane["model"], now):
            # The endpoint has refused this model (401/403) recently: excluded until the
            # recorded instant expires, without bending the provider-authored classifier.
            state = "unqualified"
        if state == "ready" and canaried is not None and lane["model"] not in canaried:
            state = "unqualified"
        if state == "ready" and accounts_by_lane:
            account = alias_map.get(accounts_by_lane.get(lane_id))
            if account is not None and active.get(account, 0) >= lane.get("max_concurrency", 1):
                state = "busy"
        entry = {"state": state, "qualified_for": qualified_for(lane["model"])}
        if state == "unqualified" and model_unsupported_until(record, lane["model"], now):
            # Distinguish a per-model exclusion from a qualification gap: canaries may
            # bootstrap the latter, never the former.
            entry["reason"] = "model_refused_recently"
        view[lane_id] = entry
    return view


def observation_paths(lanes, observations, output_dir):
    """The lane id -> observation file map a tick re-reads stale lane records through.

    The tick config's `observations` map names paths by provider (the collectors write
    `<provider>-observation.json`); a lane id is accepted too, for lane ids that are not
    provider names. A provider the map does not name falls back to the collectors'
    convention `<output_dir>/<provider>-observation.json` when the tick config carries
    `output_dir` — the capacity output directory, where the collectors write.
    """
    paths = {}
    if not observations and not output_dir:
        return paths
    for lane_id, lane in lanes.items():
        provider = lane.get("provider") or lane_id
        named = (observations or {}).get(provider) or (observations or {}).get(lane_id)
        if named:
            paths[lane_id] = Path(named)
        elif output_dir:
            paths[lane_id] = Path(output_dir) / (str(provider) + "-observation.json")
    return paths


def _board_prepare(package_src):
    """The deployments/local board_prepare module, imported through `package_src`.

    The runner shares board_prepare's observation record builder instead of copying it:
    one observation shape, one policy. None when no source directory is configured or the
    module cannot be imported — the re-read is best-effort by design, never a tick failure.
    """
    if not package_src:
        return None
    try:
        source = str(package_src)
        if source not in sys.path:
            sys.path.insert(0, source)
        return importlib.import_module("board_prepare")
    except Exception:  # noqa: BLE001 — a missing deployment module is not a tick failure
        return None


def refresh_stale_lanes(
    ledger,
    lanes,
    readiness,
    observations,
    now,
    package_src=None,
    scorecard=None,
    accounts_by_lane=None,
):
    """Re-read stale lane records from their providers' observation files before refusing.

    board_prepare refreshes the ledger once per pass and a pass can last an hour, so a
    lane record ages past its freshness window while the collector's observation file
    behind it is still fresh. Before a stale record refuses a dispatch, the runner re-reads
    the provider's observation file through board_prepare's record builder (imported
    through `package_src`, never copied), records the fresh lane record in the ledger and
    re-classifies. A file that is itself older than the freshness window admits nothing:
    its age becomes the lane's readiness reason and the tick refuses with it. A lane with
    no observation path, a missing or unreadable file, a non-ok reading, or no
    `package_src` configured behaves exactly as before.

    Returns (readiness, stale_files): the re-classified view and, per lane refused on its
    observation file's age, the reason naming the age.
    """
    stale_files = {}
    if not observations:
        return readiness, stale_files
    prepare = _board_prepare(package_src)
    if prepare is None:
        return readiness, stale_files
    refreshed = False
    for lane_id, entry in readiness.items():
        if entry.get("state") != "stale":
            continue
        record = None
        with ledger.engine.connect() as con:
            row = (
                con.execute(select(lane_records).where(lane_records.c.provider == lane_id))
                .mappings()
                .first()
            )
            if row is not None:
                record = row["record"]
        if not isinstance(record, dict):
            continue
        observed_at = record.get("quota_observed_at")
        valid = record.get("quota_freshness_seconds")
        if (
            isinstance(observed_at, bool)
            or not isinstance(observed_at, (int, float))
            or isinstance(valid, bool)
            or not isinstance(valid, (int, float))
            or valid <= 0
            or now - observed_at <= valid
        ):
            continue
        path = observations.get(lane_id)
        if path is None:
            continue
        try:
            obs = json.loads(path.read_text())
            fresh, observed, _used = prepare.observation_record({}, obs, None, valid)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if observed is None or fresh.get("quota_observed_at") is None:
            continue
        if observed + valid > now:
            merged = dict(fresh, provider=lane_id)
            if isinstance(record.get("unsupported_until"), dict):
                merged["unsupported_until"] = record["unsupported_until"]
            try:
                ledger.record_lane(lane_id, merged)
            except Refused:
                continue
            refreshed = True
        else:
            stale_files[lane_id] = (
                f"quota stale: observation file {int(now - observed)}s old"
                f" (freshness window {int(valid)}s)"
            )
    if refreshed:
        readiness = readiness_view(ledger, lanes, now, accounts_by_lane, scorecard)
    for lane_id, reason in stale_files.items():
        readiness[lane_id] = dict(readiness.get(lane_id) or {"state": "stale"}, reason=reason)
    return readiness, stale_files


def calibration_reports(ledger):
    """Per (family, model) aggregate of the ledger's calibration/eval outcomes.

    score_calibration records one outcome per case, category `eval:review` (the legacy
    `calibration` still counts; board/calibration/score.py) and accepted = all defects
    recalled and no false positives, so a lane's acceptance rate over those outcomes is
    the strict recall the calibration gate measured. A packet case's `eval:packet`
    outcome counts the same way. route blends that rate into the selection score; the
    rows match a scorecard row's identity minus the category.
    """
    from .calibration import is_calibration_outcome

    with ledger.engine.connect() as con:
        specs = {t["id"]: t["spec"] for t in con.execute(select(task_records)).mappings()}
        rows = list(con.execute(select(attempt_records)).mappings())
        outcomes = {}
        for e in con.execute(
            select(ledger_events).where(ledger_events.c.kind == "outcome_recorded")
        ).mappings():
            detail = e["detail"]
            if isinstance(detail, dict) and is_calibration_outcome(detail.get("category")):
                outcomes[e["attempt"]] = detail
    agg = {}
    for row in rows:
        detail = outcomes.get(row["id"])
        if detail is None:
            continue
        spec = specs.get(row["task"], {})
        key = (spec.get("family", "?"), spec.get("model", "?"))
        entry = agg.setdefault(key, {"family": key[0], "model": key[1], "cases": 0, "accepted": 0})
        entry["cases"] += 1
        entry["accepted"] += bool(detail.get("accepted"))
    return [agg[key] for key in sorted(agg)]


def packet_bytes(project_root, task):
    """Byte size of the packet's staged inputs, the brief among them.

    route estimates the prompt at inputs_bytes / 4 tokens; task validation requires the
    brief in `inputs`, so the estimate already covers it. Inputs missing on disk are
    skipped here — staging refuses them later with the real reason.
    """
    root = Path(project_root)
    total = 0
    for name in task["inputs"]:
        try:
            total += (root / name).stat().st_size
        except OSError:
            continue
    return total


def tree_task(task):
    """Package-shaped work: artifacts live at project-relative paths, tests run by discovery."""
    return any("/" in a for a in task["artifacts"])


def stage_packet(project_root, task, packet_dir):
    """Copy task inputs and expected-artifact list into a packet; returns (input_dir, manifest).

    Flat tasks stage inputs by basename; tree tasks keep every relative path so the packet is a
    faithful slice of the project. The brief is always staged as brief.txt at the root.
    """
    input_dir = Path(packet_dir) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    tree = tree_task(task)
    manifest = {}
    for name in task["inputs"]:
        source = Path(project_root) / name
        if Path(name).name == "expected.json":
            raise Refused(f"{task['id']}: expected.json is reserved for the artifact list")
        if name == task["brief"]:
            relative = "brief.txt"
        else:
            relative = name if tree else Path(name).name
            if relative == "brief.txt":
                # A review stages the original brief beside its own; keep both readable.
                relative = "original-brief.txt"
        if relative in manifest:
            raise Refused(f"{task['id']}: staged input names must not collide: {relative}")
        target = input_dir / relative
        if not source.is_file():
            raise Refused(f"{task['id']}: input missing: {name}")
        problem = check_input(name, source.read_bytes())
        if problem:
            raise Refused(f"{task['id']}: input refused: {name}: {problem}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    expected = input_dir / "expected.json"
    names = task["artifacts"] if tree else [Path(a).name for a in task["artifacts"]]
    expected.write_text(json.dumps(names))
    manifest["expected.json"] = hashlib.sha256(expected.read_bytes()).hexdigest()
    return input_dir, manifest


def run_tests(project_root, task, artifact_dir, scratch):
    """Run the task's tests beside the artifacts in a scratch directory; returns (passed, summary)."""
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    tree = tree_task(task)
    for name in task["inputs"] + task["tests"]:
        source = Path(project_root) / name
        if source.is_file() and name != task["brief"]:
            target = scratch / (name if tree else Path(name).name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in task["artifacts"]:
        relative = name if tree else Path(name).name
        target = scratch / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(artifact_dir) / relative, target)
    if not task["tests"]:
        return True, "no tests declared; artifact accepted on presence only"
    if tree:
        test_dir = str(Path(task["tests"][0]).parent) or "."
        argv = [sys.executable, "-m", "unittest", "discover", "-s", test_dir, "-t", ".", "-v"]
    else:
        argv = [sys.executable, "-m", "unittest", "-v", *[Path(t).stem for t in task["tests"]]]
    proc = subprocess.run(argv, cwd=scratch, capture_output=True, text=True, timeout=600)
    summary = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
    return proc.returncode == 0, summary[0][:300]


def admit(
    ledger,
    lanes,
    lanes_path,
    lane_id,
    task,
    project_root,
    packet_dir,
    account_alias,
    input_dir=None,
):
    """Submit and claim one attempt for the task without running it (J6).

    Admission is the half of `dispatch` that must stay on the pass loop: it is what the
    ledger records, so the next route in the same pass sees this attempt occupying its
    account slot. It returns the admission record `dispatch` executes — a flag saying
    whether the packet loop opens its own lease, the attempt id and generation, and where
    the artifacts will land. Packet tasks admit through `admit_packet` (their own lease,
    their own brief parsing); the rest stage the packet here and claim on the lane account.
    """
    lane = lanes[lane_id]
    if task["category"] == "packet":
        return {
            "packet": True,
            **admit_packet(ledger, lanes, lane_id, task, project_root, packet_dir, account_alias),
        }
    if input_dir is None:
        input_dir, manifest = stage_packet(project_root, task, packet_dir)
    else:
        input_dir = Path(input_dir)
        manifest = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(input_dir.iterdir())
            if p.is_file()
        }
    workspace = Path(packet_dir) / "attempts"
    workspace.mkdir(exist_ok=True)
    spec = {
        "authorized": True,
        "model": lane["model"],
        "family": lane["family"],
        "argv": RUNNER + [lane_id, "--config", str(lanes_path)],
        "workspace": str(workspace),
        "timeout": task["budget"]["wall_seconds"] + 40,
        # The lane transport reads the task's own budget, not just the lane record's.
        "wall_seconds": task["budget"]["wall_seconds"],
        "output_bytes": task["budget"]["output_bytes"],
        "thinking_tokens": task["budget"]["thinking_tokens"],
        "inputs": manifest,
        "input_root": str(input_dir),
        "manifest_sha256": digest(manifest),
    }
    # Ledger task ids are immutable; a timestamp plus a random tail keeps concurrent boards
    # (and shared test databases) from colliding within one second.
    task_id = (
        task["id"]
        + "-"
        + time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time()))
        + "-"
        + uuid.uuid4().hex[:6]
    )
    if input_dir.name == "input-verify":
        task_id += "-verify"
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
    return {
        "packet": False,
        "aid": aid,
        "generation": generation,
        "output_dir": workspace / aid / "artifacts",
    }


def dispatch(
    ledger,
    lanes,
    lanes_path,
    lane_id,
    task,
    project_root,
    packet_dir,
    account_alias,
    input_dir=None,
    admission=None,
):
    """Execute one attempt for the task on the lane; returns (aid, state, output_dir).

    With `admission` (the tick's own admitted attempt, brief J6) it only runs and settles
    what was already claimed; without it, it admits and runs, the way it always did. The
    split lets a tick admit on its pass loop — where the ledger, and so the next route,
    sees the attempt — and run the long part in a worker thread.
    """
    if admission is None:
        admission = admit(
            ledger,
            lanes,
            lanes_path,
            lane_id,
            task,
            project_root,
            packet_dir,
            account_alias,
            input_dir=input_dir,
        )
    if admission["packet"]:
        # The build→gate→re-enter loop: admission happened above, the loop runs here.
        return run_packet(ledger, admission)
    state = execute(ledger, admission["aid"], admission["generation"])
    return admission["aid"], state, admission["output_dir"]


def deadline_hold(ledger, packet_dir, aid):
    """True when a held attempt stopped at its wall deadline rather than a receipt refusal.

    The worker's deadline hold records "Refused: timeout: provider acceptance may be
    ambiguous" in the ledger; lanes that supervise a native process also report
    supervisor reason wall_deadline in the verdict beside the attempt. Any other hold
    (unexpected model, escaping artifact name, crashed adapter) stays held for the
    operator even when the expected files happen to exist.
    """
    reason = ""
    for row in ledger.status():
        if row["id"] == aid:
            reason = row.get("reason") or ""
            break
    if "timeout" in reason:
        return True
    verdict_path = Path(packet_dir) / "attempts" / aid / "verdict.json"
    try:
        verdict = json.loads(verdict_path.read_text())
    except (OSError, ValueError):
        return False
    supervisor = verdict.get("supervisor") if isinstance(verdict, dict) else None
    return isinstance(supervisor, dict) and supervisor.get("reason") == "wall_deadline"


def read_verdict(packet_dir, aid):
    """The verdict.json beside a held attempt, or None when there is none."""
    try:
        verdict = json.loads((Path(packet_dir) / "attempts" / aid / "verdict.json").read_text())
    except (OSError, ValueError):
        return None
    return verdict if isinstance(verdict, dict) else None


def hold_block_reason(aid, verdict, state):
    """The blocked_reason for a held attempt; transport timeouts carry their bounds.

    The operator otherwise has to open verdict.json to learn a hold was a transport
    timeout and which of the task/lane/transport budgets clipped it.
    """
    if verdict is None:
        return None
    refusal = verdict.get("refusal")
    if not isinstance(refusal, str):
        return None
    if refusal.startswith("reasoning_overrun") or refusal.startswith("tool_markup"):
        # The overrun and tool-markup refusals already carry what the operator needs;
        # both are named holds a re-dispatch can answer, not blind-retry candidates.
        return f"attempt {aid} held: {refusal}; resolve with evidence"
    if not refusal.startswith("transport_error"):
        return None

    def bound(value):
        return f"{value} s" if type(value) in (int, float) else "unknown"

    return (
        f"attempt {aid} held: {refusal}"
        f" (transport {bound(verdict.get('transport_timeout'))}"
        f", task {bound(verdict.get('task_wall_seconds'))}"
        f", lane {bound(verdict.get('lane_wall_seconds'))}); resolve with evidence"
    )


def note_model_refusal(ledger, lanes, lane_id, packet_dir, aid):
    """Record unsupported_until when the endpoint answered 401/403 for the lane's model.

    The verdict beside the held attempt is the only evidence read; a region/opt-in 403
    (its own region_optin_required refusal) counts the same: the operator enables the
    hosting opt-in, and until then the model stays excluded without re-probing it. The
    merge keeps the stored record classifier-valid and drops already-expired entries.
    """
    verdict = read_verdict(packet_dir, aid)
    refusal = verdict.get("refusal") if verdict else None
    if not isinstance(refusal, str) or not (
        "endpoint returned HTTP 401" in refusal
        or "endpoint returned HTTP 403" in refusal
        or refusal.startswith("region_optin_required")
    ):
        return
    with ledger.engine.connect() as con:
        row = (
            con.execute(select(lane_records).where(lane_records.c.provider == lane_id))
            .mappings()
            .first()
        )
    if row is None:
        return
    record = dict(row["record"])
    meta = record.get("unsupported_until")
    meta = dict(meta) if isinstance(meta, dict) else {}
    now = time.time()
    meta = {model: until for model, until in meta.items() if until > now}
    meta[lanes[lane_id]["model"]] = now + MODEL_REFUSAL_TTL
    record["unsupported_until"] = meta
    try:
        ledger.record_lane(lane_id, record)
    except Refused:
        # A stale or invalid stored record must not turn a hold into a crash; the
        # operator resolves the hold and the next collector reading repairs the record.
        pass


def verify_followup(task, packet_dir, held_attempt_dir):
    """Build a verify-only packet from a held attempt whose expected files all exist.

    Returns the follow-up input directory, or None when the files are incomplete. This is a
    recorded change of brief (run the tests, fix nothing unless they fail), never a blind retry.
    """
    work = Path(held_attempt_dir) / "work"
    names = task["artifacts"] if tree_task(task) else [Path(a).name for a in task["artifacts"]]
    if not all((work / n).is_file() for n in names):
        return None
    source = Path(packet_dir) / "input"
    verify = Path(packet_dir) / "input-verify"
    if verify.exists():
        shutil.rmtree(verify)
    shutil.copytree(source, verify)
    for n in names:
        (verify / n).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / n, verify / n)
    tests = [Path(t).stem for t in task["tests"] if Path(t).name.startswith("test_")]
    if tree_task(task):
        tests = ["discover -s " + (str(Path(task["tests"][0]).parent) or ".") + " -t ."]
    (verify / "brief.txt").write_text(
        "Work only inside the current directory. The files "
        + ", ".join(names)
        + " already exist here from a previous session; every other file is a read-only reference. "
        + "Run exactly this once: python3 -m unittest -v "
        + " ".join(tests)
        + ". If it passes, do not change any file. If it fails, make the smallest fix and run the "
        + "command once more. Then stop with a one-line summary stating pass or fail. No other commands, no network.\n"
    )
    return verify


REVIEW_BUDGET = {"wall_seconds": 600, "output_bytes": 2000000, "thinking_tokens": 6000}

# A line/size/length citation in a finding's expected or observed text.
SIZE_HINT = re.compile(r"\b(lines?|bytes?|chars?|characters?|size|length)\b", re.IGNORECASE)


def advisory_size_finding(finding):
    """True when a finding's expected and observed texts both cite a line or size budget.

    CONTRIBUTIONS (2026-09-12) records the policy that size hints in a brief are
    advisory, but a literal reviewer enforces them anyway; the advisory-only tag in the
    blocked reason lets the operator see a rejection that rests on nothing else. The tag
    never approves anything — the retry path with the amended brief answers it.
    """
    expected = str(finding.get("expected") or "")
    observed = str(finding.get("observed") or "")
    return bool(SIZE_HINT.search(expected)) and bool(SIZE_HINT.search(observed))


def review_brief_text(task, context=None):
    """The exact text sent to the reviewer: what was asked, what to check, verdict schema.

    Without context (board work tasks) the framing is the original brief the artifact
    was authored against. Branch reviews pass their own context — the git range, the
    commit subjects and where the evidence is staged — in front of the shared rules,
    which end with the mandatory output format either way.
    """
    if context is None:
        artifacts = ", ".join(Path(a).name for a in task["artifacts"])
        context = (
            "The artifact file(s) " + artifacts + " were authored "
            "by one provider lane against the original brief, which is staged here as "
            + (
                "original-brief.txt"
                if Path(task["brief"]).name == "brief.txt"
                else Path(task["brief"]).name
            )
            + ". Review the artifact against that brief only: whether each stated "
            "requirement is met, and defects you can demonstrate by quoting the "
            "artifact next to the brief requirement it violates."
        )
    # Branch-review ids already carry the review- prefix; never double it.
    review_label = task["id"] if task["id"].startswith("review-") else "review-" + task["id"]
    return (
        "You are an independent reviewer for task "
        + review_label
        + ". "
        + context.strip()
        + " Read every staged file first. The coordinator's acceptance tests "
        "are staged too; a difference between them and the artifact's own tests is a finding. "
        "Do not report style preferences or hypothetical concerns; mark judgment calls as "
        "checked and move on. Any line, size or length budget in the original brief is "
        "advisory: it is a hint, not a requirement, and exceeding it is not a finding. "
        'Then decide "approved" if no demonstrated defect changes '
        'behavior, otherwise "rejected". You have no tools; every file you need is in '
        "this message. Do not emit tool calls. "
        "OUTPUT FORMAT, mandatory: the entire reply is one "
        'JSON object {"verdict": "approved" or "rejected", "findings": [{"location": '
        '"<file and function>", "input": "<concrete input>", "expected": "<what the '
        'brief requires>", "observed": "<what the artifact does>"}], "checked": ["<rule '
        'you verified>", ...]}; findings may be empty; rejected requires at least one; '
        "no markdown, no code fence, no text before or after the object.\n"
    )


def create_review_task(board_dir, project_root, task, lane_id, lanes, output_dir):
    """Stage the passing artifacts and write the review-<id> task (tick step 4).

    Returns (review task dict or None, note). An existing review file is never overwritten.
    The artifacts are staged under <board>/review/<task-id>/ so the review packet can carry
    them although they are not project files; acceptance and integration stay operator steps.
    """
    review_id = "review-" + task["id"]
    if len(review_id) > 60:
        return None, "review id would exceed 60 chars"
    board_dir = Path(board_dir)
    review_path = board_dir / (review_id + ".json")
    if review_path.exists():
        return None, "review task already exists"
    try:
        board_rel = board_dir.resolve().relative_to(Path(project_root).resolve())
    except ValueError:
        return None, "board directory is not inside the project"
    grid_rel = board_rel.parent
    schema_test = str(grid_rel / "tests" / "test_review_schema.py")
    if not (Path(project_root) / schema_test).is_file():
        return None, "review schema test missing at " + schema_test
    stage = board_dir / "review" / task["id"]
    stage.mkdir(parents=True, exist_ok=True)
    staged = []
    for name in task["artifacts"]:
        relative = name if tree_task(task) else Path(name).name
        (stage / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(output_dir) / relative, stage / relative)
        staged.append(str(board_rel / "review" / task["id"] / relative))
    brief_rel = str(grid_rel / "briefs" / (review_id + ".txt"))
    brief_source = Path(project_root) / brief_rel
    brief_source.parent.mkdir(parents=True, exist_ok=True)
    brief_source.write_text(review_brief_text(task))
    inputs = [brief_rel, task["brief"], *staged, *task["tests"], schema_test]
    ordered = list(dict.fromkeys(inputs))
    review = {
        "id": review_id,
        "category": "independent_review",
        "brief": brief_rel,
        "inputs": ordered,
        "tests": [schema_test],
        "artifacts": ["reply.txt"],
        "lanes": sorted(lanes),
        "author_family": lanes[lane_id]["family"],
        "budget": dict(REVIEW_BUDGET),
        "state": "ready",
        "blocked_reason": None,
    }
    validate_task(review)
    review_path.write_text(json.dumps(review, indent=1) + "\n")
    return review, "created " + review_id


def write_source_link(board_dir, task, aid, lane_id, lanes, receipt_digest):
    """Sidecar linking a review to the attempt it judges; the task schema itself stays fixed."""
    stage = Path(board_dir) / "review" / task["id"]
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "source.json").write_text(
        json.dumps(
            {
                "task": task["id"],
                "attempt": aid,
                "lane": lane_id,
                "family": lanes[lane_id]["family"],
                "receipt_digest": receipt_digest,
                "artifacts": task["artifacts"],
                "review_task": "review-" + task["id"],
            },
            indent=1,
        )
        + "\n"
    )


def find_source_link(board_dir, review_id):
    """Resolve the source a review task judges, without deriving it from the name alone.

    A retry review (review-x-2 for source x) cannot be found by stripping the review-
    prefix, so the scan matches source.json files that name the review task explicitly.
    Returns (link, link_path); without a named match the legacy review- prefix rule
    applies, which also covers links written before the review_task field existed.
    """
    review_dir = Path(board_dir) / "review"
    if review_dir.is_dir():
        for link_path in sorted(review_dir.glob("*/source.json")):
            try:
                link = json.loads(link_path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(link, dict) and link.get("review_task") == review_id:
                return link, link_path
    legacy = review_dir / review_id[len("review-") :] / "source.json"
    try:
        return json.loads(legacy.read_text()), legacy
    except (OSError, ValueError):
        return None, legacy


def superseded(task):
    """True when a task is closed as the predecessor of a recorded retry (BOARD.md)."""
    return task["state"] == "blocked" and str(task["blocked_reason"]).startswith("superseded:")


def propagate_rejection(board, board_dir, review_id, findings):
    """Block the source task a rejected review refers to, carrying the findings summary.

    Without this the review task blocks alone while the source stays review_pending with
    no evidence attached; the source operator needs the first findings (location —
    observed) where the work itself is tracked.
    """
    if not review_id.startswith("review-"):
        return
    link, _ = find_source_link(board_dir, review_id)
    source_id = (link or {}).get("task") or review_id[len("review-") :]
    source = board.get(source_id)
    if source is None or source[1]["state"] != "review_pending" or superseded(source[1]):
        return
    note = "; ".join(
        f"{finding.get('location', '?')} — {finding.get('observed', '?')}"
        for finding in findings[:3]
    )
    save_task(
        source[0],
        source[1],
        state="blocked",
        blocked_reason=("review rejected: " + (note or "no findings listed"))[:300],
    )


def receipt_digest_of(ledger, aid):
    for row in ledger.status():
        if row["id"] == aid and row.get("receipt"):
            return digest(row["receipt"])
    return None


def receipt_of(ledger, aid):
    """The settled attempt's receipt, or None. The review policy reads it, never a digest."""
    for row in ledger.status():
        if row["id"] == aid and row.get("receipt"):
            return row["receipt"]
    return None


# One inbox worktree per project is shared by every attempt that settles in this process;
# with attempts concurrent (J6) two acceptances must not stage and commit it at once.
INBOX_LOCK = threading.Lock()


def land_in_inbox(project_root, task, artifact_dir, record):
    """Commit accepted artifacts and their record to the project's grid/inbox branch.

    A separate worktree under ~/.grid-workspaces/inbox keeps the operator's checkout untouched.
    Tree tasks land at their project paths; flat tasks under grid/inbox/<task-id>/. Nothing is
    pushed or merged; returns a short note.
    """
    with INBOX_LOCK:
        return _land_in_inbox(project_root, task, artifact_dir, record)


def _land_in_inbox(project_root, task, artifact_dir, record):
    project_root = Path(project_root)
    if not (project_root / ".git").exists():
        return "project is not a git repository; artifacts left in the packet"
    worktree = Path.home() / ".grid-workspaces" / "inbox" / project_root.name
    git = ["git", "-C", str(project_root)]
    if not worktree.exists():
        branches = subprocess.run(
            git + ["branch", "--list", "grid/inbox"], capture_output=True, text=True
        )
        args = ["worktree", "add", str(worktree)]
        args += ["grid/inbox"] if branches.stdout.strip() else ["-b", "grid/inbox", "HEAD"]
        done = subprocess.run(git + args, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            return "inbox worktree could not be created: " + done.stderr.strip()[:200]
    tree = tree_task(task)
    for name in task["artifacts"]:
        relative = name if tree else "grid/inbox/" + task["id"] + "/" + Path(name).name
        source = Path(artifact_dir) / (name if tree else Path(name).name)
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    record_path = worktree / "grid" / "inbox" / (task["id"] + ".json")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=1) + "\n")
    wt = ["git", "-C", str(worktree)]
    subprocess.run(wt + ["add", "-A"], capture_output=True, timeout=60)
    message = f"grid: accept {task['id']} ({record.get('lane')}, attempt {record.get('attempt')})"
    done = subprocess.run(
        wt + ["commit", "-q", "-m", message], capture_output=True, text=True, timeout=60
    )
    if done.returncode != 0:
        return "inbox commit failed: " + (done.stderr or done.stdout).strip()[:200]
    return "landed on grid/inbox"


def accept_reviewed(board_dir, project_root, ledger, lanes, review_lane, review_task, result):
    """An approved review accepts the source attempt in the ledger and lands it on the inbox."""
    review_id = review_task["id"]
    link, link_path = find_source_link(board_dir, review_id)
    source_id = (link or {}).get("task") or review_id[len("review-") :]
    source_path = Path(board_dir) / (source_id + ".json")
    if link is None or not source_path.is_file():
        return result + "; source link missing, nothing accepted"
    source_task = validate_task(json.loads(source_path.read_text()))
    if superseded(source_task):
        return result + "; source superseded by a recorded retry, nothing accepted"
    reviewer_family = lanes[review_lane]["family"]
    try:
        ledger.accept(link["attempt"], link["receipt_digest"], reviewer_family, "approved")
    except Refused as exc:
        return result + "; accept refused: " + str(exc)[:120]
    packet_artifacts = None
    for candidate in sorted(Path(source_path).parent.glob("review/" + source_id)):
        packet_artifacts = candidate
    record = {
        "task": source_id,
        "attempt": link["attempt"],
        "lane": link["lane"],
        "author_family": link["family"],
        "reviewer_family": reviewer_family,
        "review_task": review_task["id"],
        "receipt_digest": link["receipt_digest"],
    }
    note = land_in_inbox(project_root, source_task, packet_artifacts, record)
    save_task(source_path, source_task, state="accepted", blocked_reason=None)
    return result + "; accepted; " + note


def settle_verify_merge(path, task, project_root, packets_root):
    """Run the verify_merge code node and settle the task passed or blocked.

    No lane, no ledger attempt, no quota. A conflict or a failing gate blocks
    with the conflicting paths, or the first failing gate's tail, as the reason;
    a node that cannot run at all (bad ref, git failure) blocks without
    stopping the tick.
    """
    spec = task["spec"]
    try:
        report = verify_merge(
            project_root,
            spec["branch"],
            spec["target"],
            spec["gates"],
            work_dir=Path(packets_root) / task["id"] / "verify",
        )
    except Exception as exc:
        save_task(path, task, state="blocked", blocked_reason=("verify_merge: " + str(exc))[:300])
        return {"task": task["id"], "lane": None, "attempt": None, "result": "blocked"}
    if not report["mergeable"]:
        passed, reason = (
            False,
            (
                "merge conflicts: " + ", ".join(report["conflicts"])
                if report["conflicts"]
                else "merge did not complete"
            ),
        )
    else:
        failed = [g for g in report["gates"] if not g["ok"]]
        passed = not failed
        reason = (
            (
                f"gate {failed[0]['name']} failed (exit {failed[0]['returncode']}): "
                + failed[0]["tail"].strip()
            )[:300]
            if failed
            else ""
        )
    save_task(
        path,
        task,
        state="passed" if passed else "blocked",
        blocked_reason=None if passed else reason[:300],
    )
    return {
        "task": task["id"],
        "lane": None,
        "attempt": None,
        "result": "passed" if passed else "blocked",
    }


# The eval packet's shape (M5): a bounded build whose hidden grade is the case's reference
# suite, run by score.py after the attempt. The loop's own gate is only the tree's syntax
# plus the commit gate, so no reference test ever reaches the lane's context.
EVAL_BUDGET = {"wall_seconds": 1800, "output_bytes": 2000000, "thinking_tokens": 6000}
EVAL_MAX_ROUNDS = 3
EVAL_PACKET_ID = "E1"
EVAL_ARTIFACT = "report.md"
EVAL_COMPILE_GATE = (
    "import compileall,sys;sys.exit(0 if compileall.compile_dir('.', quiet=1) else 1)"
)


def eval_brief(case, brief_rel):
    """The brief document one eval build runs under: hard rules, heading, the case's brief.

    A packet task's brief is a document the shared prompt machinery cuts by heading
    (lanes/brief.py); the case's own brief.txt is the packet section, and the hard rules
    section is what `hard_rules` requires of every brief.
    """
    return (
        "## 1. Hard rules\n\n"
        "- Work only inside this checkout; commit on its current branch.\n"
        "- Finish with exactly one commit and a clean working tree.\n\n"
        "---\n\n"
        f"#### {EVAL_PACKET_ID}. {case['case']}\n\n" + case["brief"].strip() + "\n"
    )


def materialize_case_repo(case, root, brief_rel, brief):
    """A one-commit git repository of the case's `base/` under root; returns its path.

    A packet case's `base/` is a plain tree; the packet loop builds in a clone of a
    *repository*, so the eval makes one — the base files and the brief committed once, that
    commit the branch point. Only `base/` and the brief are written; the case's hidden
    `reference/` never touches the disk a lane can read.
    """
    repo = Path(root) / "repo"
    if (repo / ".git").exists():
        return repo
    repo.mkdir(parents=True, exist_ok=True)
    for relative, data in case["base"]:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    brief_path = repo / brief_rel
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(brief)
    for argv in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "add", "-A"],
        [
            "git",
            "-C",
            str(repo),
            "-c",
            # A synthetic identity, no address: the commit exists only to be a base ref.
            "user.email=evals",
            "-c",
            "user.name=evals",
            "commit",
            "-q",
            "--no-gpg-sign",
            "-m",
            "eval base",
        ],
    ):
        done = subprocess.run(argv, capture_output=True)
        if done.returncode != 0:
            raise RuntimeError(
                "eval repository: " + done.stderr.decode("utf-8", "replace").strip()[:200]
            )
    return repo


def case_packet_task(case, lane_id, task_id, brief_rel):
    """The in-memory packet task one packet eval builds (M5); validated before dispatch."""
    from .packet_task import validate_packet_task

    return validate_packet_task(
        {
            "id": task_id,
            "category": "packet",
            "brief": brief_rel,
            "inputs": [brief_rel],
            "tests": [],
            "artifacts": [EVAL_ARTIFACT],
            "lanes": [lane_id],
            "author_family": None,
            "budget": dict(EVAL_BUDGET),
            "state": "ready",
            "blocked_reason": None,
            "spec": {
                "brief": brief_rel,
                "packet_id": EVAL_PACKET_ID,
                "gates": [{"name": "compile", "argv": [sys.executable, "-c", EVAL_COMPILE_GATE]}],
                "base": "HEAD",
                "max_rounds": EVAL_MAX_ROUNDS,
            },
        }
    )


def step_eval_case(
    path,
    task,
    board_dir,
    project_root,
    ledger,
    packets_root,
    lanes,
    lanes_path,
    accounts_by_lane,
    dispatch_fn,
):
    """One pass of an M5 single-case eval run: author the case's task, then score it.

    A review case becomes an ordinary independent_review task on its one lane and the run
    waits for it, exactly as the whole-corpus run does (author_calibration). A packet case
    is not a review packet: the run materializes a one-commit repository of the case's
    `base/` and dispatches the build through the runner's own packet path, then grades the
    lane's tree with score.py. Either way the settled attempt gets one `eval:<kind>` outcome
    — the scorecard row the digest and the due-check read — and the run settles `passed`.
    """
    from .calibration import (
        SETTLED_STATES,
        author_review_case,
        case_attempt,
        case_attempt_dir,
        case_task_id,
        load_case,
        record_outcome,
        score,
    )

    spec = task["spec"]
    run_id = spec["run_id"]
    lane_id = spec["lanes"][0]
    case = load_case(Path(spec["corpus_dir"]) / spec["case"])
    task_id = case_task_id(run_id, case["case"])

    def settle(state, result, reason=None, **extra):
        save_task(path, task, state=state, blocked_reason=reason)
        return {"task": task["id"], "lane": None, "attempt": None, "result": result, **extra}

    if case["kind"] == "packet":
        if task["state"] != "ready":
            return settle("blocked", "blocked", "packet eval run left a non-ready state")
        brief_rel = str(Path("grid") / "briefs" / (task_id + ".txt"))
        try:
            repo = materialize_case_repo(
                case,
                Path(board_dir) / "calibration" / run_id / case["case"],
                brief_rel,
                eval_brief(case, brief_rel),
            )
            packet_task = case_packet_task(case, lane_id, task_id, brief_rel)
            packet_dir = (
                Path(packets_root)
                / task_id
                / time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time()))
            )
            aid, state, attempt_dir = (dispatch_fn or dispatch)(
                ledger,
                lanes,
                lanes_path,
                lane_id,
                packet_task,
                repo,
                packet_dir,
                accounts_by_lane[lane_id],
            )
        except Exception as exc:  # noqa: BLE001 — a bad case blocks the run, not the tick
            return settle("blocked", "blocked", ("eval packet: " + str(exc))[:300])
        if state != "completed":
            # A held attempt is ACTIVE: no outcome can attach, so the operator resolves it
            # and the next nightly run picks the case up again.
            return settle(
                "blocked",
                "blocked",
                f"eval packet attempt {aid} {state}; resolve with evidence",
            )
        try:
            record = score(case, attempt_dir)
            record_outcome(ledger, aid, record)
        except Exception as exc:  # noqa: BLE001
            return settle("blocked", "blocked", ("eval packet score: " + str(exc))[:300])
        save_task(path, task, state="passed", blocked_reason=None)
        return {
            "task": task["id"],
            "lane": None,
            "attempt": aid,
            "result": "passed",
            "accepted": record["accepted"],
            "repairs": record["repairs"],
        }

    if task["state"] == "ready":
        try:
            author_review_case(board_dir, project_root, case, run_id, [lane_id])
        except FileExistsError:
            # A crashed tick staged the task but never saved the wait state; the wait below
            # picks it up rather than authoring a second copy.
            pass
        except Exception as exc:  # noqa: BLE001
            return settle("blocked", "blocked", ("eval review: " + str(exc))[:300])
        return settle("review_pending", "authored")
    try:
        case_state = json.loads((Path(board_dir) / (task_id + ".json")).read_text()).get("state")
    except (OSError, ValueError, AttributeError):
        case_state = None
    if case_state not in SETTLED_STATES:
        return {
            "task": task["id"],
            "lane": None,
            "attempt": None,
            "result": f"waiting on {case['case']}",
        }
    aid = case_attempt(ledger, task_id)
    if aid is None:
        return settle(
            "blocked", "blocked", f"eval case {case['case']} settled with no ledger attempt"
        )
    try:
        record = score(case, case_attempt_dir(packets_root, task_id, aid))
        record_outcome(ledger, aid, record)
    except Exception as exc:  # noqa: BLE001
        return settle("blocked", "blocked", ("eval review score: " + str(exc))[:300])
    save_task(path, task, state="passed", blocked_reason=None)
    return {
        "task": task["id"],
        "lane": None,
        "attempt": aid,
        "result": "passed",
        "accepted": record["accepted"],
        "repairs": record["repairs"],
    }


def step_calibration_run(
    path,
    task,
    board_dir,
    project_root,
    ledger,
    packets_root,
    dry_run=False,
    lanes=None,
    lanes_path=None,
    accounts_by_lane=None,
    dispatch_fn=None,
):
    """Drive the calibration_run code node one pass: author, wait, then score and pass.

    No lane, no ledger attempt, no quota. The first pass authors one independent_review
    task per corpus case (author_calibration); later passes wait until every case's review
    task has settled, then score them through score_calibration(record=True) and settle
    the run `passed` with the per-lane recall and precision in the result. A dry run plans
    without authoring or scoring. A corpus that cannot be authored or scored blocks the
    run with the reason, never the tick.

    A run whose spec carries `case` (M5's nightly `evals`) measures one case on one lane
    instead: step_eval_case authors the case's task and scores the settled attempt through
    score.py, recording the `eval:<kind>` outcome. The `lanes`, `lanes_path` and
    `accounts_by_lane` are only needed there (a packet case dispatches its own build).
    """
    from .calibration import author_calibration, calibration_settled, score_calibration

    spec = task["spec"]
    run_id = spec["run_id"]
    if dry_run:
        return {"task": task["id"], "lane": None, "attempt": None, "result": "calibration_run"}
    if spec.get("case") is not None:
        return step_eval_case(
            path,
            task,
            board_dir,
            project_root,
            ledger,
            packets_root,
            lanes or {},
            lanes_path,
            accounts_by_lane or {},
            dispatch_fn,
        )
    if task["state"] == "ready":
        try:
            author_calibration(
                board_dir, project_root, spec["corpus_dir"], list(spec["lanes"]), run_id
            )
        except Exception as exc:  # noqa: BLE001 — a bad corpus blocks the run, not the tick
            save_task(
                path, task, state="blocked", blocked_reason=("calibration: " + str(exc))[:300]
            )
            return {"task": task["id"], "lane": None, "attempt": None, "result": "blocked"}
        save_task(path, task, state="review_pending", blocked_reason=None)
        return {"task": task["id"], "lane": None, "attempt": None, "result": "authored"}
    settled, pending = calibration_settled(board_dir, run_id)
    if not settled:
        return {
            "task": task["id"],
            "lane": None,
            "attempt": None,
            "result": f"waiting on {len(pending)} case(s)",
        }
    try:
        report = score_calibration(board_dir, run_id, packets_root, record=True, ledger=ledger)
    except Exception as exc:  # noqa: BLE001
        save_task(path, task, state="blocked", blocked_reason=("calibration: " + str(exc))[:300])
        return {"task": task["id"], "lane": None, "attempt": None, "result": "blocked"}
    save_task(path, task, state="passed", blocked_reason=None)
    recall = {
        lane: {
            "recall": entry["recall"],
            "precision": entry["precision"],
            "cases": entry["cases"],
        }
        for lane, entry in report["lanes"].items()
    }
    return {
        "task": task["id"],
        "lane": None,
        "attempt": None,
        "result": "passed",
        "reviewer_recall": recall,
    }


def account_slots(ledger, account):
    """Free attempt slots on one account: its capacity minus its ACTIVE attempts (J6).

    Read at admission time, this is the bound a tick admits against. A lane at capacity
    whose capacity is held by this tick's own attempt is not refused: the task waits for
    that attempt to settle and is routed again.
    """
    with ledger.engine.connect() as con:
        acct = (
            con.execute(select(account_records).where(account_records.c.id == account))
            .mappings()
            .first()
        )
        if acct is None:
            return 0
        active = list(
            con.execute(
                select(attempt_records.c.id).where(
                    attempt_records.c.account == account,
                    attempt_records.c.state.in_(ACTIVE),
                )
            )
        )
    return max(0, acct["capacity"] - len(active))


class Attempt:
    """One admitted attempt running in its own thread (J6).

    The pass starts one per dispatch and joins them: an attempt holding the last slot of
    its account is waited on before the next task is admitted, and every attempt is joined
    before the pass returns, so `board-tick` settles everything it started. Settling writes
    the task file and the result row from the worker; the pass only reads the ledger. An
    unexpected failure in a worker — a bug, not a held attempt, which `execute` reports as
    a state — is kept and re-raised on the pass loop when it is joined, exactly as a serial
    pass would have raised it.
    """

    def __init__(self, account, run):
        self.account = account
        self.settled = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._guard, args=(run,), daemon=True)

    def _guard(self, run):
        try:
            run()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the pass loop
            self.error = exc
        finally:
            self.settled.set()

    def start(self):
        self.thread.start()

    def join(self):
        self.thread.join()

    def live(self):
        return not self.settled.is_set()


def ledger_attempts(ledger, task_id):
    """Every ledger attempt for one board task, newest first.

    The ledger names an attempt's task `<board id>-<UTC stamp>-<random>`, so a candidate
    is kept only when that exact tail matches — a bare prefix test would also claim the
    attempts of a task whose id merely starts with this one (`copy` vs `copy-ok`).
    """
    prefix = task_id + "-"
    with ledger.engine.connect() as con:
        rows = list(
            con.execute(
                select(attempt_records)
                .where(attempt_records.c.task.like(prefix + "%"))
                # The last transition orders "newest"; the ledger task id (which carries
                # the stamp, and a -verify tail on a follow-up) breaks a tie the way the
                # admissions ran, so the choice is deterministic.
                .order_by(attempt_records.c.updated.desc(), attempt_records.c.task.desc())
            ).mappings()
        )
    return [r for r in rows if LEDGER_ATTEMPT.match(r["task"][len(prefix) :])]


def requeue_dead(path, task, ledger, dry_run=False):
    """Return a stranded `dispatched` task to `ready`, or report why not (L2).

    A pass marks a task `dispatched` before it admits the attempt and settles the board
    from the worker, so a runtime that dies in between leaves the task stranded: its
    newest ledger attempt is terminal and unsuccessful (`failed`/`abandoned`) and no
    ACTIVE attempt holds it any more, yet `tick` dispatches only `ready` tasks and the
    task would never run again. Requeue it with a ledger event naming the attempt and its
    terminal state; the next pass routes it afresh. A terminal *successful* attempt
    (`completed`/`accepted`) is reported, not touched — the board state is the operator's
    to settle — and a task with a live or held attempt is left entirely alone. A dry run
    reports the requeue it would make and writes nothing.
    """
    rows = ledger_attempts(ledger, task["id"])
    if not rows:
        return None
    newest = rows[0]
    if any(r["state"] in ACTIVE for r in rows):
        return None
    if newest["state"] in ("completed", "accepted"):
        return {
            "task": task["id"],
            "lane": None,
            "attempt": newest["id"],
            "result": "dispatched: attempt " + newest["state"],
        }
    if newest["state"] not in ("failed", "abandoned"):
        return None
    if not dry_run:
        save_task(path, task, state="ready", blocked_reason=None)
        with ledger.tx() as con:
            ledger.event(
                con,
                newest["id"],
                "requeued",
                task=task["id"],
                ledger_task=newest["task"],
                reason="newest attempt " + newest["state"],
            )
    return {"task": task["id"], "lane": None, "attempt": newest["id"], "result": "requeued"}


def tick(
    board_dir,
    project_root,
    ledger,
    lanes,
    lanes_path,
    accounts_by_lane,
    packets_root,
    now=None,
    prepare_argv=None,
    dry_run=False,
    auto_land=False,
    auto_dispatch=False,
    observations=None,
    output_dir=None,
    package_src=None,
    owner_only=None,
    require_lane_meta=None,
):
    """One pass over ready tasks. Returns a list of {task, lane, attempt, result} records.

    Selection goes through lanes.route, which defaults and budget-filters the candidate
    set and hands select_lane a scorecard whose Laplace scores carry the calibration
    blend; every record for a task that reached route also carries the choice's
    `candidates` (rows naming each offered lane and the max_tokens cap it would run
    under), `dropped` (lanes filtered out with reasons and the two token numbers,
    budget_unfit foremost) and `score`.

    With `require_lane_meta` (the board config's hosting/retention requirement,
    {"residency": ["us", "eu"], "retention": ["zero"]}) the sidecar `lanes-meta.json` is
    read once per tick beside `lanes_path` and each lane's record rides its view entry:
    route drops every candidate whose record does not satisfy every listed key (unknown
    never satisfies), reason `lane_policy` naming the key. A malformed sidecar refuses
    the whole tick with the parse error — fail closed — and without the requirement the
    sidecar is read (a malformed one still refuses) but satisfies nothing and nothing
    changes.

    A packet task the plan node authored waits in the board's drafts list (`draft`) until
    the operator releases it or the pass runs with auto_dispatch; the plan task itself
    runs only on a lane marked tier `plan`.

    Admission stays on this loop and the long part runs in a worker thread (brief J6):
    routing and `admit` happen here, so the ledger — and therefore the very next route —
    already counts the attempt just admitted, and each attempt runs in its own thread
    bounded by its account's free capacity as the ledger reports it at admission time.
    The pass joins every worker before it returns (and before it lands anything), so it
    settles exactly the attempts it started, and the rows keep the board's own order no
    matter which attempt finished first.

    With auto_land the pass ends by landing every `passed` packet task through
    board.land (one landing at a time per base, a lock under packets_root); the pass's
    own `passed` settlements included, since the board is re-read afterwards. With
    dry_run the tick stops at route for every ready task — readiness view,
    campaign windows, busy accounts, family exclusion, unsupported_until, budget fit —
    and returns {"readiness": view, "plan": [...]} instead: the plan a real tick would
    follow, without creating an attempt, writing a task file or touching a packet
    directory.

    With `observations` (the tick config's map, provider or lane id -> observation file
    path) or `output_dir` (the collectors' capacity output directory the map defaults
    to), a lane whose ledger record aged past its freshness window is re-read from its
    provider's observation file through board_prepare's record builder (`package_src`
    names the deployments/local directory to import it from) and re-classified before it
    is refused; only an observation file that is itself stale refuses, naming its age.

    With `owner_only` (the board config's path prefixes, `[]` by default) a packet task
    whose declared files fall under one of them is never dispatched: the row names the
    prefix that matched (`owner_only: <prefix>`), the task stays ready, and the digest
    lists it under needs-you. That is the one autonomy wait the tick enforces itself
    (docs/AUTONOMY.md); a dry run plans the refusal the real tick would make.
    """
    now = time.time() if now is None else now
    rows = {}
    attempts = []
    drafted = set(load_drafts(board_dir))
    view = lane_view(lanes, now)
    # The policy sidecar is read once per tick beside lanes.json (brief L8); a malformed
    # file raises here and refuses the whole pass — fail closed — whatever the board
    # requires. Tagged lanes carry their record into route on the view, the way tier does.
    lane_meta = load_lane_meta(lanes_path)
    if lane_meta:
        view = {
            lane_id: dict(lane, lane_meta=dict(lane_meta[lane_id]))
            if lane_id in lane_meta
            else lane
            for lane_id, lane in view.items()
        }
    scorecard = ledger.scorecard()
    calibration = calibration_reports(ledger)
    readiness = readiness_view(ledger, lanes, now, accounts_by_lane, scorecard)
    # A stale lane record is re-read from its provider's observation file before it is
    # allowed to refuse work: the collectors' file is usually fresher than a record a
    # pass-length-old board_prepare wrote (brief L1).
    readiness, stale_files = refresh_stale_lanes(
        ledger,
        lanes,
        readiness,
        observation_paths(lanes, observations, output_dir),
        now,
        package_src=package_src,
        scorecard=scorecard,
        accounts_by_lane=accounts_by_lane,
    )
    board = load_board(board_dir)

    def settle_attempt(index, lane_id, task_id, task, path, choice, packet_dir, admission):
        """Run one admitted attempt and settle its task; the board's row lands in `rows`."""
        aid = None
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
                admission=admission,
            )
            if state != "completed" and deadline_hold(ledger, packet_dir, aid):
                followup = verify_followup(task, packet_dir, Path(packet_dir) / "attempts" / aid)
                if followup is not None:
                    # The one authorized recorded-change retry (BOARD.md): a deadline hold
                    # whose expected files are all present is resolved consumed on that
                    # evidence and followed by exactly one verify-only attempt under a new
                    # ledger task id. Any other hold stays held for the operator.
                    ledger.resolve(
                        aid,
                        "consumed",
                        "deadline with all expected files present; verify-only follow-up dispatched",
                        "board runner",
                    )
                    ledger.record_outcome(
                        aid, task["category"], False, note="deadline; files complete"
                    )
                    if prepare_argv:
                        subprocess.run(prepare_argv, capture_output=True, timeout=120)
                    aid, state, output_dir = dispatch(
                        ledger,
                        lanes,
                        lanes_path,
                        lane_id,
                        task,
                        project_root,
                        packet_dir,
                        accounts_by_lane[lane_id],
                        input_dir=followup,
                    )
        except Refused as exc:
            reason = "refused: " + str(exc)[:200]
            if aid is None:
                # Staging or admission refused before any attempt existed; the task stays ready.
                save_task(path, task, state="ready", blocked_reason=None)
            else:
                save_task(
                    path,
                    task,
                    state="blocked",
                    blocked_reason=("attempt " + aid + " " + reason)[:300],
                )
            rows[index] = {"task": task_id, "lane": lane_id, "attempt": aid, "result": reason}
            return
        if state != "completed":
            # The attempt is held in the ledger; outcomes attach after the operator resolves
            # it, so the board only records the block with the hold reason. A transport
            # timeout names its bounds, and a 401/403 from the endpoint excludes the model
            # from this lane for a day.
            if task["category"] == "packet":
                # The loop already spent its bounded rounds; the verdict names what
                # happened and the branch stays in the attempt directory for the operator.
                verdict = read_verdict(packet_dir, aid) or {}
                detail = (
                    f"packet loop {verdict.get('reason')}"
                    if verdict.get("reason")
                    else f"{state}; resolve with evidence"
                )
                ledger_reason = next(
                    (r.get("reason") for r in ledger.status() if r["id"] == aid), None
                )
                task = save_task(
                    path,
                    task,
                    state="blocked",
                    blocked_reason=(f"attempt {aid} held: {detail}; branch left for the operator")[
                        :300
                    ],
                )
                row = {"task": task_id, "lane": lane_id, "attempt": aid, "result": "held"}
                # M1: a hold the operator owes nothing for drafts its own fix packet —
                # one plan task per (task, reason), whose id makes a second draft a
                # no-op. Authored before the failover so a crash between the two still
                # leaves the plan recorded.
                reason = fix_reason(verdict, task["blocked_reason"], ledger_reason)
                if reason:
                    fix = author_fix_plan(
                        board_dir,
                        project_root,
                        task,
                        verdict,
                        Path(packet_dir) / "attempts" / aid,
                        reason,
                        lanes,
                        note=f"{task['blocked_reason']}; ledger reason: {ledger_reason}",
                    )
                    if fix and fix.get("authored"):
                        row["fix"] = fix["plan"]
                # J4: one retry on a different family, authored here where the failed
                # family is known; the block reason above is what makes it eligible.
                authored = author_failover(
                    board_dir,
                    project_root,
                    ledger,
                    path,
                    task,
                    lanes[lane_id]["family"],
                    lanes,
                    board,
                    texts=(verdict.get("reason"), verdict.get("refusal"), ledger_reason),
                )
                if authored and authored.get("authored"):
                    row["failover"] = authored["task"]
                rows[index] = row
                return
            note_model_refusal(ledger, lanes, lane_id, packet_dir, aid)
            reason = hold_block_reason(aid, read_verdict(packet_dir, aid), state) or (
                f"attempt {aid} {state}; resolve with evidence"
            )
            save_task(path, task, state="blocked", blocked_reason=reason)
            rows[index] = {"task": task_id, "lane": lane_id, "attempt": aid, "result": "held"}
            return
        if task["category"] == "plan":
            # The plan node settles by code: the lane's packet.md becomes a drafted
            # packet task, or the task blocks with the validation error. No review task
            # is spawned for a draft, and no build is dispatched until it is released.
            try:
                created = settle_plan(task, board_dir, project_root, output_dir, view)
            except (OSError, ValueError, FileExistsError) as exc:
                ledger.record_outcome(aid, "plan", False, note=str(exc)[:300])
                save_task(path, task, state="blocked", blocked_reason=("plan: " + str(exc))[:300])
                rows[index] = {
                    "task": task_id,
                    "lane": lane_id,
                    "attempt": aid,
                    "result": "blocked",
                }
                return
            ledger.record_outcome(aid, "plan", True, note=f"drafted {created['id']}")
            save_task(path, task, state="passed", blocked_reason=None)
            rows[index] = {
                "task": task_id,
                "lane": lane_id,
                "attempt": aid,
                "result": "passed",
                "drafted": created["id"],
                "candidates": choice["candidates"],
                "dropped": choice["dropped"],
                "score": choice["score"],
            }
            return
        if task["category"] == "packet":
            # The loop's gates ran as code inside the attempt (its outcome is already
            # recorded with the verdict's repairs); the branch is the deliverable and no
            # review task is spawned for it. When the policy finds the receipt proves the
            # gates, that is the waiver it records — never silently. A packet the lane
            # could not verify still passes here; the branch-scope review, once a
            # verify_merge task proves the merge, is what reads such a branch as a whole.
            needed, reason = review_needed(task, receipt_of(ledger, aid), lanes[lane_id]["family"])
            if not needed:
                record_waiver(board_dir, task, reason, ledger=ledger, attempt=aid)
            save_task(path, task, state="passed", blocked_reason=None)
            rows[index] = {"task": task_id, "lane": lane_id, "attempt": aid, "result": "passed"}
            return
        try:
            passed, summary = run_tests(project_root, task, output_dir, packet_dir / "scratch")
        except (OSError, subprocess.TimeoutExpired) as exc:
            # A harness failure (missing artifact, unreadable test, timeout) must not leave
            # the task ready for a re-dispatch of the same completed attempt, nor stop the
            # rest of the tick.
            summary = ("test harness error: " + str(exc))[:200]
            ledger.record_outcome(aid, task["category"], False, note=summary)
            save_task(
                path,
                task,
                state="blocked",
                blocked_reason=("attempt " + aid + " " + summary)[:300],
            )
            rows[index] = {"task": task_id, "lane": lane_id, "attempt": aid, "result": "test_error"}
            return
        result = "passed" if passed else "failed_tests"
        if passed and task["author_family"] is not None:
            # A review task passes only on an approved verdict; the schema test alone never
            # approves anything.
            try:
                review = parse_review(output_dir / "reply.txt")
            except Exception as exc:
                # Any unreadable reply — missing file, fence without a newline, prose,
                # non-verdict JSON — blocks this review task; it must never abort the tick.
                passed, result = False, "review_unreadable"
                summary = ("review artifact unusable: " + str(exc))[:200]
            else:
                if review["verdict"] != "approved":
                    findings = review.get("findings")
                    findings = (
                        [f for f in findings if isinstance(f, dict)]
                        if isinstance(findings, list)
                        else []
                    )
                    passed = False
                    result = "review_rejected"
                    summary = f"review rejected with {len(findings)} finding(s)"
                    advisory = [i for i, f in enumerate(findings, 1) if advisory_size_finding(f)]
                    if advisory:
                        summary += (
                            " (finding " + ", ".join(map(str, advisory)) + " advisory-only:"
                            " rests on the brief's advisory size hint, not a defect)"
                        )
                    propagate_rejection(board, board_dir, task_id, findings)
        ledger.record_outcome(aid, task["category"], passed, note=(summary or "")[:300])
        if passed:
            if task["author_family"] is None and task["category"] != "canary":
                # A canary is lane evidence, not deliverable work: it passes and stops,
                # no review task is spawned for it. Otherwise the review policy decides:
                # a receipt that proves the gates waives the per-commit review, and the
                # waiver is recorded; everything else keeps the current path.
                needed, reason = review_needed(
                    task, receipt_of(ledger, aid), lanes[lane_id]["family"]
                )
                if needed:
                    next_state = "review_pending"
                    created, note = create_review_task(
                        board_dir, project_root, task, lane_id, lanes, output_dir
                    )
                    write_source_link(
                        board_dir, task, aid, lane_id, lanes, receipt_digest_of(ledger, aid)
                    )
                    if created is None and note != "review task already exists":
                        result = ("passed; review task not created: " + note)[:200]
                else:
                    next_state = "passed"
                    record_waiver(board_dir, task, reason, ledger=ledger, attempt=aid)
            else:
                next_state = "passed"
                if task["id"].startswith("review-"):
                    result = accept_reviewed(
                        board_dir, project_root, ledger, lanes, lane_id, task, result
                    )
            save_task(path, task, state=next_state, blocked_reason=None)
        else:
            if result == "failed_tests":
                summary = "failed tests: " + summary
            save_task(
                path,
                task,
                state="blocked",
                blocked_reason=("attempt " + aid + " " + summary)[:300],
            )
        rows[index] = {
            "task": task_id,
            "lane": lane_id,
            "attempt": aid,
            "result": result,
            "candidates": choice["candidates"],
            "dropped": choice["dropped"],
            "score": choice["score"],
        }

    for index, (task_id, (path, task)) in enumerate(board.items()):
        if task["category"] == "calibration_run" and task["state"] in ("ready", "review_pending"):
            # The calibration run is a code node the runner steps through across passes:
            # author the per-case reviews, wait for them to settle, then score and pass.
            # It is read before the ready gate because a waiting run sits in review_pending.
            rows[index] = step_calibration_run(
                path,
                task,
                board_dir,
                project_root,
                ledger,
                packets_root,
                dry_run,
                lanes=lanes,
                lanes_path=lanes_path,
                accounts_by_lane=accounts_by_lane,
            )
            continue
        if task["category"] == "packet" and task["state"] == "blocked":
            # M1: a packet that settled blocked for a reason the operator owes nothing
            # for drafts its fix packet. The settlement authors it; this recovers one a
            # crashed tick never wrote, or one whose plan lane the operator configured
            # afterwards. The deterministic plan id is the guard, so a task that already
            # has its plan falls through to the failover hook below.
            fix_note = recover_fix(board_dir, project_root, task, lanes, packets_root, dry_run)
            if fix_note is not None:
                rows[index] = {"task": task_id, "lane": None, "attempt": None, "result": fix_note}
                continue
        if failover_candidate(task, board):
            # J4: a packet that settled with an exhaustion or transport reason is due one
            # different-family retry. A dry run names the pending swap without writing;
            # a real tick authors it here (a blocked task never reaches the dispatch
            # path, so settlement-time authoring alone would leave it pending forever).
            if dry_run:
                rows[index] = {
                    "task": task_id,
                    "lane": None,
                    "attempt": None,
                    "result": pending_failover_note(ledger, task, lanes),
                }
                continue
            family = failed_family(ledger, task["blocked_reason"])
            authored = author_failover(
                board_dir, project_root, ledger, path, task, family, lanes, board
            )
            if authored is None:
                continue
            rows[index] = {
                "task": task_id,
                "lane": None,
                "attempt": None,
                "result": (
                    "failover: " + authored["task"]
                    if authored.get("authored")
                    else "failover: " + authored["reason"]
                ),
            }
            continue
        if task["state"] == "dispatched":
            # A task the previous pass marked `dispatched` but never settled — its runtime
            # died, or the operator resolved the attempt without the board catching up —
            # is stranded, since only `ready` tasks dispatch. Requeue the dead ones (L2);
            # a live attempt and a terminal successful one are reported, not moved.
            row = requeue_dead(path, task, ledger, dry_run)
            if row is not None:
                rows[index] = row
            continue
        if task["state"] != "ready":
            continue
        if task["category"] == "verify_merge":
            # The merge proof is a code node in a scratch worktree: no lane, no
            # ledger attempt, no quota. It settles by itself.
            if dry_run:
                rows[index] = {
                    "task": task_id,
                    "lane": None,
                    "attempt": None,
                    "result": "verify_merge",
                }
                continue
            rows[index] = settle_verify_merge(path, task, project_root, packets_root)
            continue
        if task["category"] == "packet" and task_id in drafted and not auto_dispatch:
            # The plan node drafts a packet task and records it here: nothing is
            # dispatched to build until the operator releases it or the board sets
            # auto_dispatch. The default is to draft, not to build.
            rows[index] = {"task": task_id, "lane": None, "attempt": None, "result": "draft"}
            continue
        if task["category"] == "packet":
            prefix = owner_only_prefix(task, owner_only)
            if prefix is not None:
                # The autonomy policy's one dispatch-time wait (brief 14 M3): a packet
                # whose declared files fall under an owner-only prefix is the operator's
                # to release. No attempt exists, nothing is spent, the task stays ready
                # and the row carries the prefix that matched; the digest lists it.
                rows[index] = {
                    "task": task_id,
                    "lane": None,
                    "attempt": None,
                    "result": "owner_only: " + prefix,
                }
                continue
        clash = shadowing_names(task)
        if clash:
            if not dry_run:
                save_task(
                    path,
                    task,
                    state="blocked",
                    blocked_reason=("task file names collide: " + clash)[:300],
                )
            rows[index] = {
                "task": task_id,
                "lane": None,
                "attempt": None,
                "result": "blocked: name collision",
            }
            continue
        # route restricts an explicit `lanes` list itself and defaults an absent/empty
        # one to every lane declaring the category (minus explicit_only families), so
        # the whole view is presented; readiness is classified per lane below either way.
        lanes_view = view
        if task["category"] == "canary":
            # The canary is the only task allowed on a lane still earning its evidence:
            # unverified (auth unknown) and unqualified (qualification pending) lanes are
            # presented as ready for it — but never a lane whose model the endpoint refused
            # recently, which no packet can fix by re-probing. A canary is also implicitly in
            # every lane's categories: registering a lane costs nothing until it earns rows.
            lanes_view = {
                lane_id: dict(lane, categories=list(lane.get("categories") or []) + ["canary"])
                for lane_id, lane in view.items()
            }
        elif task["category"] == "plan":
            # Category plan is declared by the lanes the operator marks tier: plan (J2).
            # Planning on any other tier is refused before selection, and only the
            # plan-tier lanes are offered (with `plan` implicitly among their categories,
            # the way a canary is).
            wrong = sorted(
                lane_id
                for lane_id in task["lanes"]
                if (lanes.get(lane_id) or {}).get("tier") != "plan"
            )
            if wrong:
                rows[index] = {
                    "task": task_id,
                    "lane": None,
                    "attempt": None,
                    "result": "plan_requires_tier_plan: " + ", ".join(wrong),
                }
                continue
            lanes_view = {
                lane_id: dict(lane, categories=list(lane.get("categories") or []) + ["plan"])
                for lane_id, lane in view.items()
                if lane.get("tier") == "plan"
            }

        def route_now(current):
            """route over the readiness view as it stands now (a settled worker moves it)."""
            task_readiness = {}
            for lane_id in view:
                entry = dict(current[lane_id])
                # A packet, like a canary, may run on an unverified or unqualified lane: it
                # names its lanes explicitly, and no packet could ever run to earn the
                # qualification rows in the first place. A recently model-refused lane stays
                # closed — no packet fixes a 401 by re-probing it. A plan task is the same:
                # the lane is named by the operator and no category rows exist to earn.
                if (
                    task["category"] in ("canary", "packet", "plan")
                    and entry.get("state") in ("unverified", "unqualified")
                    and entry.get("reason") != "model_refused_recently"
                ):
                    entry["state"] = "ready"
                if (
                    task["category"] not in ("canary", "packet", "plan")
                    and entry.get("state") == "ready"
                    and task["category"] not in entry.get("qualified_for", [])
                ):
                    entry["state"] = "unqualified"
                    entry["reason"] = "not_qualified_for_category"
                task_readiness[lane_id] = entry
            return route(
                {
                    "category": task["category"],
                    "author_family": task["author_family"],
                    "lanes": list(task["lanes"]),
                    # The budget decides the cap route fits the packet against; without it
                    # every task was measured against the unbudgeted 16 000-token cap.
                    "budget": task.get("budget"),
                    # The board's hosting/retention requirement; route drops candidates
                    # whose lanes-meta.json record does not satisfy it (lane_policy).
                    "require_lane_meta": require_lane_meta,
                },
                lanes_view,
                task_readiness,
                scorecard,
                calibration,
                now,
                packet_bytes(project_root, task),
            )

        choice = route_now(readiness)
        # A lane at capacity only because of an attempt this tick started is not refused:
        # wait for that attempt to settle, re-read the ledger and route again. Capacity the
        # ledger reports as taken by anything else is a real refusal, and route names it.
        # The dry run admits nothing, so it plans from the single readiness view it was given.
        while not dry_run:
            lane_id = choice["lane"]
            account = (accounts_by_lane or {}).get(lane_id) if lane_id is not None else None
            if lane_id is not None and account is not None and account_slots(ledger, account) > 0:
                break
            wanted = {
                (accounts_by_lane or {}).get(c)
                for c in (
                    [lane_id]
                    if lane_id is not None
                    else [c["lane"] for c in choice.get("candidates", [])]
                )
            } - {None, account}
            victim = next((a for a in attempts if a.live() and a.account in wanted), None)
            if victim is None:
                break
            victim.join()
            attempts.remove(victim)
            if victim.error is not None:
                # An unexpected worker failure takes the pass down, as a serial one did.
                raise victim.error
            readiness = readiness_view(ledger, lanes, now, accounts_by_lane, scorecard)
            choice = route_now(readiness)
        if choice["lane"] is None:
            reason = choice["reason"]
            candidates = choice["candidates"]
            if candidates and all(readiness[c["lane"]]["state"] == "busy" for c in candidates):
                # Every remaining candidate is at its concurrency cap; say so instead of
                # the generic no-ready-lane reason.
                reason = "lane_busy"
            else:
                # A candidate refused on its observation file's age names the age, not the
                # generic no-ready-lane reason (brief L1).
                named = next(
                    (stale_files[c["lane"]] for c in candidates if c["lane"] in stale_files),
                    None,
                )
                if named:
                    reason = named
            rows[index] = {
                "task": task_id,
                "lane": None,
                "attempt": None,
                "result": reason,
                "candidates": candidates,
                "dropped": choice["dropped"],
                "score": choice["score"],
            }
            continue
        lane_id = choice["lane"]
        if dry_run:
            # The plan stops here: no dispatched state, no packet, no attempt.
            rows[index] = {
                "task": task_id,
                "lane": lane_id,
                "attempt": None,
                "result": choice["reason"],
                "candidates": choice["candidates"],
                "dropped": choice["dropped"],
                "score": choice["score"],
            }
            continue
        packet_dir = Path(packets_root) / task_id / time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
        if prepare_argv:
            # Refresh account observations right before the claim; a long tick outlives them.
            subprocess.run(prepare_argv, capture_output=True, timeout=120)
        # Mark the task dispatched before any dispatch so an overlapping or later tick can
        # never submit a second attempt for it; a crashed tick leaves this state behind for
        # the operator to resolve together with the ledger attempt.
        save_task(path, task, state="dispatched", blocked_reason=None)
        try:
            admission = admit(
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
            # Refused before any attempt existed: the task stays ready for the next tick.
            readiness = readiness_view(ledger, lanes, now, accounts_by_lane, scorecard)
            reason = "refused: " + str(exc)[:200]
            save_task(path, task, state="ready", blocked_reason=None)
            rows[index] = {"task": task_id, "lane": lane_id, "attempt": None, "result": reason}
            continue
        attempt = Attempt(
            accounts_by_lane[lane_id],
            # partial snapshots this task's values: the loop rebinds them next iteration.
            partial(
                settle_attempt,
                index,
                lane_id,
                task_id,
                task,
                path,
                choice,
                packet_dir,
                admission,
            ),
        )
        attempts.append(attempt)
        attempt.start()
        # The attempt now occupies its account slot (a hold stays ACTIVE), so re-evaluate
        # the busy count before the next task in this tick is selected — dispatching on a
        # stale view collided with a refused 'account busy' attempt. One query.
        readiness = readiness_view(ledger, lanes, now, accounts_by_lane, scorecard)
    for attempt in attempts:
        attempt.join()
    for attempt in attempts:
        if attempt.error is not None:
            # Every attempt is joined first, then the unexpected failure is raised on the
            # pass loop — the caller sees what a serial pass would have raised.
            raise attempt.error
    # Board order, not finish order: the rows a print or a listener reads are the ones a
    # serial pass produced, whatever order the workers settled in.
    ordered = [rows[i] for i in sorted(rows)]
    if auto_land and not dry_run:
        # The board is re-read so the pass's own `passed` settlements land in this pass.
        for task_id, (path, task) in load_board(board_dir).items():
            if task["category"] != "packet" or task["state"] != "passed":
                continue
            try:
                report = land(
                    board_dir, project_root, task_id, packets_root=packets_root, ledger=ledger
                )
            except Exception as exc:
                save_task(path, task, state="blocked", blocked_reason=("land: " + str(exc))[:300])
                ordered.append(
                    {"task": task_id, "lane": None, "attempt": None, "result": "land_error"}
                )
                continue
            ordered.append(
                {
                    "task": task_id,
                    "lane": None,
                    "attempt": None,
                    "result": "landed"
                    if report["landed"]
                    else ("land_refused: " + report["reason"])[:120],
                }
            )
    if dry_run:
        return {
            "readiness": readiness,
            "plan": [
                {
                    "task": r["task"],
                    "lane": r["lane"],
                    "reason": r["result"],
                    "candidates": r.get("candidates", []),
                    "dropped": r.get("dropped", []),
                    "score": r.get("score"),
                }
                for r in ordered
            ],
        }
    return ordered
