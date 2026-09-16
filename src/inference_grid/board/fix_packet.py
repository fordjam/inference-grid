"""A held attempt drafts its own fix packet (brief 20, M1).

A `packet` task that settles held or blocked for a reason the operator owes nothing for
is not the operator's to diagnose by hand. The wall deadline, an idle agent, the round
budget, a gate that failed identically in every round, a transport refusal: none of
those say the operator did something wrong — they say something about the packet, its
gates or the harness, and the fix is a change to one of those three.

So the runner authors exactly one `plan` task (the J1 plan node, `board/plan_task.py`)
on a lane the operator marked `tier: plan`, whose ticket is built from the verdict: the
task's own packet section, the last round's gate tails, the last 4 KB of the
transcript, and the question the plan exists to answer — *what change to the packet, the
gates or the harness would let this land?* The plan lane drafts `packet.md`; the plan
node turns that into a `packet` task, which stays a draft until the board sets
`auto_dispatch: true`, and appears under the dashboard's *needs-you* list until then.

Idempotency is the plan task's own id: `fix_plan_id(task, reason)` is derived from the
pair, so the same task settling the same way a second time computes the same id, finds
the file already there and drafts nothing. No plan is drafted twice for the same task +
reason, and there is no sidecar to keep in step with the task files.

`verdict_for` locates the held attempt's verdict under the packets root so the pass loop
can recover a draft a crashed tick never wrote, or one whose plan lane the operator
configured after the failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..lanes.brief import mentioned_paths, packet_text
from .plan_task import plan_task, slug

# The settle reasons the operator owes nothing for. `build_loop` names the first four
# (`agent_idle` is brief 20's L3 idle watchdog); `agent_stopped_early` is the loop's
# own "the session answered without working".
ELIGIBLE_REASONS = (
    "wall_deadline_before_round",
    "wall_deadline",
    "agent_idle",
    "rounds_exhausted",
    "agent_stopped_early",
)
# A gate that ended every round identically and never passed: the packet or the harness
# is wrong, not the operator. Its own label because it can qualify a reason the loop
# named something else.
REPEATED_GATES = "same_gate_repeated"
# A hold whose text is a transport refusal rather than a loop reason: the adapter or the
# endpoint failed, which no packet edit explains to the operator.
TRANSPORT_WORDS = ("transport_error", "timeouterror", "timed out", "connection")
TRANSPORT_REFUSED = "transport_refused"

QUESTION = "What change to the packet, the gates or the harness would let this land?"
TRANSCRIPT_BYTES = 4096
PLAN_PREFIX = "plan-fix-"


def plan_lanes(lanes):
    """The lane ids the operator marked `tier: plan`, sorted: the only lanes that plan."""
    return sorted(
        lane_id
        for lane_id, lane in (lanes or {}).items()
        if isinstance(lane, dict) and lane.get("tier") == "plan"
    )


def fix_plan_id(task_id, reason):
    """The plan task id for one (task, reason) pair; the same pair always yields it.

    The file at this id *is* the record that a plan was already drafted, so a second
    settle of the same task for the same reason drafts nothing — no sidecar to lose.
    """
    return PLAN_PREFIX + slug(f"{task_id}-{reason}", 48)


def reason_from_texts(*texts):
    """The first eligible reason a text names, longest name first.

    `wall_deadline_before_round` contains `wall_deadline`, so the longer name wins.
    """
    for text in texts:
        if not isinstance(text, str):
            continue
        lowered = text.lower()
        for reason in sorted(ELIGIBLE_REASONS, key=len, reverse=True):
            if reason in lowered:
                return reason
    return None


def _marks(results):
    return [
        (r.get("name"), bool(r.get("ok")), r.get("reason"), r.get("returncode"))
        for r in results
        if isinstance(r, dict)
    ]


def same_gate_repeated(verdict):
    """True when every round's gates ended identically and none of them passed.

    `build_loop` compares consecutive rounds for its early-stop rule; a gate that failed
    the same way in every round of the attempt is the other reading of the same evidence:
    no agent round moved it, so no further agent round will.
    """
    rounds = verdict.get("rounds") if isinstance(verdict, dict) else None
    if not isinstance(rounds, list) or len(rounds) < 2:
        return False
    marks = [_marks(r.get("results") or []) for r in rounds if isinstance(r, dict)]
    if len(marks) != len(rounds) or not marks[0]:
        return False
    if any(mark != marks[0] for mark in marks[1:]):
        return False
    return any(not ok for _, ok, _, _ in marks[0])


def _is_transport(*texts):
    return any(
        isinstance(text, str) and any(word in text.lower() for word in TRANSPORT_WORDS)
        for text in texts
    )


def fix_reason(verdict, *texts):
    """The label a fix plan is owed for, or None when the operator owes the work.

    The verdict's own `reason` is authoritative when it carries one; only a hold with no
    verdict (an adapter that raised before the loop wrote one) falls back to the reason
    named in the ledger and board texts.
    """
    verdict = verdict if isinstance(verdict, dict) else {}
    reason = verdict.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = reason_from_texts(*texts)
    if reason in ELIGIBLE_REASONS:
        return reason
    if same_gate_repeated(verdict):
        return REPEATED_GATES
    if _is_transport(*texts):
        return TRANSPORT_REFUSED
    return None


def packet_section(project_root, task):
    """The packet's own section out of its brief, or "" when the brief is unreadable."""
    try:
        brief = (Path(project_root) / task["brief"]).read_text()
        return packet_text(brief, task["spec"]["packet_id"]).strip()
    except (OSError, KeyError, TypeError, ValueError):
        return ""


def last_round_gates(verdict):
    """The final round's gate results as markdown: name, outcome, reason and tail."""
    rounds = verdict.get("rounds") if isinstance(verdict, dict) else None
    results = []
    if isinstance(rounds, list) and rounds and isinstance(rounds[-1], dict):
        results = [r for r in rounds[-1].get("results") or [] if isinstance(r, dict)]
    if not results:
        return "(no gate results were recorded)"
    lines = []
    for result in results:
        exit_note = f", exit {result['returncode']}" if result.get("returncode") is not None else ""
        outcome = "passed" if result.get("ok") else "failed"
        lines += [
            f"### {result.get('name')} — {outcome} ({result.get('reason')}){exit_note}",
            "```",
            (result.get("tail") or "").strip(),
            "```",
            "",
        ]
    return "\n".join(lines).rstrip()


def _round_number(path):
    match = re.search(r"native-(\d+)\.jsonl$", path.name)
    return int(match.group(1)) if match else 0


def transcript_tail(attempt_dir, limit=TRANSCRIPT_BYTES):
    """The last `limit` bytes of the newest round's transcript, or ""."""
    if attempt_dir is None:
        return ""
    attempt_dir = Path(attempt_dir)
    natives = list(attempt_dir.glob("native-*.jsonl"))
    if not natives:
        return ""
    path = max(natives, key=_round_number)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def fix_ticket(task, project_root, verdict, attempt_dir, reason, note=None):
    """The plan ticket for one failed packet: the section, the gates, the transcript, the ask."""
    packet = packet_section(project_root, task)
    note = note if isinstance(note, str) and note.strip() else str(task.get("blocked_reason") or "")
    body = "\n".join(
        [
            f"A `packet` task failed for a reason the operator owes nothing for: {reason}.",
            "",
            "## The packet",
            "",
            packet or "(the packet's brief section could not be read)",
            "",
            "## The last round's gates",
            "",
            last_round_gates(verdict),
            "",
            "## The last 4 KB of the transcript",
            "",
            "```",
            transcript_tail(attempt_dir).strip(),
            "```",
            "",
            "## The question",
            "",
            QUESTION,
            "",
            "## The verdict",
            "",
            note,
            "",
            "Answer with one `packet.md`: the smallest change to the packet, its gates or "
            "the harness that would let this land.",
        ]
    )
    ticket = {
        "title": f"Fix {task['id']} after {reason}",
        "body": body,
        "repo": str(project_root),
    }
    paths = mentioned_paths(packet)
    if paths:
        # The packet's own citations are the scope; without them plan_task greps the body.
        ticket["paths"] = paths
    return ticket


def author_fix_plan(board_dir, project_root, task, verdict, attempt_dir, reason, lanes, note=None):
    """Author the one fix plan a settled packet task is owed, or say why not.

    Returns None when the task is owed nothing (no reason); otherwise a dict with
    `authored` and either the plan's id and lane, or the reason nothing was written. The
    plan task's deterministic id makes a second call for the same (task, reason) a no-op.
    """
    if not reason:
        return None
    plan_id = fix_plan_id(task["id"], reason)
    if (Path(board_dir) / (plan_id + ".json")).exists():
        return {"authored": False, "plan": plan_id, "reason": "already drafted"}
    candidates = plan_lanes(lanes)
    if not candidates:
        return {"authored": False, "plan": plan_id, "reason": "no lane is marked tier plan"}
    ticket = fix_ticket(task, project_root, verdict, attempt_dir, reason, note=note)
    try:
        created = plan_task(board_dir, ticket, candidates[0], task_id=plan_id)
    except (OSError, ValueError, FileExistsError) as exc:
        return {
            "authored": False,
            "plan": plan_id,
            "reason": type(exc).__name__ + ": " + str(exc)[:120],
        }
    return {
        "authored": True,
        "plan": plan_id,
        "lane": candidates[0],
        "task": created["task"],
        "brief": created["brief"],
    }


def verdict_for(packets_root, task_id, blocked_reason):
    """The held attempt's verdict for a blocked task and its directory, else (None, None).

    A blocked packet task's reason names its attempt (`attempt <id> held`), and the
    verdict sits under the packets root. An absent or unreadable verdict returns None —
    never a guess about why the attempt stopped.
    """
    match = re.search(r"attempt (\S+)", str(blocked_reason or ""))
    if match is None or not packets_root:
        return None, None
    aid = match.group(1)
    for path in sorted(Path(packets_root).glob(f"{task_id}/*/attempts/{aid}/verdict.json")):
        try:
            verdict = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(verdict, dict):
            return verdict, path.parent
    return None, None


def recover_fix(board_dir, project_root, task, lanes, packets_root, dry_run):
    """Draft (or plan to draft) a blocked packet task's fix, without authoring twice.

    The pass-loop half of M1: a draft the settlement never wrote — a tick that died
    between the settle and the authoring, or a plan lane the operator configured after
    the failure — is recovered here. Returns a one-line row result, or None when this
    task is owed nothing, already has its plan, or has no plan lane to run on.
    """
    if task.get("category") != "packet" or task.get("state") != "blocked":
        return None
    if str(task.get("blocked_reason") or "").startswith("superseded:"):
        return None
    verdict, attempt_dir = verdict_for(packets_root, task["id"], task.get("blocked_reason"))
    reason = fix_reason(verdict or {}, task.get("blocked_reason"))
    if reason is None:
        return None
    plan_id = fix_plan_id(task["id"], reason)
    if (Path(board_dir) / (plan_id + ".json")).exists() or not plan_lanes(lanes):
        return None
    if dry_run:
        return "fix pending: " + plan_id
    authored = author_fix_plan(
        board_dir, project_root, task, verdict or {}, attempt_dir, reason, lanes
    )
    return "fix: " + plan_id if authored and authored.get("authored") else None
