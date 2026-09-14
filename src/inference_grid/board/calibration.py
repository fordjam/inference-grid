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
from pathlib import Path, PurePosixPath

from .guard import check_input, check_name
from .branch_review import _plan_task, _safe_rel, _write_files
from .runner import parse_review

# Task-id charset: ids are `calib-<run_id>-<case>`, so both parts are constrained.
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
# The file blocks of a unified diff, on both sides of each rename pair.
DIFF_PATH = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)

AUTHOR_FAMILY = "calibration"
SEVERITY_WEIGHTS = {"high": 3, "medium": 2, "low": 1}
DEFECT_KEYS = {"id", "file", "must_mention", "severity", "note"}


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
    for case_dir in sorted(p for p in corpus_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
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


def score_calibration(board_dir, run_id, packets_root, record=False, ledger=None):
    """Compare each calibration task's settled reply against its answer key.

    Per lane: cases reviewed, defects total, recalled, recall, false positives,
    precision, weighted recall (high=3, medium=2, low=1) and a per-case table. The
    report is written to <board_dir>/calibration/<run_id>/report.json and a markdown
    table is printed. Nothing reaches the ledger unless record=True — then each case's
    attempt gets one `record_outcome` with category "calibration" and accepted = (all
    defects recalled and no false positives).
    """
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

    report = {"run_id": run_id, "lanes": {}}
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
        ["lane", "cases", "defects", "recalled", "recall", "false positives", "precision",
         "weighted recall"],
        printed,
    )
    report["cases"] = _markdown_table(
        ["lane", "case", "verdict", "defects", "recalled", "false positives", "accepted"],
        [
            [r["lane"], r["case"], r["verdict"], r["defects"], r["recalled"],
             r["false_positives"], r["accepted"]]
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
