"""A brief becomes board work: `board-new --json {from_brief: <path>, ...}`.

Every repository's backlog lives in a handoff brief; the board holds only what someone
authored by hand, and "is this item done?" is answered differently in each repository.
This module reads every `#### <id>. <title>` packet in the brief document and authors one
`packet` task per item **not already done**, where done is any of the three signals the
swept repositories use:

- a report file matching `docs/reports/*<id-lower>*.md` exists;
- a `docs/CONTRIBUTIONS.md` row begins with `| <id> |`;
- a commit since the brief's own commit (the commit that added the brief file) has a
  subject pairing the id with the brief's number, in one of the shapes seen in this
  repository, yt-research-mcp and factory-frontend: `brief 16, I2`, `(brief 14 A1)` and
  `[handoff-6/B1]` (a colon after the number — `brief 17: J6 — …` — is the first shape
  with another separator). A subject naming the id without the number decides nothing.

Each authored task's brief file carries the umbrella brief's section 1 (`## 1. Hard
rules`, closed by `---`, exactly as `lanes/brief.py::hard_rules` cuts it) plus the item's
own section — the same composition a hand-authored packet brief uses.

Gates: the caller's `gates` list, or the repository's declared test command as one
`tests` gate read from the tick config's `test_argv` (the config in `boards_dir` whose
`board_dir` matches). The commit gate is never authored into the spec: the packet loop
(`board/packet_task.py::_gates_for`) appends it for every packet task at dispatch, so
"the commit gate always" holds without a second commit gate running per round. A board
config that names no `test_argv` refuses, naming the fix.

An item whose board task or brief file already exists is skipped, not authored — a
re-run of the sweep over a grown brief is idempotent. Items judged done are returned
with the evidence that decided them; `dry_run` returns the plan and writes nothing.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..lanes.brief import PACKET_HEADING, hard_rules, packet_text
from .packet_task import validate_packet_task
from .plan_task import PACKET_BUDGET

# Subjects pairing a brief number with a packet id: `brief 16, I2` (also `brief 17: J6`,
# `brief 14 A1` inside parens) and `[handoff-6/B1]`.
BRIEF_SHAPE = re.compile(r"brief\s*(\d+)\s*[:,]?\s*([A-Z]\d{1,3})\b")
HANDOFF_SHAPE = re.compile(r"handoff[-/ ](\d+)\s*[/ ]?([A-Z]\d{1,3})\b")


def run(argv, cwd=None):
    """The git seam: every repository read goes through here, so tests can fake it."""
    done = subprocess.run(argv, capture_output=True, cwd=cwd)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def _git(project_root, *args):
    return run(["git", "-C", str(project_root), *args])


def brief_number(doc: str, path: Path):
    """The brief's own number: the stem's trailing digits, else the title's `brief <n>`."""
    m = re.search(r"(\d+)$", path.stem)
    if m:
        return int(m.group(1))
    m = re.search(r"brief\s+(\d+)", doc.split("\n", 1)[0])
    return int(m.group(1)) if m else None


def report_evidence(project_root: Path, pid: str):
    """The report file that proves the item done, or None."""
    reports = project_root / "docs" / "reports"
    if reports.is_dir():
        for path in sorted(reports.glob(f"*{pid.lower()}*.md")):
            return f"report {path.relative_to(project_root)}"
    return None


def contributions_evidence(project_root: Path, pid: str):
    """The CONTRIBUTIONS row that proves the item done, or None.

    Two row shapes are recognised: the specified one — the row begins `| <id> |` — and
    the variant this repository's own tables write, where the lane names the item first
    (`| GLM-5.3-Flash (J5, branch ...)`).
    """
    contributions = project_root / "docs" / "CONTRIBUTIONS.md"
    if contributions.exists():
        lane_named = re.compile(r"^\|[^|]*\(" + re.escape(pid) + r",")
        for line in contributions.read_text().splitlines():
            if line.startswith(f"| {pid} |") or lane_named.match(line):
                return "CONTRIBUTIONS row for " + pid
    return None


def commit_evidence(project_root: Path, brief_rel: str, number, pid: str):
    """The commit subject that proves the item done, or None.

    Commits since the brief's own commit — the one that added the brief file — whose
    subject pairs the id with the brief's number. An uncommitted brief has no own commit
    and no commit evidence; a brief without a number cannot pair one.
    """
    if number is None:
        return None
    code, out = _git(project_root, "log", "--format=%H", "--diff-filter=A", "--", brief_rel)
    if code != 0:
        return None
    added = [line.strip() for line in out.splitlines() if line.strip()]
    if not added:
        return None
    own = added[-1]
    code, out = _git(project_root, "log", "--format=%h %s", f"{own}..HEAD")
    if code != 0:
        return None
    for line in out.splitlines():
        for shape in (BRIEF_SHAPE, HANDOFF_SHAPE):
            for m in shape.finditer(line):
                if m.group(1) == str(number) and m.group(2) == pid:
                    return "commit " + line.strip()
    return None


def check_rules(doc: str) -> None:
    """Refuse a brief whose rules section cannot be cut: no heading, or no closing `---`.

    `lanes/brief.py::hard_rules` would raise unhelpfully or cut nothing, and the packet
    would dispatch ruleless — the refusal names the line to add.
    """
    start = doc.find("## 1. Hard rules")
    if start == -1:
        raise ValueError(
            "umbrella brief has no '## 1. Hard rules' section; add one so the packet "
            "carries the rules it runs under"
        )
    if "\n---" not in doc[start:]:
        raise ValueError(
            "umbrella brief's section 1 ('## 1. Hard rules') is never closed: add a line "
            "reading --- after the hard-rules section (lanes/brief.py::hard_rules cuts "
            "the rules at the first --- following the heading)"
        )


def composed_brief(doc: str, pid: str) -> str:
    """The task brief: the umbrella's section 1 closed by `---`, then the item's section."""
    check_rules(doc)
    return hard_rules(doc).rstrip() + "\n\n---\n\n" + packet_text(doc, pid)


def default_gates(boards_dir, board_dir):
    """The board config's declared test command as one `tests` gate, or the refusal."""
    if not boards_dir:
        raise ValueError(
            "from_brief needs gates, or a boards_dir whose tick config declares the "
            "repository's test command as test_argv"
        )
    for path in sorted(Path(boards_dir).glob("*.json")):
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(config, dict) or not config.get("board_dir"):
            continue
        if Path(config["board_dir"]) == Path(board_dir):
            argv = config.get("test_argv")
            if isinstance(argv, list) and argv and all(isinstance(x, str) and x for x in argv):
                return [{"name": "tests", "argv": list(argv)}]
            raise ValueError(
                "the board config " + path.name + " names no test_argv: declare the "
                "repository's test command in the tick config or pass gates explicitly"
            )
    raise ValueError(
        "no board config in " + str(boards_dir) + " matches board_dir " + str(board_dir)
    )


def from_brief(
    board_dir,
    project_root,
    from_brief,
    lanes=None,
    gates=None,
    base=None,
    boards_dir=None,
    dry_run=False,
):
    """Author one packet task per not-done item in the brief; returns the sweep's verdict.

    `from_brief` is the brief document's path (absolute, or relative to project_root).
    `lanes` is required — a packet task dispatches only on packet-capable lanes and no
    default can know which ids those are without reading lanes.json. `base` defaults to
    the project's current branch; `gates` to the tick config's test_argv (one `tests`
    gate). Nothing is written when `dry_run`.
    """
    if (
        not isinstance(lanes, list)
        or not lanes
        or not all(isinstance(lane, str) and lane for lane in lanes)
    ):
        raise ValueError(
            "from_brief needs lanes: the packet-capable lane ids the task may dispatch on"
        )
    board_dir = Path(board_dir)
    project_root = Path(project_root)
    doc_path = Path(from_brief)
    if not doc_path.is_absolute():
        doc_path = project_root / doc_path
    doc = doc_path.read_text()
    number = brief_number(doc, doc_path)
    check_rules(doc)
    if base is None:
        code, out = _git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            raise ValueError("from_brief needs base: the project's current branch did not resolve")
        base = out.strip()
    if gates is None:
        gates = default_gates(boards_dir, board_dir)
    if not isinstance(gates, list) or not gates:
        raise ValueError("gates must be a non-empty list of {name, argv} dicts")
    try:
        brief_rel = str(doc_path.relative_to(project_root))
    except ValueError:
        brief_rel = str(doc_path)

    authored, done, skipped, plans = [], [], [], []
    for match in PACKET_HEADING.finditer(doc):
        pid, title = match.group(1), match.group(2)
        evidence = (
            report_evidence(project_root, pid)
            or contributions_evidence(project_root, pid)
            or commit_evidence(project_root, brief_rel, number, pid)
        )
        if evidence:
            done.append({"id": pid, "evidence": evidence})
            continue
        task_file = board_dir / ("packet-" + pid.lower() + ".json")
        brief_file = project_root / "grid" / "briefs" / ("packet-" + pid.lower() + ".txt")
        if task_file.exists():
            skipped.append({"id": pid, "reason": "board task exists: " + str(task_file)})
            continue
        if brief_file.exists():
            skipped.append({"id": pid, "reason": "brief exists without a task: " + str(brief_file)})
            continue
        plans.append((pid, title, task_file, brief_file))

    tasks = []
    for pid, title, task_file, brief_file in plans:
        rel = str(brief_file.relative_to(project_root))
        tasks.append(
            validate_packet_task(
                {
                    "id": "packet-" + pid.lower(),
                    "category": "packet",
                    "brief": rel,
                    "inputs": [rel],
                    "tests": [],
                    "artifacts": ["docs/reports/packet-" + pid.lower() + ".md"],
                    "lanes": list(lanes),
                    "author_family": None,
                    "budget": dict(PACKET_BUDGET),
                    "state": "ready",
                    "blocked_reason": None,
                    "spec": {
                        "brief": rel,
                        "packet_id": pid,
                        "gates": [dict(g) for g in gates],
                        "base": base,
                    },
                }
            )
        )

    if not dry_run:
        for (pid, title, task_file, brief_file), task in zip(plans, tasks):
            board_dir.mkdir(parents=True, exist_ok=True)
            brief_file.parent.mkdir(parents=True, exist_ok=True)
            task_file.write_text(json.dumps(task, indent=1) + "\n")
            brief_file.write_text(composed_brief(doc, pid))
    authored = [
        {
            "id": pid,
            "title": title,
            "task": str(task_file),
            "brief": str(brief_file),
        }
        for pid, title, task_file, brief_file in plans
    ]
    return {
        "brief": str(doc_path),
        "brief_number": number,
        "authored": authored,
        "done": done,
        "skipped": skipped,
        "dry_run": bool(dry_run),
    }
