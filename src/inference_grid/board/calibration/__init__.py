"""Reviewer and packet calibration: a corpus of eval cases with known answers.

The board routes tasks by an acceptance rate that means "the lane produced a well-formed
result", not "the result was right". A calibration corpus measures the difference, in two
kinds (board/calibration/case.py): a `review` case holds exactly what a review packet
holds (a diff.patch, the changed files, a brief.txt) plus an answer.json naming the
defects a correct reviewer must find (or `clean: true` to measure false positives); a
`packet` case holds a brief and the repository at `base` plus a hidden `reference/` with
the reference fix and the reference test suite. `author_calibration` writes the review
cases onto a board as ordinary independent_review tasks; `score_calibration` compares the
settled replies against the answer keys and reports recall, false positives and weighted
recall per lane. Scoring the two kinds is board/calibration/score.py's `score`, and one
outcome path records both as `eval:<kind>` rows.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .case import (
    CASE_FILE,
    DEFECT_KEYS,
    KINDS,
    NAME,
    SEVERITY_WEIGHTS,
    diff_paths,
    load_case,
    load_corpus,
)
from .score import _score_findings, record_outcome, score

from ..branch_review import _plan_task, _write_files
from ..runner import parse_review
from ..task import validate_task

__all__ = [
    "CASE_FILE",
    "DEFECT_KEYS",
    "KINDS",
    "NAME",
    "SEVERITY_WEIGHTS",
    "author_calibration",
    "calibration_settled",
    "calibration_task",
    "diff_paths",
    "is_calibration_outcome",
    "load_case",
    "load_corpus",
    "newest_calibration_at",
    "record_outcome",
    "score",
    "score_calibration",
    "validate_calibration_run_task",
]

AUTHOR_FAMILY = "calibration"
# A calibration_run is code, not a model call: no lane runs it, no artifacts come back.
RUN_BUDGET = {"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None}
# The per-case review task states that count as settled: approved (passed) or refused (blocked).
SETTLED_STATES = ("passed", "blocked")
# score_calibration records review outcomes as `eval:review` (the pre-kinds corpus used
# the bare `calibration`); both feed the ledger-side calibration aggregate.
EVAL_CATEGORY_PREFIX = "eval:"
LEGACY_CATEGORY = "calibration"


def is_calibration_outcome(category):
    """True for a calibration/eval outcome: `eval:<kind>` or the legacy `calibration`."""
    return isinstance(category, str) and (
        category.startswith(EVAL_CATEGORY_PREFIX) or category == LEGACY_CATEGORY
    )


def author_calibration(board_dir, project_root, corpus_dir, lanes, run_id):
    """Author one independent_review task per review case; answer keys live beside the board.

    Task ids are `calib-<run_id>-<case>`; author_family is the `calibration` sentinel,
    which no lane declares, so select_lane's family exclusion keeps every listed lane
    eligible — the run measures the listed lanes, it does not route around them. The
    case brief travels inside the shared review brief (which carries the mandatory
    verdict format), the staged files and the diff are written through the same
    planning path review_branch uses, and the answer keys plus a manifest land under
    `<board_dir>/calibration/<run_id>/`, outside every task's inputs. A packet case is
    not a review packet: it is listed in the result as `skipped` (authoring it is the
    evals path, M5) rather than staged as one.
    """
    if not isinstance(run_id, str) or not NAME.fullmatch(run_id):
        raise ValueError("run_id must match [a-z0-9-]")
    if not isinstance(lanes, list) or not lanes:
        raise ValueError("calibration needs a non-empty list of lanes")
    cases = load_corpus(corpus_dir)
    review = [case for case in cases if case["kind"] == "review"]
    skipped = [case["case"] for case in cases if case["kind"] != "review"]
    plans, summaries, keys = [], [], []
    for case in review:
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
                "cases": [case["case"] for case in review],
                "tasks": [f"calib-{run_id}-{case['case']}" for case in review],
            },
            indent=1,
        )
        + "\n"
    )
    _write_files([file for plan in plans for file in plan] + keys)
    result = {"tasks": summaries, "manifest": str(manifest)}
    if skipped:
        result["skipped"] = skipped
    return result


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

    score_calibration(record=True) records one eval outcome per settled case; the
    ledger's own event timestamps are the durable state the weekly trigger reads, so a
    run's presence on disk is never mistaken for a scored one.
    """
    from sqlalchemy import select

    from ...ledger import events as event_records

    newest = None
    with ledger.engine.connect() as con:
        rows = con.execute(
            select(event_records).where(event_records.c.kind == "outcome_recorded")
        ).mappings()
        for event in rows:
            detail = event["detail"]
            if isinstance(detail, dict) and is_calibration_outcome(detail.get("category")):
                at = event["at"]
                if isinstance(at, (int, float)) and (newest is None or at > newest):
                    newest = at
    return newest


# --- scoring (K3) ---


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

    from ...ledger import attempts as attempt_records, tasks as task_records

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
    attempt gets one `record_outcome` with category "eval:review" (board/calibration/score.py)
    and accepted = (all defects recalled and no false positives). `scored_at` (ISO 8601
    UTC) is stamped into the report so the overlay can name the newest run per lane.
    """
    now = time.time() if now is None else now
    run_dir = Path(board_dir) / "calibration" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text())
    lanes = manifest.get("lanes") or []
    rows, per_lane = [], {}
    for case, task_id in zip(manifest["cases"], manifest["tasks"]):
        answer = json.loads((run_dir / (case + ".answer.json")).read_text())
        packet = _find_packet(packets_root, task_id)
        attempt_dir = packet[0].parent.parent if packet else None
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
        # One scorer for both kinds: the review record is what score.py returns, and the
        # per-defect ids the table names come from the same primitive.
        case_record = {"case": case, "kind": "review", "answer": answer}
        scored = score(case_record, attempt_dir)
        recalled, findings, false_positives = _score_findings(findings, answer)
        total = len(answer["defects"])
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
            "kind": scored["kind"],
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
            "accepted": scored["accepted"],
            "weighted_recalled": weights(recalled),
            "weighted_total": all_weights,
        }
        if note or scored["notes"]:
            row["note"] = note or scored["notes"]
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
                record_outcome(
                    ledger,
                    row["attempt"],
                    {
                        "kind": row["kind"],
                        "accepted": row["accepted"],
                        "repairs": 0,
                        "notes": f"calibration {run_id}/{row['case']}: {row['recalled']}"
                        f"/{row['defects']} recalled, {row['false_positives']} false positive(s)",
                    },
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
