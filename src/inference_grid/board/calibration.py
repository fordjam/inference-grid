"""Reviewer calibration: a corpus of review packets with known answers.

The board routes independent_review tasks by an acceptance rate that means "the
reviewer produced a well-formed verdict", not "the verdict was right". A calibration
corpus measures the difference: each case is a directory holding exactly what a review
packet holds (a diff.patch, the changed files under their relative paths, a brief.txt)
plus an answer.json that is never staged into the packet, naming the defects a correct
reviewer must find (file, must_mention keywords, severity) or `clean: true` to measure
false positives. `author_calibration` writes the cases onto a board as ordinary
independent_review tasks; `score_calibration` compares the settled replies against the
answer keys and reports recall, false positives and weighted recall per lane.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .guard import check_input, check_name
from .branch_review import _plan_task, _safe_rel, _write_files
from .runner import parse_review
from .task import validate_task

# Task-id charset: ids are `calib-<run_id>-<case>`, so both parts are constrained.
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
# The file blocks of a unified diff, on both sides of each rename pair.
DIFF_PATH = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)

AUTHOR_FAMILY = "calibration"
SEVERITY_WEIGHTS = {"high": 3, "medium": 2, "low": 1}
DEFECT_KEYS = {"id", "file", "must_mention", "severity", "note"}
# A calibration_run is code, not a model call: no lane runs it, no artifacts come back.
RUN_BUDGET = {"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None}
# The per-case review task states that count as settled: approved (passed) or refused (blocked).
SETTLED_STATES = ("passed", "blocked")


def diff_paths(patch):
    """Every path a unified diff touches, both the a/ and b/ side of each block."""
    paths = set()
    for match in DIFF_PATH.finditer(patch):
        paths.update(match.groups())
    return paths


def _validate_answer(case, answer, patch_paths):
    """The answer key must be well-formed and only name files the diff touches."""
    if not isinstance(answer, dict) or set(answer) != {"defects", "clean"}:
        raise ValueError(f"{case}: answer.json must hold exactly defects and clean")
    clean = answer["clean"]
    defects = answer["defects"]
    if not isinstance(clean, bool) or not isinstance(defects, list):
        raise ValueError(f"{case}: answer.json clean must be bool, defects a list")
    if clean and defects:
        raise ValueError(f"{case}: a clean case must have zero defects")
    seen = set()
    for defect in defects:
        if not isinstance(defect, dict) or set(defect) != DEFECT_KEYS:
            raise ValueError(f"{case}: each defect holds exactly {sorted(DEFECT_KEYS)}")
        if not isinstance(defect["id"], str) or not defect["id"] or defect["id"] in seen:
            raise ValueError(f"{case}: defect ids must be distinct non-empty strings")
        seen.add(defect["id"])
        file = defect["file"]
        if not isinstance(file, str) or not _safe_rel(file):
            raise ValueError(f"{case}: defect file must be a safe relative path")
        denied = check_name(file)
        if denied:
            raise ValueError(f"{case}: {file}: {denied}")
        if file not in patch_paths:
            raise ValueError(f"{case}: defect names {file}, which the diff does not touch")
        mentions = defect["must_mention"]
        if (
            not isinstance(mentions, list)
            or not mentions
            or not all(isinstance(m, str) and m for m in mentions)
        ):
            raise ValueError(f"{case}: must_mention must be a non-empty list of strings")
        if defect["severity"] not in SEVERITY_WEIGHTS:
            raise ValueError(f"{case}: severity must be one of {sorted(SEVERITY_WEIGHTS)}")
        if not isinstance(defect["note"], str) or not defect["note"]:
            raise ValueError(f"{case}: defect note must be a non-empty string")


def load_corpus(corpus_dir):
    """Load and validate every case under corpus_dir; returns case records in name order.

    Each case is a directory holding brief.txt, diff.patch and answer.json plus the
    changed files under their relative paths. Everything that would be staged passes the
    board's input guard (credential names and content are refused), the diff must touch
    every file an answer names, and two staged files may not share a basename (packets
    are flat: the runner copies inputs into one scratch directory by basename). The
    answer key itself is validated but never enters the staged file list.
    """
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.is_dir():
        raise ValueError(f"calibration corpus is not a directory: {corpus_dir}")
    cases = []
    for case_dir in sorted(
        p for p in corpus_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        name = case_dir.name
        if not NAME.fullmatch(name):
            raise ValueError(f"{name}: case names must match [a-z0-9-]")
        for required in ("brief.txt", "diff.patch", "answer.json"):
            if not (case_dir / required).is_file():
                raise ValueError(f"{name}: a case holds {required}")
        staged = []
        seen_basenames = set()
        for path in sorted(case_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(case_dir).as_posix()
            if relative in ("answer.json", "brief.txt", "diff.patch"):
                continue
            if not _safe_rel(relative):
                raise ValueError(f"{name}: {relative} is not a safe relative path")
            if PurePosixPath(relative).name in seen_basenames:
                raise ValueError(f"{name}: {relative} shares a basename with another staged file")
            seen_basenames.add(PurePosixPath(relative).name)
            data = path.read_bytes()
            problem = check_input(relative, data)
            if problem:
                raise ValueError(f"{name}: {relative}: {problem}")
            staged.append((relative, data))
        brief = (case_dir / "brief.txt").read_bytes()
        patch = (case_dir / "diff.patch").read_bytes()
        for label, data in (("brief.txt", brief), ("diff.patch", patch)):
            problem = check_input(label, data)
            if problem:
                raise ValueError(f"{name}: {label}: {problem}")
        try:
            answer = json.loads((case_dir / "answer.json").read_text())
        except ValueError as exc:
            raise ValueError(f"{name}: answer.json is not valid JSON: {exc}") from exc
        _validate_answer(name, answer, diff_paths(patch.decode("utf-8", "replace")))
        cases.append(
            {
                "case": name,
                "brief": brief.decode("utf-8"),
                "files": staged,
                "diff": patch.decode("utf-8"),
                "answer": answer,
            }
        )
    if not cases:
        raise ValueError(f"calibration corpus holds no cases: {corpus_dir}")
    return cases


def author_calibration(board_dir, project_root, corpus_dir, lanes, run_id):
    """Author one independent_review task per case; answer keys live beside the board.

    Task ids are `calib-<run_id>-<case>`; author_family is the `calibration` sentinel,
    which no lane declares, so select_lane's family exclusion keeps every listed lane
    eligible — the run measures the listed lanes, it does not route around them. The
    case brief travels inside the shared review brief (which carries the mandatory
    verdict format), the staged files and the diff are written through the same
    planning path review_branch uses, and the answer keys plus a manifest land under
    `<board_dir>/calibration/<run_id>/`, outside every task's inputs.
    """
    if not isinstance(run_id, str) or not NAME.fullmatch(run_id):
        raise ValueError("run_id must match [a-z0-9-]")
    if not isinstance(lanes, list) or not lanes:
        raise ValueError("calibration needs a non-empty list of lanes")
    cases = load_corpus(corpus_dir)
    plans, summaries, keys = [], [], []
    for case in cases:
        task_id = f"calib-{run_id}-{case['case']}"
        if len(task_id) > 60:
            raise ValueError(f"task id would exceed 60 chars: {task_id}")
        staged_bytes = sum(len(data) for _, data in case["files"]) + len(case["diff"].encode())
        files, summary = _plan_task(
            board_dir,
            project_root,
            task_id,
            AUTHOR_FAMILY,
            lanes,
            None,
            case["brief"].strip(),
            case["files"],
            case["diff"],
            staged_bytes,
        )
        plans.append(files)
        summaries.append(dict(summary, case=case["case"]))
        keys.append(
            (
                Path(board_dir) / "calibration" / run_id / (case["case"] + ".answer.json"),
                (json.dumps(case["answer"], indent=1) + "\n").encode(),
            )
        )
    manifest = Path(board_dir) / "calibration" / run_id / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "lanes": lanes,
                "cases": [case["case"] for case in cases],
                "tasks": [f"calib-{run_id}-{case['case']}" for case in cases],
            },
            indent=1,
        )
        + "\n"
    )
    _write_files([file for plan in plans for file in plan] + keys)
    return {"tasks": summaries, "manifest": str(manifest)}


# --- the run as a code node (I3) ---


def validate_calibration_run_task(raw):
    """The calibration_run shape: the standard task keys plus spec {corpus_dir, lanes, run_id}.

    board/task.py is provider-authored (integrated unmodified), so the shared key checks
    run on a copy whose category it accepts. The node is code, not work: it stages nothing
    and produces no artifacts, so `inputs`, `tests` and `artifacts` may be empty — the
    validator fills placeholders only to satisfy the shared checks. Everything specific to
    a calibration run is checked below.
    """

    def err(k, m):
        raise ValueError(k + ": " + m)

    if not isinstance(raw, dict):
        err("task", "expected a dict")
    if raw.get("category") != "calibration_run":
        err("category", "expected calibration_run")
    if "spec" not in raw:
        err("task", "missing spec key")
    core = {k: v for k, v in raw.items() if k != "spec"}
    core["category"] = "pure_function"
    if not isinstance(core.get("brief"), str) or not core["brief"]:
        err("brief", "must be a non-empty str")
    core["inputs"] = [core["brief"]]
    core["artifacts"] = core["artifacts"] or ["report.json"]
    core["lanes"] = core["lanes"] or ["code-node"]
    validate_task(core)
    spec = raw["spec"]
    if not isinstance(spec, dict):
        err("spec", "expected a dict")
    required = {"corpus_dir", "lanes", "run_id"}
    missing = required - set(spec)
    unknown = set(spec) - required
    if missing:
        err("spec", "missing keys: " + ", ".join(sorted(missing)))
    if unknown:
        err("spec", "unknown keys: " + ", ".join(sorted(unknown)))
    if not isinstance(spec["corpus_dir"], str) or not spec["corpus_dir"]:
        err("spec", "corpus_dir must be a non-empty str")
    if not isinstance(spec["run_id"], str) or not NAME.fullmatch(spec["run_id"]):
        err("spec", "run_id must match [a-z0-9-]")
    lanes = spec["lanes"]
    if (
        not isinstance(lanes, list)
        or not lanes
        or not all(isinstance(lane, str) and NAME.fullmatch(lane) for lane in lanes)
    ):
        err("spec", "lanes must be a non-empty list of lane ids")
    out = {
        k: (dict(v) if k == "budget" else list(v) if isinstance(v, list) else v)
        for k, v in raw.items()
        if k != "spec"
    }
    out["spec"] = {
        "corpus_dir": spec["corpus_dir"],
        "lanes": list(lanes),
        "run_id": spec["run_id"],
    }
    return out


def calibration_task(board_dir, corpus_dir, lanes, run_id):
    """Author the one calibration_run board task for run_id; idempotent per run_id.

    The task is a code node: the runner authors the per-case review tasks on the first
    pass and, once they have all settled, scores them and settles the run `passed` with
    the per-lane recall and precision. No lane ever runs it, so it carries no inputs or
    artifacts and the brief is documentation. Re-authoring the same run_id returns the
    existing task instead of refusing: the weekly trigger may fire again before the run
    has been scored.
    """
    if not isinstance(run_id, str) or not NAME.fullmatch(run_id):
        raise ValueError("run_id must match [a-z0-9-]")
    if not isinstance(lanes, list) or not lanes:
        raise ValueError("calibration needs a non-empty list of lanes")
    if not isinstance(corpus_dir, str) or not corpus_dir:
        raise ValueError("corpus_dir must be a non-empty str")
    board_dir = Path(board_dir)
    task_id = "calibration-" + run_id
    if len(task_id) > 60:
        raise ValueError(f"task id would exceed 60 chars: {task_id}")
    task_path = board_dir / (task_id + ".json")
    if task_path.exists():
        return {"id": task_id, "task": str(task_path), "run_id": run_id, "existing": True}
    brief_rel = str(Path("grid") / "briefs" / (task_id + ".txt"))
    task = {
        "id": task_id,
        "category": "calibration_run",
        "brief": brief_rel,
        "inputs": [],
        "tests": [],
        "artifacts": [],
        "lanes": list(dict.fromkeys(lanes)),
        "author_family": None,
        "budget": dict(RUN_BUDGET),
        "state": "ready",
        "blocked_reason": None,
        "spec": {"corpus_dir": corpus_dir, "lanes": list(lanes), "run_id": run_id},
    }
    task = validate_calibration_run_task(task)
    board_dir.mkdir(parents=True, exist_ok=True)
    task_path.write_text(json.dumps(task, indent=1) + "\n")
    brief_path = board_dir.parent.parent / brief_rel
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(
        "Calibration run "
        + run_id
        + " for lanes "
        + ", ".join(lanes)
        + ". This is a code node: it authors one independent_review task per corpus "
        "case, scores them when every case has settled, and settles itself passed with "
        "the per-lane recall and precision. No lane runs it.\n"
    )
    return {
        "id": task_id,
        "task": str(task_path),
        "brief": str(brief_path),
        "run_id": run_id,
        "existing": False,
    }


def calibration_settled(board_dir, run_id):
    """(all_settled, pending): per-case review tasks not yet in a terminal state.

    The manifest author_calibration wrote names every case task; a case is settled when
    its board task reached `passed` (approved) or `blocked` (refused). A missing manifest
    is never "settled" — the run waits rather than scoring a run it cannot enumerate.
    """
    manifest_path = Path(board_dir) / "calibration" / run_id / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return False, [run_id]
    pending = []
    for task_id in manifest.get("tasks") or []:
        try:
            state = json.loads((Path(board_dir) / (task_id + ".json")).read_text()).get("state")
        except (OSError, ValueError, AttributeError):
            state = None
        if state not in SETTLED_STATES:
            pending.append(task_id)
    return not pending, pending


def newest_calibration_at(ledger):
    """The newest instant a calibration case outcome was recorded, or None.

    score_calibration(record=True) records one `calibration` outcome per settled case;
    the ledger's own event timestamps are the durable state the weekly trigger reads, so
    a run's presence on disk is never mistaken for a scored one.
    """
    from sqlalchemy import select

    from ..ledger import events as event_records

    newest = None
    with ledger.engine.connect() as con:
        rows = con.execute(
            select(event_records).where(event_records.c.kind == "outcome_recorded")
        ).mappings()
        for event in rows:
            detail = event["detail"]
            if isinstance(detail, dict) and detail.get("category") == "calibration":
                at = event["at"]
                if isinstance(at, (int, float)) and (newest is None or at > newest):
                    newest = at
    return newest


# --- scoring (K3) ---


def _names_file(location, file):
    """True when a finding's location names the defect's file: the relative path or its
    basename on word boundaries ("src/api/routes/views.py (handle_views)" matches both)."""
    location = str(location).replace("\\", "/").lower()
    file = str(file).lower()
    if file in location:
        return True
    base = re.escape(PurePosixPath(file).name)
    return re.search(r"(?<![a-z0-9._/-])" + base + r"(?![a-z0-9._-])", location) is not None


def _finding_text(finding):
    return " ".join(str(v) for v in finding.values() if isinstance(v, str)).lower()


def _score_findings(findings, answer):
    """Classify findings against one answer key; returns (recalled ids, false positives).

    A defect is recalled when some finding names its file AND the finding's text carries
    every must_mention string case-insensitively. A finding that recalls no defect is a
    false positive — on a clean case every finding is one.
    """
    findings = [f for f in findings if isinstance(f, dict)]
    matched = [False] * len(findings)
    recalled = []
    for defect in answer["defects"]:
        hit = False
        for index, finding in enumerate(findings):
            if _names_file(finding.get("location"), defect["file"]) and all(
                m.lower() in _finding_text(finding) for m in defect["must_mention"]
            ):
                matched[index] = hit = True
        if hit:
            recalled.append(defect["id"])
    return recalled, findings, matched.count(False)


def _find_packet(packets_root, task_id):
    """The settled reviewer reply for a task under packets_root, or None.

    The packet layout is the runner's: <packets_root>/<task-id>/<stamp>/attempts/<aid>/
    artifacts/reply.txt. One attempt per task id is the board's rule; if several exist,
    the latest (highest stamp/aid path) is the settled one.
    """
    root = Path(packets_root) / task_id
    if not root.is_dir():
        return None
    replies = sorted(root.rglob("reply.txt"))
    if not replies:
        return None
    reply = replies[-1]
    aid = reply.parent.parent.name if reply.parent.name == "artifacts" else None
    return reply, aid


def _lane_from_ledger(ledger, aid):
    """The lane id a dispatched attempt ran on, from the ledger task's argv."""
    from sqlalchemy import select

    from ..ledger import attempts as attempt_records, tasks as task_records

    with ledger.engine.connect() as con:
        row = con.execute(
            select(task_records.c.spec)
            .join(attempt_records, attempt_records.c.task == task_records.c.id)
            .where(attempt_records.c.id == aid)
        ).first()
    spec = row[0] if row else None
    argv = spec.get("argv") if isinstance(spec, dict) else None
    return argv[3] if isinstance(argv, list) and len(argv) > 3 else None


def _markdown_table(header, rows):
    head = "| " + " | ".join(header) + " |"
    rule = "|" + "|".join("---" for _ in header) + "|"
    return "\n".join([head, rule] + ["| " + " | ".join(map(str, row)) + " |" for row in rows])


def score_calibration(board_dir, run_id, packets_root, record=False, ledger=None, now=None):
    """Compare each calibration task's settled reply against its answer key.

    Per lane: cases reviewed, defects total, recalled, recall, false positives,
    precision, weighted recall (high=3, medium=2, low=1) and a per-case table. The
    report is written to <board_dir>/calibration/<run_id>/report.json and a markdown
    table is printed. Nothing reaches the ledger unless record=True — then each case's
    attempt gets one `record_outcome` with category "calibration" and accepted = (all
    defects recalled and no false positives). `scored_at` (ISO 8601 UTC) is stamped into
    the report so the overlay can name the newest run per lane.
    """
    now = time.time() if now is None else now
    run_dir = Path(board_dir) / "calibration" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text())
    lanes = manifest.get("lanes") or []
    rows, per_lane = [], {}
    for case, task_id in zip(manifest["cases"], manifest["tasks"]):
        answer = json.loads((run_dir / (case + ".answer.json")).read_text())
        packet = _find_packet(packets_root, task_id)
        verdict, findings, aid, note = None, [], (packet[1] if packet else None), None
        if packet is None:
            note = "no settled reply found"
        else:
            try:
                review = parse_review(packet[0])
                verdict, findings = review["verdict"], review.get("findings")
                if not isinstance(findings, list):
                    findings = []
            except Exception as exc:
                note = f"reply unreadable: {exc}"[:200]
        recalled, findings, false_positives = _score_findings(findings, answer)
        total = len(answer["defects"])
        accepted = packet is not None and len(recalled) == total and false_positives == 0
        if len(lanes) == 1:
            lane = lanes[0]
        elif ledger is not None and aid is not None:
            lane = _lane_from_ledger(ledger, aid) or "unknown"
        else:
            lane = "unknown"
        weights = lambda ids: sum(  # noqa: E731
            SEVERITY_WEIGHTS[d["severity"]] for d in answer["defects"] if d["id"] in ids
        )
        all_weights = weights([d["id"] for d in answer["defects"]])
        row = {
            "case": case,
            "task": task_id,
            "lane": lane,
            "verdict": verdict or ("missing" if packet is None else "unreadable"),
            "attempt": aid,
            "defects": total,
            "recalled": len(recalled),
            "recalled_ids": recalled,
            "missed": [d["id"] for d in answer["defects"] if d["id"] not in recalled],
            "false_positives": false_positives,
            "findings": len(findings),
            "accepted": accepted,
            "weighted_recalled": weights(recalled),
            "weighted_total": all_weights,
        }
        if note:
            row["note"] = note
        rows.append(row)
        per_lane.setdefault(lane, []).append(row)

    report = {
        "run_id": run_id,
        "scored_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "lanes": {},
    }
    printed = []
    for lane, lane_rows in sorted(per_lane.items()):
        defects = sum(r["defects"] for r in lane_rows)
        recalled = sum(r["recalled"] for r in lane_rows)
        findings = sum(r["findings"] for r in lane_rows)
        false_positives = sum(r["false_positives"] for r in lane_rows)
        weighted_recalled = sum(r["weighted_recalled"] for r in lane_rows)
        weighted_total = sum(r["weighted_total"] for r in lane_rows)
        entry = {
            "cases": len(lane_rows),
            "defects": defects,
            "recalled": recalled,
            "recall": (recalled / defects) if defects else None,
            "false_positives": false_positives,
            "findings": findings,
            "precision": ((findings - false_positives) / findings) if findings else None,
            "weighted_recall": (weighted_recalled / weighted_total) if weighted_total else None,
            "cases_detail": lane_rows,
        }
        report["lanes"][lane] = entry
        printed.append(
            [
                lane,
                entry["cases"],
                defects,
                recalled,
                _percent(entry["recall"]),
                false_positives,
                _percent(entry["precision"]),
                _percent(entry["weighted_recall"]),
            ]
        )
    report["table"] = _markdown_table(
        [
            "lane",
            "cases",
            "defects",
            "recalled",
            "recall",
            "false positives",
            "precision",
            "weighted recall",
        ],
        printed,
    )
    report["cases"] = _markdown_table(
        ["lane", "case", "verdict", "defects", "recalled", "false positives", "accepted"],
        [
            [
                r["lane"],
                r["case"],
                r["verdict"],
                r["defects"],
                r["recalled"],
                r["false_positives"],
                r["accepted"],
            ]
            for r in rows
        ],
    )
    if record:
        if ledger is None:
            raise ValueError("record=True needs the ledger")
        for row in rows:
            if row["attempt"] is None:
                continue
            try:
                ledger.record_outcome(
                    row["attempt"],
                    "calibration",
                    row["accepted"],
                    note=f"calibration {run_id}/{row['case']}: {row['recalled']}/{row['defects']}"
                    f" recalled, {row['false_positives']} false positive(s)",
                )
            except Exception as exc:
                row["record_error"] = str(exc)[:200]
    (run_dir / "report.json").write_text(json.dumps(report, indent=1) + "\n")
    print(report["table"])
    print()
    print(report["cases"])
    return report


def _percent(value):
    return "n/a" if value is None else f"{value:.2f}"
