"""Failover on failure, not race (brief J4).

A packet task that settles blocked with `rounds_exhausted`, `agent_stopped_early` or a
transport refusal has spent its whole round budget on one family. The hedge the board
never had is one retry on a family that fails differently: when a packet settles that
way, the runner authors exactly one successor task whose `lanes` name only lanes of a
*different* family that declare the packet category. The link is recorded on both sides —
`failover_from` on the successor, the existing `superseded: <new id> — …` reason on the
predecessor — and a `failover` ledger event names the family swap. No second attempt is
ever authored on the failed family by this path, and a failover task never fails over
again: a task carrying `failover_from` is never a candidate. The operator turns the whole
behaviour off per task with `"failover": false`; packet tasks default to on.

This module is the decision and the authoring; the runner calls it at the packet
settlement and asks it for the dry-run's pending-failover report. `board/task.py` is
provider-authored (integrated unmodified) and refuses the extra keys, so the successor
and the superseded predecessor validate through `packet_task.validate_board_task`.
"""

import json
import re
from pathlib import Path

from sqlalchemy import select

from ..ledger import attempts as attempt_records
from ..ledger import tasks as task_records
from .packet_task import PACKET_KINDS, validate_board_task

# The settle reasons that earn one different-family retry: the loop's two exhaustion
# names, and a transport refusal (the runner records those with `transport_error` in the
# reason it writes or holds). A wall deadline or a disk guard is not here: those stop the
# loop from outside, they are not the work failing.
FAILOVER_REASONS = ("rounds_exhausted", "agent_stopped_early", "transport_error")


def reason_eligible(*texts):
    """True when any of the texts names a failover reason."""
    return any(
        isinstance(text, str) and any(reason in text for reason in FAILOVER_REASONS)
        for text in texts
    )


def failover_on(task):
    """Packet tasks fail over by default; only an explicit false turns it off."""
    return task.get("failover") is not False


def is_failover(task):
    """True when the task itself was authored as a failover — it never fails over again."""
    return bool(task.get("failover_from"))


def failover_candidate(task, board):
    """True when a settled packet task on this board is due its one different-family retry.

    `board` is the loaded mapping (id -> (path, task)); the successor-exists scan is the
    crash guard: a tick that died between writing the successor and superseding the
    predecessor must not author a second one.
    """
    if task.get("category") != "packet" or task.get("state") != "blocked":
        return False
    if not failover_on(task) or is_failover(task):
        return False
    if str(task.get("blocked_reason") or "").startswith("superseded:"):
        return False
    if not reason_eligible(task.get("blocked_reason")):
        return False
    return not any(other.get("failover_from") == task["id"] for _, other in board.values())


def failover_candidates(lanes, failed_family):
    """Lane ids of a different family that declare the packet category and can run one."""
    return sorted(
        lane_id
        for lane_id, lane in lanes.items()
        if lane.get("family") != failed_family
        and "packet" in (lane.get("categories") or [])
        and lane.get("kind") in PACKET_KINDS
    )


def failed_family(ledger, blocked_reason):
    """The family that ran the attempt named in a blocked task's reason, via the ledger.

    A dry run needs this for failures settled by an earlier tick; the live settlement
    path knows the lane directly and never pays the query.
    """
    match = re.search(r"attempt (\S+)", str(blocked_reason or ""))
    if match is None:
        return None
    with ledger.engine.connect() as con:
        row = (
            con.execute(
                select(attempt_records.c.task).where(attempt_records.c.id == match.group(1))
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        spec = (
            con.execute(select(task_records.c.spec).where(task_records.c.id == row["task"]))
            .mappings()
            .first()
        )
    spec = spec["spec"] if spec else None
    family = (spec or {}).get("family")
    return family if isinstance(family, str) else None


def _write_task(path, task):
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(task, indent=1) + "\n")
    tmp.replace(path)


def author_failover(
    board_dir, project_root, ledger, path, task, failed_family, lanes, board, texts=()
):
    """Author the one different-family retry for a settled packet task, or say why not.

    Returns None when the task is not a failover candidate; otherwise a dict with
    `authored` and either the successor's id and lanes, or the reason nothing was
    written. The successor is written first (a crash then leaves a guarded state, never
    a lost or doubled retry), then the predecessor is superseded, then the ledger event
    names the swap.
    """
    if not failover_candidate(task, board):
        return None
    if not reason_eligible(task.get("blocked_reason"), *texts):
        return None
    candidates = failover_candidates(lanes, failed_family)
    if not candidates:
        return {
            "authored": False,
            "reason": "no lane of a different family declares the packet category",
        }
    from .new import next_retry_id

    new_id = next_retry_id(board_dir, task["id"])
    old_brief = Path(task["brief"])
    new_brief_rel = str(old_brief.with_name(new_id + old_brief.suffix))
    to_families = sorted({str(lanes[c].get("family") or "?") for c in candidates})
    swap = f"failover {failed_family or '?'} -> {', '.join(to_families)}"
    successor = validate_board_task(
        dict(
            task,
            id=new_id,
            brief=new_brief_rel,
            inputs=[new_brief_rel if p == task["brief"] else p for p in task["inputs"]],
            lanes=candidates,
            spec=dict(task["spec"], brief=new_brief_rel),
            state="ready",
            blocked_reason=None,
            failover=True,
            failover_from=task["id"],
        )
    )
    predecessor = validate_board_task(
        dict(task, state="blocked", blocked_reason=f"superseded: {new_id} — {swap}"[:300])
    )
    new_file = Path(board_dir) / (new_id + ".json")
    new_brief = Path(project_root) / new_brief_rel
    if new_file.exists() or new_brief.exists():
        return {"authored": False, "reason": "failover task id already taken: " + new_id}
    new_brief.parent.mkdir(parents=True, exist_ok=True)
    new_brief.write_text((Path(project_root) / task["brief"]).read_text())
    new_file.write_text(json.dumps(successor, indent=1) + "\n")
    _write_task(path, predecessor)
    if ledger is not None:
        with ledger.tx() as con:
            ledger.event(
                con,
                None,
                "failover",
                from_task=task["id"],
                to_task=new_id,
                from_family=failed_family,
                to_families=to_families,
            )
    return {"authored": True, "task": new_id, "lanes": candidates, "families": to_families}


def pending_failover_note(ledger, task, lanes):
    """The dry run's one-line report of the failover that would be authored."""
    family = failed_family(ledger, task.get("blocked_reason"))
    candidates = failover_candidates(lanes, family)
    if not candidates:
        return "failover pending: no lane of a different family declares packet"
    to_families = sorted({str(lanes[c].get("family") or "?") for c in candidates})
    return f"failover pending: {family or '?'} -> {', '.join(to_families)}"
